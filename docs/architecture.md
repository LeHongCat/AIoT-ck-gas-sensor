# Kiến trúc pipeline hiện tại

## 1. Bức tranh tổng thể

Hệ thống có ba plane. Việc tách chúng giúp lỗi ở dashboard không làm dừng thu thập dữ liệu,
và lỗi lưu trữ không trực tiếp khóa đường điều khiển thiết bị.

```text
DATA PLANE
16 sensors --MQTT readings--> Mosquitto --> Edge inference (60 s window -> TabM INT8)
                                 ^                 |  \--> /logs/predictions.jsonl
                                 +-- predictions --+
                              Mosquitto --> MQTT-Kafka Bridge --> Kafka
                                                               |      \
                                                               |       +--> Kitchen UI
                                                               v
                                                          Influx Sink
                                                               |
                                                               v
                                                           InfluxDB --> Grafana

CONTROL PLANE
Prediction -------------------------> Kafka -----> Safety Controller
Kitchen UI --MQTT control request---> Bridge ----/         |
                                                           | MQTT command
                                                           v
                                                     Device/Simulator
                                                           |
                                                           | MQTT state/ACK
                                                           v
                                                  Bridge -> Kafka -> UI/InfluxDB

OBSERVABILITY PLANE
Bridge metrics ------------\
Edge inference metrics -----+
Safety Controller metrics --+
Kafka exporter -------------+--> Prometheus (recording + alert rules) --> Grafana
Sink metrics ---------------+
InfluxDB / Grafana metrics -/
```

## 2. Input của hệ thống

### Sensor input

`SensorReading` là input gốc:

- `schema_version`, `event_id` để versioning và trace.
- `node_id` định danh thiết bị/phòng.
- `observed_at` là thời điểm đo UTC, không phải thời điểm server nhận.
- `sequence` tăng dần để phát hiện mất gói.
- `sample_rate_hz`, mặc định 1 Hz.
- `sensors`: đúng 16 giá trị MOX `S1..S16`.

MQTT topic: `aiot/v1/nodes/{node_id}/readings`.

### Model input/output

Service `edge-inference` (`edge_inference.py` + `tabm_model.py`) subscribe
`aiot/v1/nodes/+/readings` trực tiếp trên broker cục bộ — đúng vị trí của edge gateway — và với
mỗi gói 16 kênh:

1. Đẩy vector vào **cửa sổ 60 s** của node đó. Chưa đủ 60 s thì cửa sổ được đệm bằng mẫu cũ nhất
   nên node dự đoán ngay từ gói đầu tiên; `window_s` cho biết số giây thật.
2. Tính **144 đặc trưng** = 16 kênh × 9: `last, mean, std, min, max, slope, d5, d15, d30`, rồi
   chuẩn hóa theo tham số trong `tabm_int8.json` (xuất từ `model/preprocessing.json`).
3. **TabM INT8** (ensemble 32 thành viên, 4 block × 1024, lượng tử QDQ) chạy bằng phép tính số
   nguyên trên cùng blob với firmware ESP32 → xác suất có khí, nồng độ CO / C₂H₄ (ppm) và độ bất
   định (đầu ra DER).
4. Trung bình nhân quả 5 s → `alarm_level`: warning khi P(CO) ≥ τ = 0,48, danger khi CO ≥ 200 ppm.
   C₂H₄ được ước lượng và báo nhưng không gây alarm.
5. `top_channels`: tối đa 3 kênh đóng góp lớn nhất so với nền khí sạch, chỉ khi có khí đáng kể.

Node tự chạy model (ESP32-S3) publish dự đoán của chính nó; gateway thấy dự đoán đó (MQTT 5
No Local nên không nhận lại tin của chính mình) và tạm dừng cho node này trong
`EDGE_ON_DEVICE_TIMEOUT_S` (30 s). `EDGE_SKIP_NODES` ghim cố định các node không bao giờ được
gateway phục vụ.

`Prediction` publish lên `.../predictions` (QoS 1):

- `co_ppm`, `ethylene_ppm`, `alarm_level`: `normal`, `warning`, `danger`.
- `model_name` (`tabm-int8`), `model_version` (`seed42`; firmware 4 thành viên là `seed42-k4`),
  `inference_ms` (~10 ms trên gateway, ~4,5 s trên ESP32-S3 trong Wokwi).
- `window_s`, `top_channels`.

