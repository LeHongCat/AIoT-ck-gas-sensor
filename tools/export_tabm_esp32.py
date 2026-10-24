"""Export TabM INT8 (ONNX, QDQ) for the ESP32 firmware in wokwi/esp32-replay.

Writes, next to the firmware:
    model/tabm_int8.bin        all INT8 tensors, laid out for the C++ kernels (embedded in flash)
    src/tabm_model_params.h    quantisation scales / zero points, blob offsets, pre-processing
    host_test/golden.bin       sample windows with the onnxruntime features and outputs, plus the
                               integer outputs of the first --subset-members members

The graph is a TabM ensemble (k = 32 members sharing one weight matrix per layer):

    x (144) ─► 4 × [ ·r_m ─► @W ─► ·s_m ─► +b_m ─► ReLU ] ─► per-member head (1024 → 10) ─► mean

Every tensor is per-tensor INT8; ReLU is folded into the zero point (-128) of each block output.
Before writing anything the script re-runs the graph with the integer algorithm of the C++ port
and checks it against onnxruntime.

    python tools/export_tabm_esp32.py            # needs numpy, onnx, onnxruntime
"""

import argparse
import json
import struct
from pathlib import Path

import numpy as np
import onnx
from onnx import numpy_helper
from tabm_reference import MODEL_DIR, ROOT, WINDOW_S, load_bundle, load_csv, model_inputs, run_onnx

FIRMWARE_DIR = ROOT / "wokwi" / "esp32-replay" / "esp32-replay"
MEMBERS, WIDTH, FEATURES, OUTPUTS, BLOCKS = 32, 1024, 144, 10, 4
GOLDEN_MAGIC = b"TABMGLD2"


class Graph:
    """Name lookups in the QDQ graph: tensor -> (int8 values, scale, zero point)."""

    def __init__(self, path: Path) -> None:
        self.model = onnx.load(str(path))
        self.init = {t.name: numpy_helper.to_array(t) for t in self.model.graph.initializer}
        self.producer = {out: node for node in self.model.graph.node for out in node.output}
        ops = {node.op_type for node in self.model.graph.node}
        assert "Relu" not in ops, "expected ReLU folded into the quantisation of each block"

    def qparam(self, tensor: str) -> tuple[float, int]:
        return float(self.init[f"{tensor}_scale"]), int(self.init[f"{tensor}_zero_point"])

    def weight(self, dq_output: str) -> tuple[np.ndarray, float, int]:
        node = self.producer[dq_output]
        assert node.op_type == "DequantizeLinear"
        q, scale, zp = (self.init[name] for name in node.input)
        return q, float(scale), int(zp)

    def matmul_weight(self, matmul_output: str) -> tuple[np.ndarray, float, int]:
        node = self.producer[matmul_output]
        assert node.op_type == "MatMul"
        return self.weight(node.input[1])


