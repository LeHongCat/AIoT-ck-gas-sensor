# Wokwi ESP32-S3 · replay + TabM INT8 on the device

The ESP32-S3 reads `demo_1hz.csv` (one row per second: `t_s, CO, Ethylene, S1..S16`, the last
20 % of UCI 322 that the benchmark kept out of training) from a simulated microSD card, publishes
the existing MQTT contracts and runs the **TabM INT8** model itself:

```text
loop() · core 1 · 1 Hz     CSV row ─► readings + ground-truth ─► 60 s window
inference task · core 0    newest full window ─► 144 features ─► TabM INT8 ─► predictions
```

| Step | Where |
|---|---|
| `(x − x_mean) / x_std`, 60 s window, 144 features (last, mean, std, min, max, slope, d5, d15, d30), standardise | `src/tabm_int8.cpp` `computeFeatures()` |
| TabM: 32 members × 4 blocks × 1024, INT8 weights + activations exactly as the QDQ ONNX graph | `Model::forward()`, weights in flash (`model/tabm_int8.bin`) |
| `P = sigmoid(logit)`, present when `P ≥ τ = 0.48`, ppm `= clip(γ, 0, 1.5) × y_max` (0 if absent), DER uncertainty | `decode()` |
| Alarm (CO only, as the notebook): `warning` when the mean P(CO) over the last 5 s of readings ≥ τ, `danger` when the mean CO ≥ 200 ppm; C₂H₄ is reported but raises no alarm | `AlarmFilter` |

Predictions keep the `observed_at` / `sequence` of the reading that ended their window. The first
reading after boot (or after the CSV wraps) is predicted at once: until 60 readings exist the
window is padded with the oldest one, exactly like the gateway (`tabm_model.pad_window`), and
`window_s` reports the real readings (1…60). There is no one-minute warm-up. Two constants in
`src/main.cpp` set the cost:

| Constant | Now | Meaning |
|---|---|---|
| `kInferEveryS` | 5 | the window goes to the model every 5 s |
| `kActiveMembers` | 4 | run the first 4 of the 32 ensemble members (3.3 M MACs each) |

Measured in Wokwi (simulated time, `esp_timer`): 4 members take p50 4.49 s / p95 6.78 s per
prediction and keep the 5 s cadence; 32 members take p50 37.7 s / p95 40.0 s. On the demo
data 4 members keep F1 CO 0.895 / C₂H₄ 0.856 (32: 0.894 / 0.865); predictions carry
`model_version` `seed42-k4`. If a pass misses the cadence the task takes the newest window, the
skipped one is reported on the serial monitor, and the alarm still averages by `sequence`
(1 Hz), never over minutes. The serial monitor prints `P(CO)`, `P(C2H4)`, ppm ± uncertainty and
the measured `inference_ms`.

The board is an ESP32-S3 with 16 MB flash: 3.99 MB of weights plus WiFi/MQTT do not fit in 4 MB.

## Prepare

1. Generate the model files (needs numpy, onnx, onnxruntime, e.g. the `aiot` conda env), from
   the repository root:

   ```powershell
   python tools/export_tabm_esp32.py
   ```

   It writes `model/tabm_int8.bin`, `src/tabm_model_params.h` and `host_test/golden.bin`, and
   stops if its integer kernels do not match onnxruntime.
2. Put `demo_data/demo_1hz.csv` (the whole 2.3 h recording, the single demo dataset) into `sdcard/`;
   the firmware reads `/demo_1hz.csv` (`kDatasetPath` in `src/main.cpp`). The simulation copies this
   folder onto the virtual microSD card at startup, so swapping the file never needs a rebuild.
3. Open this folder in VS Code with the PlatformIO and Wokwi extensions and build (`esp32s3`).
   The post-build step `merge_bin.py` writes `.pio/build/esp32s3/firmware_merged.bin`
   (bootloader + 16 MB partition table + app), which `wokwi.toml` loads.
4. Start the pipeline in ESP32 mode, from the repository root (see the main README):

   ```powershell
   ./scripts/demo.ps1 esp32
   ```

   It removes the simulator, pins the kitchen screen to `esp32-edge-01` and starts everything
   else. The gateway never predicts for this node (`EDGE_SKIP_NODES=esp32-edge-01` by default).
5. Start the simulation (`diagram.json` → *Wokwi: Start Simulator*). Wokwi for VS Code uses its
   built-in Private IoT Gateway, so the firmware reaches the host Mosquitto broker at
   `host.wokwi.internal:1883`.

The Public Wokwi Gateway cannot reach services on your computer. Mosquitto allows anonymous local
development connections; do not expose its port publicly.

## Check the C++ port on a PC

`host_test/host_test.cpp` runs `src/tabm_int8.cpp` on the golden windows and compares features,
raw outputs and presence decisions with numpy / onnxruntime. Any C++17 compiler works, e.g. zig
(`pip install ziglang`):

```powershell
python -m ziglang c++ -O2 -std=c++17 -Isrc host_test/host_test.cpp src/tabm_int8.cpp -o host_test/host_test.exe
host_test/host_test.exe model/tabm_int8.bin host_test/golden.bin
```

## MQTT contracts

- Publishes `aiot/v1/nodes/esp32-edge-01/readings` (`SensorReading`),
  `.../predictions` (`Prediction`, `model_name` `tabm-int8`, `model_version` `seed42-k4`) and
  `.../ground-truth` (`GroundTruth`: the dataset's CO / C₂H₄ columns, same `observed_at` and
  `sequence` as the reading). The model reads the 16 sensor columns only; the labels are a
  reference for the dashboards.
- Subscribes to `.../commands` and publishes retained `ActuatorState` acknowledgements to
  `.../actuator-state`. GPIO 2 drives the red alarm LED from the received `alarm_on` command.
- Every 30 s publishes `.../diagnostics` (free / minimum free heap, largest block, stack headroom
  of the inference task and the loop, skipped windows). The bridge does not forward it; it is read
  from the broker by `scripts/bench/wokwi_device.py`.
- The replay waits until SNTP has set the clock, so no reading carries a 1970 timestamp. The
  broker address is resolved once before SNTP starts (Arduino-ESP32 3.3 aborts when
  `hostByName()` runs while an SNTP lookup is pending).

Wiring (ESP32-S3): microSD SCK 12, MISO 13, MOSI 11, CS 10. Topics and payload fields match
`src/aiot_pipeline/topics.py` and `contracts.py`.