Prediction giữ cùng `observed_at` và `sequence` của sensor input để Grafana và UI ghép
prediction với input tại cùng thời điểm. Mỗi dự đoán còn được ghi một dòng JSON vào
`PREDICTION_LOG` (`/logs/predictions.jsonl` trong volume `edge_logs`, xoay vòng 5 MiB × 3) gồm
16 input, `window_s`, output, alarm, kênh chính và thời gian suy luận — log thô của model, độc
lập với Kafka/InfluxDB.

`EDGE_MODEL=ridge` chọn model dự phòng **Ridge** (`edge_model.py`, Python thuần, ~0,5 ms): cùng
cửa sổ 60 s nhưng 144 đặc trưng riêng (`last, mean, min, max, std, slope10, slope30, rise,
deconv`), alarm có hysteresis (warning ≥ 50 ppm CO hoặc ≥ 3 ppm C₂H₄, tắt khi < 40 ppm; danger
≥ 200 ppm CO). Trọng số ở `models/edge_ridge_v1.json`, sinh bởi `tools/train_edge_model.py` (chia
theo thời gian: train 0–60 %, test 60–80 %). Bản trong repo huấn luyện trên dữ liệu tổng hợp nên
không dùng cho `demo_1hz.csv`; dùng `--dataset data/ethylene_CO.txt` để huấn luyện trên UCI 322.

Hai log có thể đối chiếu theo `sequence`:

| Log | Nguồn | Nơi lưu |
|---|---|---|
| Log cảm biến (16 kênh thô) | `.../readings` → Bridge → `gas.sensor.readings.v1` → sink | `sensor_reading` (InfluxDB), bảng "Log cảm biến" (UI, Grafana) |
| Log dự đoán | `.../predictions` → Bridge → `gas.predictions.v1` → sink | `gas_prediction` (InfluxDB), `predictions.jsonl`, bảng "Log dự đoán" (UI, Grafana) |

### Operator input

Kitchen UI tạo `ControlRequest` gồm:

- `mode`: `manual` biểu diễn direct override có TTL; `auto` dùng để hủy override ngay;
  `mute` / `unmute` tắt hoặc bật lại chuông (công tắc trên màn hình bếp và panel Grafana).
- năm desired outputs: `fan_on`, `exhaust_on`, `gas_valve_closed`, `alarm_on`,
  `suppression_on` (hệ thống dập lửa chụp hút; mặc định `False` nên payload cũ vẫn hợp lệ).

Khi chuông bị tắt, Safety Controller vẫn ở AUTO và chỉ ép `alarm_on = false`; `ActuatorCommand` và
`ActuatorState` mang `alarm_muted = true` (mặc định `false`, firmware cũ vẫn hợp lệ) nên lệnh, ACK,
dự đoán và log vẫn đi đủ đường Kafka → InfluxDB. DANGER mới bật lại chuông.

### Demo input

`DemoScenario` (`co_warning`, `co_spike`, `ethylene_spike`, `mixture`, `clear`) đi thẳng từ
Kitchen UI tới simulator qua MQTT `.../demo-scenario`. Nó chỉ dùng cho demo, không qua bridge.

Simulator mô hình bếp là một thể tích không khí trộn đều: nguồn rò bơm khí vào, thông gió tự
nhiên + quạt chụp hút + quạt mái hút ra, **van gas đóng thì nguồn rò bị cô lập**. Nhờ vậy lệnh
của Safety Controller/operator làm khí giảm thật; `clear` chỉ ngắt nguồn, khí vẫn tan dần.
Trạng thái (`phase`: `clean`/`leaking`/`isolated`/`clearing`, cường độ nguồn, nồng độ trong
phòng, mức thông gió, ETA về an toàn) được publish retained lên `.../demo-state` mỗi giây cho
Kitchen UI hiển thị.

Request được validate và audit qua Kafka trước khi Safety Controller xử lý.

## 3. Xử lý qua từng tầng

### Mosquitto

Nhận telemetry và control event bằng MQTT QoS 1. MQTT phù hợp kết nối IoT nhẹ và command có
latency thấp. QoS 1 có thể tạo duplicate, nên downstream phải idempotent.

### MQTT-Kafka Bridge

Bridge chỉ subscribe sáu suffix được cho phép, không còn wildcard nhận mọi topic. Nó:

