"""D08 on the host: one window through the integer TabM kernels (features + forward), 1 BLAS thread.

500 windows spread evenly over demo_data/demo_1hz.csv, 20 warm-up windows, members 32 and 4.

    uv run --no-project --with numpy python scripts/bench/inference_timing.py
"""

import os

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

import json  # noqa: E402
import statistics  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from aiot_pipeline.tabm_model import TabMEngine  # noqa: E402

data = np.loadtxt(ROOT / "demo_data/demo_1hz.csv", delimiter=",", skiprows=1, dtype=np.float32)[
    :, 3:19
]
ends = np.linspace(59, len(data) - 1, 500).astype(int)
result = {"windows": len(ends), "blas_threads": 1}
for members in (32, 4):
    engine = TabMEngine(members=members)
    for i in ends[:20]:
        engine.predict(data[i - 59 : i + 1])
    times = []
    for i in ends:
        started = time.perf_counter()
        engine.predict(data[i - 59 : i + 1])
        times.append(time.perf_counter() - started)
    q = statistics.quantiles(times, n=100, method="inclusive")
    result[f"members_{members}"] = {
        "mean_ms": round(statistics.fmean(times) * 1000, 3),
        "p50_ms": round(q[49] * 1000, 3),
        "p95_ms": round(q[94] * 1000, 3),
        "max_ms": round(max(times) * 1000, 3),
        "windows_per_s": round(1 / statistics.fmean(times), 1),
    }
out = ROOT / "docs/bench/system-2026-10-24/model/inference_timing.json"
out.write_text(json.dumps(result, indent=1), encoding="utf-8")
print(json.dumps(result))