def read_graph(path: Path) -> dict:
    g = Graph(path)
    params = {"input": g.qparam("/net/Flatten_1_output_0")}
    # Concat / Unsqueeze / Expand re-quantise with the same parameters: an identity.
    for tensor in ("/net/Concat_output_0", "/net/ensemble_view/Expand_output_0"):
        assert g.qparam(tensor) == params["input"], tensor

    blocks, din = [], FEATURES
    for b in range(BLOCKS):
        base = f"/net/backbone/blocks.{b}/blocks.{b}.0"
        r = g.weight(f"net.backbone.blocks.{b}.0.r_DequantizeLinear_Output")
        w = g.matmul_weight(f"{base}/MatMul_output_0")
        s = g.weight(f"net.backbone.blocks.{b}.0.s_DequantizeLinear_Output")
        bias = g.weight(f"net.backbone.blocks.{b}.0.bias_DequantizeLinear_Output")
        assert r[0].shape == (MEMBERS, din) and w[0].shape == (din, WIDTH)
        assert s[0].shape == bias[0].shape == (MEMBERS, WIDTH) and w[2] == 0
        out = g.qparam(f"/net/backbone/blocks.{b}/blocks.{b}.1/Relu_output_0")
        assert out[1] == -128, "block output must clamp at 0 (folded ReLU)"
        blocks.append(
            {
                "din": din,
                "r": r,
                "w": w,
                "s": s,
                "bias": bias,
                "out": out,
                "mul": g.qparam(f"{base}/Mul_output_0"),
                "mm": g.qparam(f"{base}/MatMul_output_0"),
                "mul1": g.qparam(f"{base}/Mul_1_output_0"),
            }
        )
        din = WIDTH
    params["blocks"] = blocks

    # Head: each Transpose re-quantises with the parameters of its input (identity), then a
    # per-member matmul.
    for node in g.model.graph.node:
        if node.op_type == "QuantizeLinear" and g.producer[node.input[0]].op_type == "Transpose":
            source = g.producer[g.producer[node.input[0]].input[0]]  # the DequantizeLinear before
            assert list(node.input[1:]) == list(source.input[1:]), node.name
    hw = g.weight("net.output.weight_DequantizeLinear_Output")
    hb = g.weight("net.output.bias_DequantizeLinear_Output")
    assert (
        hw[0].shape == (MEMBERS, WIDTH, OUTPUTS)
        and hb[0].shape == (MEMBERS, OUTPUTS)
        and hw[2] == 0
    )
    params["head"] = {
        "w": hw,
        "bias": hb,
        "mm": g.qparam("/net/output/MatMul_output_0"),
        "add": g.qparam("/net/output/Add_output_0"),
    }
    return params


# ---------------- integer forward pass: the exact algorithm of src/tabm_int8.cpp ----------------
def quant(x: np.ndarray, qp: tuple[float, int]) -> np.ndarray:
    scale, zp = np.float32(qp[0]), qp[1]
    return np.clip(np.rint(x.astype(np.float32) / scale) + zp, -128, 127).astype(np.int8)


def dequant(q: np.ndarray, qp: tuple[float, int]) -> np.ndarray:
    return (q.astype(np.float32) - np.float32(qp[1])) * np.float32(qp[0])


def int8_forward(params: dict, z: np.ndarray) -> np.ndarray:
    """(n, 144) standardised features -> (n, 10) raw outputs."""
    act = quant(z, params["input"])[:, None, :]  # (n, 1, 144), shared by the members
    act_qp = params["input"]
    for blk in params["blocks"]:
        r = blk["r"]
        u = quant(dequant(act, act_qp) * dequant(r[0], r[1:]), blk["mul"])  # (n, 32, din)
        w = blk["w"]
        acc = np.einsum("nmi,ij->nmj", u.astype(np.int32), w[0].astype(np.int32))
        acc -= blk["mul"][1] * w[0].astype(np.int32).sum(0)
        v = quant(
            acc.astype(np.float32) * (np.float32(blk["mul"][0]) * np.float32(w[1])), blk["mm"]
        )
        s, bias = blk["s"], blk["bias"]
        wq = quant(dequant(v, blk["mm"]) * dequant(s[0], s[1:]), blk["mul1"])
        act = quant(dequant(wq, blk["mul1"]) + dequant(bias[0], bias[1:]), blk["out"])
        act_qp = blk["out"]
    head = params["head"]
    hw, hb = head["w"], head["bias"]
    acc = np.einsum("nmi,mik->nmk", act.astype(np.int32), hw[0].astype(np.int32))
    acc -= act_qp[1] * hw[0].astype(np.int32).sum(1)
    v = quant(acc.astype(np.float32) * (np.float32(act_qp[0]) * np.float32(hw[1])), head["mm"])
    o = quant(dequant(v, head["mm"]) + dequant(hb[0], hb[1:]), head["add"])
    return dequant(o, head["add"]).mean(1)