1. Chọn Pydantic contract theo suffix.
2. Kiểm tra schema và `node_id` giữa topic/payload.
3. Phát hiện sequence gap và đo event age.
4. Produce Kafka với key=`node_id`, idempotence, `acks=all`, Snappy và batching ngắn.
5. Đưa payload sai contract vào `gas.dead-letter.v1`.

Bridge không inference và không ghi InfluxDB. Vì bridge thấy cả `.../commands` lẫn
`.../actuator-state` trên MQTT, nó ghép command với ACK theo `command_id` để đo
`aiot_actuator_ack_latency_seconds`, đếm lệnh đang chờ và lệnh quá 5 giây không có ACK.
ACK retained không có command tương ứng được bỏ qua.

### Kafka

Kafka là durable event backbone:

- Buffer khi consumer chậm hoặc tạm dừng.
- Replay cho consumer mới.
- Tách ingestion, storage, UI và control service.
- Key theo `node_id` giữ ordering trong từng node/partition.

Demo dùng một KRaft broker và replication factor 1. Đây là cấu hình local, không phải production.

### Safety Controller

Controller consume prediction và control request bằng group `safety-controller-v1`.

- Auto `normal`: tắt quạt/xả/còi, mở van gas.
- Auto `warning`: bật quạt, xả khí và còi.
- Auto `danger`: bật quạt, xả khí, còi và khóa van gas.
- Direct override: áp dụng ngay tổ hợp operator chọn trong 30 giây, không chuyển UI sang mode khác.
- Interlock dập lửa: `suppression_on` luôn kéo theo khóa gas và bật còi; Auto không tự phun.
  Khi danger chiếm quyền, lần phun đang chạy được giữ tới hạn của nó (danger chỉ thêm bảo vệ).
- Prediction `danger` luôn ưu tiên Auto và hủy override đang hoạt động.
- Command chỉ phát khi desired state thay đổi để tránh duplicate command mỗi giây.
- Metrics (`:8000`): alarm level hiện tại, override đang hoạt động, desired output, số lệnh theo
  mode/trigger, số quyết định bị chặn trùng và control loop lag (tuổi prediction khi xử lý).

Command cuối cùng publish bằng MQTT tới `.../commands`. Thiết bị chỉ được xem là đã đổi trạng
thái sau khi publish `ActuatorState` ACK về `.../actuator-state`.

### Influx Sink

Sink consume toàn bộ telemetry/control topics bằng group `influx-sink-v1`, gom batch và ghi
InfluxDB. Auto commit bị tắt. Offset chỉ được commit sau khi InfluxDB xác nhận batch, vì vậy
restart không làm mất record đã consume nhưng chưa lưu.

Sau mỗi batch thành công, sink ghi `aiot_pipeline_latency_seconds` = thời điểm InfluxDB xác
nhận − `observed_at`, tức latency end-to-end của dữ liệu đã bền vững. Batch flush theo
`INFLUX_BATCH_SIZE` (200) hoặc `INFLUX_FLUSH_SECONDS` (0.5s); khoảng flush chiếm phần lớn
latency. Đo trên stack demo: giảm flush từ 1s xuống 0.5s đưa p95 sensor từ ~1.4s xuống ~0.6s
(p50 ~0.26s), đổi lại vài request ghi nhỏ hơn mỗi giây.

Payload độc chèn thẳng vào Kafka được ghi metric rồi skip để không chặn partition vô hạn.

### Kitchen UI (bếp công nghiệp)

Backend là Kafka consumer độc lập `kitchen-demo-ui-v1`:

- Giữ snapshot mới nhất, event feed và 600 điểm (10 phút) lịch sử sensor/prediction
  trong RAM; lịch sử prediction gồm cả alarm, `inference_ms`, kênh chính và `window_s` để vẽ log.
- Đọc telemetry và actuator ACK từ Kafka. Khi khởi động, consumer bắt đầu ở cuối mỗi partition
  (màn hình live không cần replay), riêng hai topic actuator đọc lại đúng record cuối để có
  ngay trạng thái thiết bị. `session.timeout.ms=6000` giúp UI khởi động lại nhận partition sau
  ~5 s thay vì ~45 s khi tiến trình cũ không kịp rời group.
- `GET /api/stream` (Server-Sent Events) đẩy snapshot chỉ khi có record mới, kiểm tra mỗi
  200 ms và gửi keep-alive mỗi 15s; `GET /api/state` giữ lại cho fallback polling.
