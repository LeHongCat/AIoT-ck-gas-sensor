// ESP32-S3 gas node: replays a CSV from the microSD card, runs TabM INT8 on the device and
// publishes readings, predictions and ground truth with the pipeline's MQTT contracts. The model
// reads the 16 sensor columns only; the dataset's CO/Ethylene columns are published as
// ground-truth (same observed_at and sequence as the reading), a reference for the dashboards.
//
//   loop() (core 1), every second:  CSV row ─► MQTT readings + ground-truth ─► 60 s window
//   inference task (core 0):        latest full window ─► TabM INT8 ─► MQTT predictions
//
// Inference takes longer than a second on the S3 (and longer still in Wokwi), so the task always
// takes the newest window and skips the ones that arrived while it was busy. A prediction keeps
// the observed_at and sequence of the reading that ended its window. The first reading after boot
// (or after the dataset wraps) is predicted at once: until 60 readings exist the window is padded
// with the oldest one, exactly like the gateway (tabm_model.pad_window), so there is no
// one-minute warm-up before the first prediction.
#include <Arduino.h>
#include <ArduinoJson.h>
#include <PubSubClient.h>
#include <SD.h>
#include <SPI.h>
#include <WiFi.h>
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "tabm_int8.h"

// model/tabm_int8.bin, embedded in flash by board_build.embed_files (platformio.ini).
extern const uint8_t kTabmBlobStart[] asm("_binary_model_tabm_int8_bin_start");
extern const uint8_t kTabmBlobEnd[] asm("_binary_model_tabm_int8_bin_end");