def first_members(params: dict, k: int) -> dict:
    """The same graph with only the first k ensemble members (W is shared, so it stays whole)."""
    blocks = [
        {**blk, **{key: (blk[key][0][:k], *blk[key][1:]) for key in ("r", "s", "bias")}}
        for blk in params["blocks"]
    ]
    head = {**params["head"]}
    for key in ("w", "bias"):
        head[key] = (head[key][0][:k], *head[key][1:])
    return {**params, "blocks": blocks, "head": head}


# ---------------- writers ----------------
def build_blob(params: dict) -> tuple[bytes, dict]:
    """Weights are stored output-major (W transposed): every dot product reads contiguous bytes."""
    parts, offsets, size = [], {}, 0

    def add(key: str, array: np.ndarray) -> None:
        nonlocal size
        data = np.ascontiguousarray(array, dtype=np.int8).tobytes()
        offsets[key] = size
        parts.append(data)
        size += len(data)

    for b, blk in enumerate(params["blocks"]):
        add(f"r{b}", blk["r"][0])
        add(f"w{b}", blk["w"][0].T)  # (1024, din)
        add(f"s{b}", blk["s"][0])
        add(f"bias{b}", blk["bias"][0])
    add("head_w", params["head"]["w"][0].transpose(0, 2, 1))  # (32, 10, 1024)
    add("head_bias", params["head"]["bias"][0])
    return b"".join(parts), offsets


def c_float(value: float) -> str:
    text = f"{float(np.float32(value)):.9g}"
    return (text if any(c in text for c in ".en") else text + ".0") + "f"


def c_floats(values) -> str:
    items = [c_float(v) for v in values]
    lines = [", ".join(items[i : i + 6]) for i in range(0, len(items), 6)]
    return "{\n    " + ",\n    ".join(lines) + ",\n}"


def c_qparam(qp: tuple[float, int]) -> str:
    return f"{{{c_float(qp[0])}, {qp[1]}}}"


def write_descriptor(
    path: Path, params: dict, offsets: dict, blob: bytes, bundle, onnx_name: str
) -> None:
    """The same layout as tabm_model_params.h, but readable by the gateway engine.

    The container has no onnx and no C header parser: this JSON plus model/tabm_int8.bin are
    enough to reproduce the firmware's integer kernels exactly (src/aiot_pipeline/tabm_model.py).
    """

    def qparam(qp: tuple[float, int]) -> dict[str, float | int]:
        return {"scale": float(qp[0]), "zero_point": int(qp[1])}

    payload = {
        "model_name": "tabm-int8",
        "model_version": "seed42",
        "source": onnx_name,
        "blob": "tabm_int8.bin",
        "blob_size": len(blob),
        "members": MEMBERS,
        "width": WIDTH,
        "features": FEATURES,
        "outputs": OUTPUTS,
        "blocks": BLOCKS,
        "window_s": WINDOW_S,
        "sensors": 16,
        "gases": 2,
        # decode() and AlarmFilter constants of src/tabm_int8.cpp
        "gamma_max": 1.5,
        "alarm_smooth_s": 5,
        "co_danger_ppm": 200.0,
        "infer_every_s": 5,
        "input": qparam(params["input"]),
        "block_params": [
            {
                "din": blk["din"],
                "r_offset": offsets[f"r{b}"],
                "r": qparam(blk["r"][1:]),
                "mul": qparam(blk["mul"]),
                "w_offset": offsets[f"w{b}"],
                "w_scale": float(blk["w"][1]),
                "w_zero_point": int(blk["w"][2]),
                "matmul": qparam(blk["mm"]),
                "s_offset": offsets[f"s{b}"],
                "s": qparam(blk["s"][1:]),
                "mul1": qparam(blk["mul1"]),
                "bias_offset": offsets[f"bias{b}"],
                "bias": qparam(blk["bias"][1:]),
                "out": qparam(blk["out"]),
            }
            for b, blk in enumerate(params["blocks"])
        ],
        "head": {
            "w_offset": offsets["head_w"],
            "w_scale": float(params["head"]["w"][1]),
            "w_zero_point": int(params["head"]["w"][2]),
            "matmul": qparam(params["head"]["mm"]),
            "bias_offset": offsets["head_bias"],
            "bias": qparam(params["head"]["bias"][1:]),
            "add": qparam(params["head"]["add"]),
        },
        "x_mean": [float(value) for value in bundle.x_mean],
        "x_std": [float(value) for value in bundle.x_std],
        "feature_mean": [float(value) for value in bundle.feature_mean],
        "feature_std": [float(value) for value in bundle.feature_std],
        "y_max": [float(value) for value in bundle.y_max],
        "tau": [float(value) for value in bundle.tau],
        "uct_cut": [float(value) for value in bundle.uct_cut],
    }
    path.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")