- Tính msg/s (cửa sổ 10s) và latency Edge → UI (EMA) cho từng topic. Event cũ hơn 30s khi tới
  (ACK retained, replay) được đánh dấu `replayed` và không tính vào latency.
- **MqttObserver** (`observers.py`) là một client MQTT chỉ đọc, subscribe `aiot/v1/nodes/+/+`
  với QoS 0: UI thấy đúng message đang đi qua Mosquitto (topic, kích thước, tốc độ, payload rút
  gọn) và vài thống kê `$SYS`. Nó không publish gì và không nằm trên data path.
- **PipelineMetrics** gửi 2 truy vấn Prometheus mỗi 5 s **phía server** (recording rules + alert
  đang firing) và dùng chung cho mọi trình duyệt. Prometheus lỗi thì UI chỉ hiện "Prometheus
  chưa sẵn sàng", không ảnh hưởng gì khác. Thay cho 9 truy vấn mỗi trình duyệt của bản đầu.
- POST direct override/cancel tới MQTT, nhưng chỉ render trạng thái actuator đã ACK qua Kafka.

Frontend vẽ mặt bằng bếp công nghiệp bằng SVG (`static/kitchen.js`): 9 nhân sự có lịch công
việc theo vai trò, tìm đường trên đồ thị lối đi, giữ chỗ trạm làm việc, và phản ứng theo
trạng thái đã ACK (quạt, xả khí, van gas, còi, dập lửa) cùng alarm level (`danger` hoặc phun
dập lửa → sơ tán).

- Mặt bằng tĩnh (sàn, thiết bị, bóng đổ) và lớp động (người, lửa, quạt, khói) là hai SVG chồng
  nhau, nên phần tĩnh không phải vẽ lại mỗi frame. Hiệu ứng còi chỉ animate `opacity`.
- Mô phỏng chạy theo thời gian thực với bước con ≤ 50 ms: FPS thấp không làm người đi chậm lại.
  Né người chỉ là dịch chuyển hiển thị sang bên, vị trí thật luôn ở trên lối đi nên không thể kẹt.
- Một frame lỗi không dừng vòng lặp; lỗi được ghi lại (`KitchenScene.errors()`).
- `static/flow.js` vẽ sơ đồ pipeline; mỗi record mới nhận từ Kafka sinh một chấm chạy đúng
  đường của nó (qua dòng MQTT topic và Kafka topic tương ứng).
- Xả khẩn cấp và phun dập lửa có hiệu ứng riêng trên mặt bằng (luồng khí, vòi phun, bọt) và HUD
  đếm ngược; toast hiển thị round-trip UI → MQTT → Kafka → Controller → thiết bị → ACK.
- Màn hình đầu chứa ba khu: panel **trạng thái khí** (alarm + CO + C₂H₄ + xu hướng + dòng rò rỉ/
  ETA từ `demo-state`, chiều cao cố định), **mặt bằng bếp** và **điều khiển**. Chiều cao mặt bằng
  = `100vh − --fold` (phần còn lại của màn hình), chiều rộng suy ra từ tỉ lệ 1720:1000, nên cả ba
  khu vừa một màn hình từ ~1500 × 730. Xả khẩn cấp không bật còi.
- 16 cảm biến là **một bo e-nose** bên phải mặt bằng (hàng = loại cảm biến), không rải trên sàn:
  model cần cả 16 kênh đo cùng một mẫu khí. Trên mặt bằng chỉ có vị trí bo (giữa chụp hút, cạnh
  ống gas) và, khi rò rỉ, đường nối tới điểm rò kèm khoảng cách (thang 1 đơn vị SVG = 1 cm).
- Card **Edge model** hiển thị 6 bước của model với số liệu sống, log cảm biến
  (16 ô tô màu theo mức tăng so với nền) và log dự đoán (dự đoán vs thật, alarm, ms, kênh chính).
  Model info đọc từ `GET /api/config`.

### Grafana và Prometheus

- `AIoT Gas Monitoring`: trạng thái hiện tại, CO/C₂H₄ do edge model dự đoán real-time kèm dải
  ngưỡng, timeline alarm và actuator ACK (có dòng "Chuông tắt tay"), panel **Chuông cảnh báo**
  (Canvas: đèn, chuông, hai nút action gọi `POST /api/control`), audit trail lệnh, bảng **log dự
  đoán** (`gas_prediction`) và **log cảm biến** (pivot S1..S16), 16 kênh MOX. Grafana không hiển
  thị ground truth; chỉ màn hình bếp so dự đoán với nhãn.