namespace {
// Its own node id: the simulator keeps streaming demo_1hz.csv as virtual-gas-01 and the gateway
// predicts for it, while this board replays the same recording from its SD card under this id.
// Two publishers never share one node id, so neither sequence stream is interleaved.
constexpr char kNodeId[] = "esp32-edge-01";
constexpr char kMqttHost[] = "host.wokwi.internal";
constexpr uint16_t kMqttPort = 1883;
// The single demo dataset: the real UCI 322 recording at 1 Hz, copied into sdcard/.
constexpr char kDatasetPath[] = "/demo_1hz.csv";
constexpr uint8_t kSdSck = 12;
constexpr uint8_t kSdMiso = 13;
constexpr uint8_t kSdMosi = 11;
constexpr uint8_t kSdChipSelect = 10;
constexpr uint8_t kAlarmLed = 2;
constexpr uint32_t kPublishIntervalMs = 1000;
constexpr time_t kValidEpoch = 1700000000;  // 2023-11: anything earlier means SNTP has not run
constexpr size_t kColumnCount = 19;  // t_s, CO, Ethylene, S1..S16
constexpr int kWindow = tabm::kWindow;
constexpr int kSensors = tabm::kSensors;
// Run the 60 s window through the model every kInferEveryS seconds, with the first
// kActiveMembers of the 32 ensemble members (cost scales with it). On the demo data 4 members
// keep F1 CO 0.895 / C2H4 0.856 (32: 0.894 / 0.865); a pass must finish within the cadence.
constexpr uint64_t kInferEveryS = 5;
constexpr int kActiveMembers = 4;

WiFiClient wifiClient;
PubSubClient mqttClient(wifiClient);
File dataset;
uint64_t sequenceNumber = 0;
bool actuatorAlarmOn = false;

struct ReplayEvent {
  float sensors[kSensors] = {};
  float coPpm = 0.0f;  // dataset label: published as ground truth, never a model input
  float ethylenePpm = 0.0f;
};

// ---- 60 s window (loop side) ----
float history[kWindow][kSensors];
int historyCount = 0;
int historyNext = 0;
bool windowRestarted = true;  // the next full window follows a dataset wrap: reset the alarm filter

struct InferenceJob {
  float window[kWindow][kSensors];  // oldest first
  uint64_t sequence;
  char observedAt[25];
  int readings;  // real readings in the window; the rest is padding
  bool restart;
};

struct InferenceResult {
  uint64_t sequence;
  char observedAt[25];
  int readings;
  tabm::Estimate estimate;
  tabm::AlarmLevel level;
  float inferenceMs;
};

QueueHandle_t jobQueue;     // length 1, overwritten: always the newest window
QueueHandle_t resultQueue;  // length 1, read by loop() which owns the MQTT client
tabm::Model model;
InferenceJob pendingJob;
char modelVersion[32];  // "seed42", or "seed42-k4" when only some members run
uint32_t skippedWindows = 0;
TaskHandle_t inferenceTaskHandle = nullptr;
IPAddress mqttHostIp;
bool mqttHostResolved = false;
bool ntpStarted = false;
constexpr uint32_t kDiagnosticsIntervalMs = 30000;

String topic(const char *suffix) {
  return String("aiot/v1/nodes/") + kNodeId + "/" + suffix;
}

String newUuid() {
  uint8_t bytes[16];
  for (size_t i = 0; i < sizeof(bytes); i += 4) {
    const uint32_t randomValue = esp_random();
    memcpy(bytes + i, &randomValue, sizeof(randomValue));
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;

  char uuid[37];
  snprintf(uuid, sizeof(uuid),
           "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
           bytes[0], bytes[1], bytes[2], bytes[3], bytes[4], bytes[5], bytes[6], bytes[7],
           bytes[8], bytes[9], bytes[10], bytes[11], bytes[12], bytes[13], bytes[14], bytes[15]);
  return String(uuid);
}

String utcTimestamp() {
  const time_t now = time(nullptr);
  struct tm utc;
  gmtime_r(&now, &utc);
  char timestamp[25];
  strftime(timestamp, sizeof(timestamp), "%Y-%m-%dT%H:%M:%SZ", &utc);
  return String(timestamp);
}

bool parseDatasetLine(char *line, float values[kColumnCount]) {
  size_t parsed = 0;
  char *cursor = line;
  while (*cursor != '\0' && parsed < kColumnCount) {
    while (*cursor == ' ' || *cursor == '\t' || *cursor == ',' || *cursor == '\r') {
      ++cursor;
    }
    if (*cursor == '\0' || *cursor == '\n') {
      break;
    }
    char *end = nullptr;
    values[parsed] = strtof(cursor, &end);
    if (end == cursor) {
      return false;
    }
    ++parsed;
    cursor = end;
  }
  return parsed == kColumnCount;
}

// One CSV row per second (the header and malformed rows are skipped). Wraps at the end of file.
bool nextReplayEvent(ReplayEvent &event) {
  for (int attempts = 0; attempts < 4; ++attempts) {
    if (!dataset.available()) {
      dataset.seek(0);
      historyCount = 0;  // a wrap breaks the time series: refill the window
      historyNext = 0;
      windowRestarted = true;
      Serial.println("Dataset finished; replaying from the start");
    }
    String row = dataset.readStringUntil('\n');
    char line[384];
    row.toCharArray(line, sizeof(line));
    float values[kColumnCount];
    if (!parseDatasetLine(line, values)) {
      continue;
    }
    event.coPpm = values[1];
    event.ethylenePpm = values[2];
    for (size_t i = 0; i < kSensors; ++i) {
      event.sensors[i] = values[i + 3];
    }
    return true;
  }
  return false;
}

bool publishJson(const String &destination, JsonDocument &document, bool retained = false) {
  char payload[1400];
  const size_t length = serializeJson(document, payload, sizeof(payload));
  if (length == 0 || length >= sizeof(payload)) {
    Serial.println("JSON serialization failed or payload is too large");
    return false;
  }
  return mqttClient.publish(destination.c_str(), payload, retained);
}

void pushWindow(const ReplayEvent &event, const String &observedAt) {
  memcpy(history[historyNext], event.sensors, sizeof(event.sensors));
  historyNext = (historyNext + 1) % kWindow;
  historyCount = historyCount < kWindow ? historyCount + 1 : kWindow;
  if (!windowRestarted && sequenceNumber % kInferEveryS != 0) {
    return;  // on the inference cadence, except the first reading after a (re)start
  }
  if (uxQueueMessagesWaiting(jobQueue) > 0) {
    ++skippedWindows;  // the task has not even started the previous window: it is overwritten
    Serial.printf("Inference busy: skipped the window of sequence %llu (%lu skipped)\r\n",
                  pendingJob.sequence, static_cast<unsigned long>(skippedWindows));
  }
  // Oldest first; a window shorter than 60 s repeats its oldest reading in front.
  const int missing = kWindow - historyCount;
  const int oldest = (historyNext - historyCount + kWindow) % kWindow;
  for (int t = 0; t < kWindow; ++t) {
    const int k = t < missing ? 0 : t - missing;
    memcpy(pendingJob.window[t], history[(oldest + k) % kWindow], sizeof(history[0]));
  }
  pendingJob.sequence = sequenceNumber;
  pendingJob.readings = historyCount;
  observedAt.toCharArray(pendingJob.observedAt, sizeof(pendingJob.observedAt));
  pendingJob.restart = windowRestarted;
  windowRestarted = false;
  xQueueOverwrite(jobQueue, &pendingJob);
}

void publishReading(const ReplayEvent &event) {
  const String observedAt = utcTimestamp();
  JsonDocument reading;
  reading["schema_version"] = 1;
  reading["event_id"] = newUuid();
  reading["node_id"] = kNodeId;
  reading["observed_at"] = observedAt;
  reading["sequence"] = sequenceNumber;
  reading["event_type"] = "sensor.reading";
  reading["sample_rate_hz"] = 1.0;
  JsonArray sensorValues = reading["sensors"].to<JsonArray>();
  for (float value : event.sensors) {
    sensorValues.add(value);
  }
  if (!publishJson(topic("readings"), reading)) {
    Serial.println("Failed to publish sensor reading");
  }

  JsonDocument truth;
  truth["schema_version"] = 1;
  truth["event_id"] = newUuid();
  truth["node_id"] = kNodeId;
  truth["observed_at"] = observedAt;
  truth["sequence"] = sequenceNumber;
  truth["event_type"] = "gas.ground_truth";
  truth["co_ppm"] = event.coPpm > 0.0f ? event.coPpm : 0.0f;
  truth["ethylene_ppm"] = event.ethylenePpm > 0.0f ? event.ethylenePpm : 0.0f;
  if (!publishJson(topic("ground-truth"), truth)) {
    Serial.println("Failed to publish ground truth");
  }

  pushWindow(event, observedAt);
  Serial.printf("Published sequence=%llu window=%d/%d\r\n", sequenceNumber, historyCount, kWindow);
  ++sequenceNumber;
}

void publishPrediction(const InferenceResult &result) {
  const tabm::Estimate &e = result.estimate;
  JsonDocument prediction;
  prediction["schema_version"] = 1;
  prediction["event_id"] = newUuid();
  prediction["node_id"] = kNodeId;
  prediction["observed_at"] = result.observedAt;
  prediction["sequence"] = result.sequence;
  prediction["event_type"] = "gas.prediction";
  prediction["co_ppm"] = e.ppm[0];
  prediction["ethylene_ppm"] = e.ppm[1];
  prediction["model_name"] = tabm::kModelName;
  prediction["model_version"] = modelVersion;
  prediction["alarm_level"] = tabm::alarmName(result.level);
  prediction["inference_ms"] = result.inferenceMs;
  prediction["window_s"] = result.readings;
  if (!publishJson(topic("predictions"), prediction)) {
    Serial.println("Failed to publish prediction");
  }
  Serial.printf("TabM seq=%llu P(CO)=%.3f P(C2H4)=%.3f CO=%.1f ppm (±%.0f%s) C2H4=%.2f ppm "
                "(±%.1f%s) alarm=%s %.0f ms%s\r\n",
                result.sequence, e.prob[0], e.prob[1], e.ppm[0], e.uctPpm[0],
                e.uncertain[0] ? " uncertain" : "", e.ppm[1], e.uctPpm[1],
                e.uncertain[1] ? " uncertain" : "", tabm::alarmName(result.level),
                result.inferenceMs,
                result.inferenceMs > kInferEveryS * 1000.0f ? " (slower than the cadence)" : "");
}

void yieldToScheduler() {
  vTaskDelay(1);  // lets IDLE0 run so the task watchdog stays fed during a long forward pass
}

void inferenceTask(void *) {
  static InferenceJob job;
  static InferenceResult result;
  static tabm::AlarmFilter alarm;
  float features[tabm::kFeatures];
  float raw[tabm::kOutputs];
  for (;;) {
    if (xQueueReceive(jobQueue, &job, portMAX_DELAY) != pdTRUE) {
      continue;
    }
    if (job.restart) {
      alarm = tabm::AlarmFilter();
    }
    const int64_t started = esp_timer_get_time();
    tabm::computeFeatures(job.window, features);
    model.forward(features, raw, yieldToScheduler);
    tabm::decode(raw, result.estimate);
    result.inferenceMs = (esp_timer_get_time() - started) / 1000.0f;
    result.level = alarm.update(result.estimate, job.sequence);
    result.sequence = job.sequence;
    result.readings = job.readings;
    memcpy(result.observedAt, job.observedAt, sizeof(result.observedAt));
    xQueueOverwrite(resultQueue, &result);
  }
}

// Runtime memory and stack headroom (benchmark D07), every 30 s on .../diagnostics. The bridge does
// not forward this topic: it is read straight from the broker by scripts/bench/wokwi_device.py.
void publishDiagnostics() {
  JsonDocument diag;
  diag["node_id"] = kNodeId;
  diag["observed_at"] = utcTimestamp();
  diag["uptime_ms"] = millis();
  diag["cpu_mhz"] = getCpuFrequencyMhz();
  diag["heap_size"] = ESP.getHeapSize();
  diag["free_heap"] = ESP.getFreeHeap();
  diag["min_free_heap"] = ESP.getMinFreeHeap();
  diag["max_alloc_heap"] = ESP.getMaxAllocHeap();
  diag["psram_size"] = ESP.getPsramSize();
  diag["min_free_psram"] = ESP.getMinFreePsram();
  // ESP-IDF FreeRTOS counts stack in bytes.
  diag["inference_stack_size"] = 12288;
  diag["inference_stack_free_min"] =
      inferenceTaskHandle ? uxTaskGetStackHighWaterMark(inferenceTaskHandle) : 0;
  diag["loop_stack_free_min"] = uxTaskGetStackHighWaterMark(nullptr);
  diag["skipped_windows"] = skippedWindows;
  publishJson(topic("diagnostics"), diag);
}

void onMqttMessage(char *receivedTopic, byte *payload, unsigned int length) {
  JsonDocument command;
  const DeserializationError error = deserializeJson(command, payload, length);
  if (error || String(receivedTopic) != topic("commands")) {
    Serial.println("Ignored invalid MQTT command");
    return;
  }

  const char *commandId = command["event_id"] | "";
  const char *mode = command["mode"] | "auto";
  const char *source = command["source"] | "safety-controller";
  if (strlen(commandId) != 36) {
    Serial.println("Rejected command without a UUID event_id");
    return;
  }

  const bool fanOn = command["fan_on"] | false;
  const bool exhaustOn = command["exhaust_on"] | false;
  const bool gasValveClosed = command["gas_valve_closed"] | false;
  actuatorAlarmOn = command["alarm_on"] | false;
  const bool suppressionOn = command["suppression_on"] | false;
  const bool alarmMuted = command["alarm_muted"] | false;
  digitalWrite(kAlarmLed, actuatorAlarmOn ? HIGH : LOW);

  JsonDocument state;
  state["schema_version"] = 1;
  state["event_id"] = newUuid();
  state["node_id"] = kNodeId;
  state["observed_at"] = utcTimestamp();
  state["sequence"] = command["sequence"] | 0ULL;
  state["event_type"] = "actuator.state";
  state["command_id"] = commandId;
  state["mode"] = mode;
  state["source"] = source;
  state["fan_on"] = fanOn;
  state["exhaust_on"] = exhaustOn;
  state["gas_valve_closed"] = gasValveClosed;
  state["alarm_on"] = actuatorAlarmOn;
  state["suppression_on"] = suppressionOn;
  state["alarm_muted"] = alarmMuted;

  if (publishJson(topic("actuator-state"), state, true)) {
    Serial.printf("Applied command %s; actuator state acknowledged\r\n", commandId);
  } else {
    Serial.println("Failed to publish actuator acknowledgement");
  }
}

void connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin("Wokwi-GUEST", "", 6);
  Serial.print("Connecting to Wokwi WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(250);
    Serial.print('.');
  }
  Serial.printf(" connected, IP=%s\r\n", WiFi.localIP().toString().c_str());

  // Resolve the broker once, before SNTP starts, and connect by IP from then on. With
  // Arduino-ESP32 3.3, hostByName() clears the DNS cache without the TCP/IP core lock; while an
  // SNTP lookup is pending (NTP unreachable, as in Wokwi) that fires the SNTP callback and lwIP
  // aborts ("Required to lock TCPIP core functionality").
  if (!mqttHostResolved && WiFi.hostByName(kMqttHost, mqttHostIp)) {
    mqttHostResolved = true;
    mqttClient.setServer(mqttHostIp, kMqttPort);
    Serial.printf("MQTT broker %s is %s\r\n", kMqttHost, mqttHostIp.toString().c_str());
  }

  if (ntpStarted) {
    return;  // a WiFi reconnect: SNTP keeps running on its own
  }
  ntpStarted = true;
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");
  const uint32_t started = millis();
  while (time(nullptr) < kValidEpoch && millis() - started < 15000) {
    delay(250);
  }
  if (time(nullptr) < kValidEpoch) {
    Serial.println("NTP not synced yet; the replay waits for it");
  }
}

void connectMqtt() {
  while (!mqttClient.connected()) {
    const String clientId = String("wokwi-") + kNodeId + "-" + String(esp_random(), HEX);
    Serial.printf("Connecting to MQTT at %s:%u...\r\n", kMqttHost, kMqttPort);
    if (mqttClient.connect(clientId.c_str())) {
      mqttClient.subscribe(topic("commands").c_str(), 1);
      Serial.println("MQTT connected; subscribed to commands");
    } else {
      Serial.printf("MQTT failed, state=%d; retrying\r\n", mqttClient.state());
      delay(2000);
    }
  }
}
}  // namespace