def write_header(
    path: Path, params: dict, offsets: dict, blob_size: int, bundle, onnx_name: str
) -> None:
    blocks = []
    for b, blk in enumerate(params["blocks"]):
        rows = [
            [str(blk["din"]), f"{offsets[f'r{b}']}u", c_qparam(blk["r"][1:]), c_qparam(blk["mul"])],
            [f"{offsets[f'w{b}']}u", c_float(blk["w"][1]), c_qparam(blk["mm"])],
            [f"{offsets[f's{b}']}u", c_qparam(blk["s"][1:]), c_qparam(blk["mul1"])],
            [f"{offsets[f'bias{b}']}u", c_qparam(blk["bias"][1:]), c_qparam(blk["out"])],
        ]
        blocks.append("    {" + ",\n     ".join(", ".join(row) for row in rows) + "},")
    head = params["head"]
    head_row = (
        f"{offsets['head_w']}u, {c_float(head['w'][1])}, {c_qparam(head['mm'])},\n"
        f"                              {offsets['head_bias']}u, {c_qparam(head['bias'][1:])}, "
        f"{c_qparam(head['add'])}"
    )
    text = f"""// Generated by tools/export_tabm_esp32.py from {onnx_name}. Do not edit.
#pragma once

#include <stddef.h>
#include <stdint.h>

namespace tabm {{

struct QParam {{
  float scale;
  int8_t zero_point;
}};

// One TabM block: u = Q(act * r_m) -> v = Q(u @ W) -> Q(v * s_m) -> out = Q(. + bias_m).
// ReLU is the saturation at the output zero point (-128).
struct BlockParams {{
  int din;
  size_t r_offset;     // int8 [32][din]
  QParam r, mul;
  size_t w_offset;     // int8 [1024][din], W transposed; zero point 0
  float w_scale;
  QParam matmul;
  size_t s_offset;     // int8 [32][1024]
  QParam s, mul1;
  size_t bias_offset;  // int8 [32][1024]
  QParam bias, out;
}};

struct HeadParams {{
  size_t w_offset;     // int8 [32][10][1024]; zero point 0
  float w_scale;
  QParam matmul;
  size_t bias_offset;  // int8 [32][10]
  QParam bias, add;
}};

constexpr char kModelName[] = "tabm-int8";
constexpr char kModelVersion[] = "seed42";
constexpr size_t kBlobSize = {blob_size}u;
constexpr int kMembers = {MEMBERS};
constexpr int kWidth = {WIDTH};
constexpr int kFeatures = {FEATURES};
constexpr int kOutputs = {OUTPUTS};
constexpr int kBlocks = {BLOCKS};
constexpr int kWindow = {WINDOW_S};
constexpr int kSensors = 16;
constexpr int kGases = 2;  // CO, Ethylene

constexpr QParam kInput = {c_qparam(params["input"])};

constexpr BlockParams kBlockParams[kBlocks] = {{
{chr(10).join(blocks)}
}};

constexpr HeadParams kHead = {{{head_row}}};

// preprocessing.json: per-channel normalisation and feature standardisation.
constexpr float kXMean[kSensors] = {c_floats(bundle.x_mean)};
constexpr float kXStd[kSensors] = {c_floats(bundle.x_std)};
constexpr float kFeatureMean[kFeatures] = {c_floats(bundle.feature_mean)};
constexpr float kFeatureStd[kFeatures] = {c_floats(bundle.feature_std)};
constexpr float kYMax[kGases] = {c_floats(bundle.y_max)};

// thresholds.json: presence threshold tau and the "high uncertainty" cut (ppm).
constexpr float kTau[kGases] = {c_floats(bundle.tau)};
constexpr float kUctCut[kGases] = {c_floats(bundle.uct_cut)};

}}  // namespace tabm
"""
    path.write_text(text, encoding="utf-8", newline="\n")