- Tiêu đề panel chỉ ghi tên (ví dụ "Latency end-to-end p95"); giải thích nằm trong description
  (biểu tượng ⓘ).
- `AIoT Pipeline Operations` (trang chủ Grafana): ① bản đồ pipeline dạng Canvas với số liệu
  sống từng tầng; ② thiết bị & cảm biến (node online, gói/giây, bảng 16 kênh, timeline kênh phản
  ứng); ③ MQTT (broker stats, bảng topic MQTT → Kafka, publisher/subscriber, độ trễ chặng MQTT);
  ④–⑨ alert, throughput/latency, control plane, bridge/DLQ, Kafka, InfluxDB.
- Edge inference xuất `aiot_inference_seconds` (histogram), `aiot_predictions_total{alarm_level}`
  và `aiot_predicted_ppm{gas}`; rule `AiotEdgeInferenceStale` cảnh báo khi model ngừng dự đoán.
- Bridge xuất thêm `aiot_sensor_reading{channel,sensor_type}`, `aiot_gas_ppm{gas,source}` và
  `aiot_mqtt_bytes_received_total{event_type}` để dashboard thấy từng node gửi gì.
- Dashboard được sinh bởi `infra/grafana/build_dashboards.py`; `tests/test_dashboards.py` kiểm
  JSON đã commit khớp script, refId không trùng và Canvas chỉ trỏ tới field có thật.
- Prometheus là observability plane; nó không nằm trên data/control critical path. Recording
  rules tính sẵn throughput/lag/latency để dashboard refresh 5s không phải tính lại histogram.
- Annotation DANGER và Direct override phủ lên mọi biểu đồ thời gian của cả hai dashboard.

## 4. Output của hệ thống

| Output | Consumer | Ý nghĩa |
|---|---|---|
| Kafka telemetry topics | UI, Influx Sink, service mới | Stream có thể replay |
| MQTT actuator command | Thiết bị/simulator | Desired physical state |
| Kafka actuator state | UI, Influx Sink | ACK trạng thái thực tế |
| Influx measurements | Grafana | Lịch sử time-series và audit |
| Prometheus metrics | Grafana Operations | Sức khỏe pipeline |
| `predictions.jsonl` | Debug/đánh giá model | Log thô input → output của model |
| Dead-letter event | Operator/debug tool | Payload bị từ chối và nguyên nhân |

## 5. Topic và measurement

| Event | MQTT topic | Kafka topic | Influx measurement |
|---|---|---|---|
| Sensor | `.../readings` | `gas.sensor.readings.v1` | `sensor_reading` |
| Prediction (edge-inference) | `.../predictions` | `gas.predictions.v1` | `gas_prediction` |
| Control request | `.../control-requests` | `gas.control-requests.v1` | `actuator_control_request` |
| Command | `.../commands` | `gas.actuator-commands.v1` | `actuator_command` |
| Device ACK | `.../actuator-state` | `gas.actuator-state.v1` | `actuator_state` |
| Demo scenario (UI → simulator) | `.../demo-scenario` | — | — |
| Demo state (simulator → UI, retained) | `.../demo-state` | — | — |
| Invalid | — | `gas.dead-letter.v1` | — |

`...` là `aiot/v1/nodes/{node_id}`.

## 6. Delivery và consistency

- End-to-end là **at-least-once**, không tuyên bố exactly-once.
- MQTT QoS 1 có thể duplicate.
- Kafka producer idempotence giảm duplicate do retry trong producer session.
- Influx natural identity là measurement + tags + timestamp; ghi lại cùng timestamp cập nhật
  cùng logical point.
- `event_id`, `command_id`, `sequence` cho phép trace request → command → ACK.
- Direct override giữ tối đa 30 giây trong memory của Safety Controller. Hết TTL, có `danger`,
  hoặc operator bấm hủy thì Auto tiếp quản lại ở prediction kế tiếp.

## 7. Các tối ưu đã áp dụng

1. Bridge subscribe allow-list thay vì `+/+`, giảm traffic không liên quan và tránh command lạ.
2. Kafka producer dùng idempotence, `acks=all`, Snappy và linger 20 ms để gom batch nhỏ.
3. Mọi event key theo node để giữ ordering cục bộ.
4. Influx sink batch write và commit đúng offset của batch sau khi ghi thành công. Batch bị
   InfluxDB từ chối vì dữ liệu (HTTP 400/422, ví dụ timestamp ngoài retention) được ghi log và
   commit qua, để một bản ghi lỗi không làm dừng cả partition; lỗi InfluxDB không sẵn sàng vẫn
   được thử lại.