void setup() {
  Serial.begin(115200);
  pinMode(kAlarmLed, OUTPUT);
  digitalWrite(kAlarmLed, LOW);

  const size_t blobSize = static_cast<size_t>(kTabmBlobEnd - kTabmBlobStart);
  if (!model.begin(reinterpret_cast<const int8_t *>(kTabmBlobStart), blobSize, kActiveMembers)) {
    Serial.printf("TabM blob is %u bytes, expected %u: rerun tools/export_tabm_esp32.py\r\n",
                  static_cast<unsigned int>(blobSize), static_cast<unsigned int>(tabm::kBlobSize));
    while (true) delay(1000);
  }
  if (kActiveMembers < tabm::kMembers) {
    snprintf(modelVersion, sizeof(modelVersion), "%s-k%d", tabm::kModelVersion, kActiveMembers);
  } else {
    snprintf(modelVersion, sizeof(modelVersion), "%s", tabm::kModelVersion);
  }
  Serial.printf("Model %s/%s ready (%u bytes of INT8 weights in flash, %d/%d members, every %llu s)"
                "\r\n",
                tabm::kModelName, modelVersion, static_cast<unsigned int>(blobSize), kActiveMembers,
                tabm::kMembers, kInferEveryS);

  SPI.begin(kSdSck, kSdMiso, kSdMosi, kSdChipSelect);
  if (!SD.begin(kSdChipSelect, SPI)) {
    Serial.println("microSD mount failed");
    while (true) delay(1000);
  }
  dataset = SD.open(kDatasetPath, FILE_READ);
  if (!dataset) {
    Serial.printf("Missing %s on microSD. Copy it from demo_data/ into sdcard/ first.\r\n",
                  kDatasetPath);
    while (true) delay(1000);
  }
  Serial.printf("Dataset opened: %s (%u bytes)\r\n", kDatasetPath,
                static_cast<unsigned int>(dataset.size()));

  jobQueue = xQueueCreate(1, sizeof(InferenceJob));
  resultQueue = xQueueCreate(1, sizeof(InferenceResult));
  if (jobQueue == nullptr || resultQueue == nullptr ||
      xTaskCreatePinnedToCore(inferenceTask, "tabm", 12288, nullptr, 1, &inferenceTaskHandle, 0) != pdPASS) {
    Serial.println("Could not start the inference task");
    while (true) delay(1000);
  }

  connectWifi();
  if (!mqttHostResolved) {
    mqttClient.setServer(kMqttHost, kMqttPort);  // fallback: resolve on connect
  }
  mqttClient.setCallback(onMqttMessage);
  mqttClient.setBufferSize(2048);
  connectMqtt();
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    connectWifi();
  }
  if (!mqttClient.connected()) {
    connectMqtt();
  }
  mqttClient.loop();

  static InferenceResult result;
  if (xQueueReceive(resultQueue, &result, 0) == pdTRUE) {
    publishPrediction(result);
  }

  // Hold the replay until SNTP has set the clock: a reading stamped 1970 falls outside the
  // InfluxDB retention period. The replay resumes where it stopped, so no reading is lost.
  if (time(nullptr) < kValidEpoch) {
    return;
  }

  static uint32_t lastDiagnostics = 0;
  if (millis() - lastDiagnostics >= kDiagnosticsIntervalMs) {
    publishDiagnostics();
    lastDiagnostics = millis();
  }

  static uint32_t lastPublish = 0;
  if (millis() - lastPublish >= kPublishIntervalMs) {
    ReplayEvent event;
    if (nextReplayEvent(event)) {
      publishReading(event);
      lastPublish = millis();
    }
  }
}
