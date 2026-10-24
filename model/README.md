# TabM INT8 edge model

Copied from the benchmark run `tinyml_benchmark_v8_new` (notebook
`aiot_gas_tinyml_benchmark_v8_new_ipynb.ipynb`, seed 42):

| File | Content |
|---|---|
| `TabM_seed42_int8_static.onnx` | TabM (k = 32, 4 × 1024), static INT8 QDQ, input `x [batch, 144]`, output `y [batch, 10]` |
| `preprocessing.json` | `x_mean` / `x_std` per channel, the 144 feature names, `feature_mean` / `feature_std`, `y_max` |
| `thresholds.json` | per model: presence threshold `tau` and the high-uncertainty cut `uct_cut_ppm` (key `TabM_seed42`) |

Benchmark (test block): F1 CO 0.880, F1 C₂H₄ 0.837, RMSE CO 95.1 ppm, 3.83 MB.

```powershell
python tools/tabm_reference.py       # reference pipeline on demo_data/demo_1hz.csv
python tools/export_tabm_esp32.py    # weights + header for the ESP32 firmware
```

Both need numpy, onnx and onnxruntime (the `aiot` conda env). After retraining, copy the three
files again and rerun the export.