5. Safety Controller deduplicate desired state; alarm giữ nguyên không tạo command mỗi giây.
6. Kitchen UI không truy vấn Prometheus từ trình duyệt; backend tóm tắt 2 truy vấn/5 s cho mọi
   người xem.
7. Control dùng command/ACK thay vì optimistic UI, tránh hiển thị thiết bị đã bật khi thiết bị
   chưa thực sự phản hồi.
8. Kitchen UI nhận dữ liệu bằng SSE push thay cho polling 1s: độ trễ hiển thị ≤ 200 ms và không
   tải lại snapshot khi không có record mới.
9. Sink flush 0.5s và không còn serialize mỗi point hai lần chỉ để lấy tên measurement.
10. Prometheus recording rules cho các series dashboard dùng nhiều; consumer lag được clamp
    theo từng partition trước khi cộng (kafka-exporter trả `-1` cho partition chưa commit).

11. Docker image dùng chung cho 5 service Python, dependency nằm ở layer riêng: sửa code chỉ
    rebuild ~15 s.

## 8. Tài nguyên

Mọi container có `deploy.resources.limits` và log `json-file` xoay vòng 3 × 10 MB.

| Service | Đo (1 nút, 24/10) | Giới hạn | Ghi chú |
|---|--:|--:|---|
| Kafka | ~680 MiB | 1 CPU · 768M | `KAFKA_HEAP_OPTS=-Xms256m -Xmx512m`, 2 network/2 I/O thread, segment 64 MiB, `retention.bytes` 256 MiB/partition |
| Grafana | ~250 MiB | 1 CPU · 640M | tắt analytics/update check/news |
| InfluxDB | ~100 MiB | 1 CPU · 512M | cache 64 MiB, query memory 64 MiB, concurrency 8 |
| Prometheus | ~180 MiB | 0.5 CPU · 384M | retention 15 ngày hoặc 1 GB |
| bridge / sink / controller / simulator | 28–50 MiB | 0.25–0.5 CPU · 128–192M | `MALLOC_ARENA_MAX=2` |
| Kitchen UI | ~180 MiB | 0.5 CPU · 192M | 10 phút lịch sử mỗi node trong RAM |
| edge-inference | ~55 MiB | 0.5 CPU · 192M | BLAS float32 1 luồng |
| Mosquitto, kafka-exporter | 5–12 MiB | 0.25 CPU · 64M | `max_packet_size 64 KiB`, `max_queued_messages 1000` |

Bộ đệm librdkafka được đặt nhỏ hơn giới hạn RAM: producer của bridge tối đa 32 MiB
(`queue.buffering.max.kbytes`, mặc định 1 GiB). Nếu Kafka sập lâu và bộ đệm đầy, bridge nhận
`BufferError`, đếm vào `aiot_mqtt_invalid_messages_total{reason="BufferError"}`, ghi log và bỏ
message đó thay vì bị OOM; khoảng ~30 MiB đệm tương đương hàng chục phút dữ liệu ở 3 msg/s), consumer 4 MiB (UI, controller) và 16 MiB (sink) thay cho
64 MiB mặc định. `kafka-init` (`aiot_pipeline/kafka_topics.py`) chờ broker trả lời rồi tạo các
topic còn thiếu trong một lệnh AdminClient: dưới 1 giây, thay cho 7 lần khởi động JVM
`kafka-topics.sh` (~45 s) mà mọi service phía sau phải chờ.

## 9. Việc cần làm trước production

1. MQTT TLS, password/ACL; Kafka SASL/TLS; tắt anonymous access.
2. Xác thực và RBAC cho direct control; audit operator identity.
3. Interlock vật lý, emergency stop và watchdog độc lập với cloud/UI.
4. Durable override TTL và policy rõ ràng khi operator/session mất kết nối.
5. Kafka tối thiểu 3 broker, replication factor 3 và Schema Registry.
6. Idempotent command handling trên device theo `command_id`.
7. Docker secrets/secret manager cho token và mật khẩu.
8. Định tuyến alert (Alertmanager tới Slack/SMS/on-call). Rule đã có trong
   `infra/prometheus/rules/aiot.yml` nhưng chưa có receiver.