def write_golden(
    path: Path, windows: np.ndarray, z: np.ndarray, raw: np.ndarray, subset: int, raw_subset
) -> None:
    """Little-endian: magic, n, subset members, then per window: window, z, raw, raw_subset."""
    with path.open("wb") as f:
        f.write(GOLDEN_MAGIC + struct.pack("<II", len(windows), subset))
        for k in range(len(windows)):
            for array in (windows[k], z[k], raw[k], raw_subset[k]):
                f.write(array.astype("<f4").tobytes())


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--model-dir", type=Path, default=MODEL_DIR)
    parser.add_argument("--csv", type=Path, default=ROOT / "demo_data" / "demo_1hz.csv")
    parser.add_argument("--firmware", type=Path, default=FIRMWARE_DIR)
    parser.add_argument("--golden", type=int, default=96, help="windows in host_test/golden.bin")
    parser.add_argument(
        "--subset-members", type=int, default=4, help="members of the reduced golden outputs"
    )
    args = parser.parse_args()

    bundle = load_bundle(args.model_dir)
    params = read_graph(bundle.onnx_path)

    _, _, sensors = load_csv(args.csv)
    end_idx, z = model_inputs(sensors, bundle)
    ort_raw = run_onnx(z, bundle.onnx_path)
    mine = np.concatenate([int8_forward(params, z[i : i + 256]) for i in range(0, len(z), 256)])
    diff = np.abs(mine - ort_raw)
    agree = np.mean((mine[:, :2] >= 0) == (ort_raw[:, :2] >= 0))
    print(
        f"Integer kernels vs onnxruntime on {len(z)} windows: max |Δ| {diff.max():.4f}, "
        f"mean |Δ| {diff.mean():.6f}, logit-sign agreement {agree:.4%}"
    )
    assert diff.max() < 0.25 and agree > 0.999, "integer port does not match onnxruntime"

    blob, offsets = build_blob(params)
    (args.firmware / "model").mkdir(parents=True, exist_ok=True)
    (args.firmware / "model" / "tabm_int8.bin").write_bytes(blob)
    write_header(
        args.firmware / "src" / "tabm_model_params.h",
        params,
        offsets,
        len(blob),
        bundle,
        bundle.onnx_path.name,
    )
    # The same blob + layout for the gateway engine (src/aiot_pipeline/tabm_model.py). It sits
    # next to the blob so the container can mount one directory.
    write_descriptor(
        args.firmware / "model" / "tabm_int8.json",
        params,
        offsets,
        blob,
        bundle,
        bundle.onnx_path.name,
    )

    pick = np.linspace(0, len(z) - 1, args.golden).astype(int)
    windows = np.stack([sensors[end_idx[k] - WINDOW_S + 1 : end_idx[k] + 1] for k in pick])
    raw_subset = int8_forward(first_members(params, args.subset_members), z[pick])
    (args.firmware / "host_test").mkdir(parents=True, exist_ok=True)
    write_golden(
        args.firmware / "host_test" / "golden.bin",
        windows,
        z[pick],
        ort_raw[pick],
        args.subset_members,
        raw_subset,
    )
    print(
        f"Wrote model/tabm_int8.bin ({len(blob) / 1e6:.2f} MB), src/tabm_model_params.h, "
        f"host_test/golden.bin ({len(pick)} windows, {args.subset_members}-member outputs) "
        f"under {args.firmware}"
    )


if __name__ == "__main__":
    main()
