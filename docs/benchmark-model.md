# Độ chính xác mô hình TabM INT8 — phiên 24/10/2026

Tài liệu tách riêng khỏi [`benchmark-system.md`](benchmark-system.md), vốn chỉ tập trung vào hệ thống.

Script: `uv run --no-project --with numpy --with onnxruntime --with scipy python scripts/bench/model_quality.py`.

**Dữ liệu đánh giá:** `demo_data/demo_1hz.csv`, tức **20% cuối của bản ghi UCI 322**, không phải khối
test 60–80% của notebook. Có 8 360 cửa sổ đủ 60 s; 59 cửa sổ khởi động (đệm bằng mẫu đầu tiên) được
báo cáo riêng. Ngưỡng cố định, không chọn lại trên dữ liệu này:
- Có khí khi nồng độ thật đạt CO ≥ 50 ppm hoặc C₂H₄ ≥ 1 ppm (`PRESENT_PPM` của notebook).
- Mô hình báo có khí khi P ≥ τ = 0,48 cho từng khí.
- Cảnh báo: WARNING khi trung bình 5 s của P(CO) ≥ 0,48; DANGER khi trung bình 5 s của CO ≥ 200 ppm.

Script: [`scripts/bench/model_quality.py`](../scripts/bench/model_quality.py). Kết quả:
[`model/quality_metrics.json`](bench/system-2026-10-24/model/quality_metrics.json). Vết từng cửa sổ:
`model/prediction_trace.csv.gz`.

| Cấu hình | Khí | Precision | Recall | F1 | Dương / âm | RMSE (ppm) | MAE (ppm) | PICP90 | Độ rộng khoảng 90% (ppm) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| INT8-32, onnxruntime (tham chiếu) | CO | 0,8845 | 0,9041 | **0,8942** | 3 109 / 5 251 | **84,72** | 32,15 | 0,969 | 210,8 |
| | C₂H₄ | 0,9499 | 0,7942 | **0,8651** | 4 991 / 3 369 | **3,545** | 1,630 | 0,992 | 11,24 |
| INT8-32, nhân số nguyên (gateway, firmware) | CO / C₂H₄ | giống hệt tham chiếu ở mọi chỉ số | | | | | | | |
| INT8-4 (thành viên 0–3, cấu hình ESP32) | CO | 0,8854 | 0,9045 | 0,8948 | | 85,02 | 32,41 | 0,968 | 214,5 |
| | C₂H₄ | 0,9425 | 0,7842 | 0,8561 | | 3,648 | 1,725 | 0,993 | 11,42 |

- **PICP90:** tính trên mọi cửa sổ có khí thật (kể cả cửa sổ bị bỏ sót), từ khoảng Student-t 90% của
  đầu ra DER (2α bậc tự do). Độ bao phủ 0,97–0,99, cao hơn mức 90% danh nghĩa, đổi lại khoảng khá rộng
  (CO trung bình 211 ppm).
- **RMSE và MAE** tính trên nồng độ đầu ra cuối cùng của mọi cửa sổ, kể cả giá trị 0 khi mô hình báo
  không có khí.

**F1 macro bốn trạng thái (Q04–Q05)**: INT8-32 đạt **0,789**, INT8-4 đạt 0,781. Cả bốn trạng thái đều
có trong dữ liệu. F1 từng trạng thái: không khí 0,720 · chỉ CO 0,836 · chỉ C₂H₄ 0,740 · hỗn hợp 0,862.

Ma trận nhầm lẫn của INT8-32 (hàng: thật, cột: dự đoán):

| | không khí | chỉ CO | chỉ C₂H₄ | hỗn hợp |
|---|---:|---:|---:|---:|
| **không khí** (2 071) | 1 785 | 165 | 121 | 0 |
| **chỉ CO** (1 298) | 125 | 1 085 | 0 | 88 |
| **chỉ C₂H₄** (3 180) | **952** | 0 | 2 026 | 202 |
| **hỗn hợp** (1 811) | 27 | 48 | 146 | 1 590 |

Lỗi lớn nhất là bỏ sót C₂H₄ khi chỉ có C₂H₄ (952 cửa sổ bị dự đoán là không khí). Lỗi này khớp với
Recall C₂H₄ thấp (0,79).

**Cảnh báo trên dòng thời gian liên tục (Q10–Q13)**: 2,32 giờ, 17 đợt CO.

| Cấu hình / nhịp | Đợt phát hiện | Trễ tới WARNING (min / trung vị / max) | Trễ tới DANGER | Báo động giả: tín hiệu P(CO) (Q10) / mức vận hành (Q13) | Chuyển WARNING → DANGER |
|---|---:|---:|---:|---:|---:|
| INT8-32 · 1 s | 17/17 | 18 / 19 / 21 s | 19 / 20 / 22 s | 0 / 0 (0,0/h) | 16 |
| INT8-32 · 5 s | 17/17 | 17 / 20 / 22 s | 17 / 20 / 22 s | 0 / 0 | 3 |
| INT8-4 · 1 s | 17/17 | 18 / 19 / 21 s | 19 / 20 / 22 s | 0 / 0 | 16 |
| INT8-4 · 5 s (ESP32) | 17/17 | 17 / 20 / 22 s | 17 / 20 / 22 s | 0 / 0 | 5 |

- **Không có đợt nào đang báo động sẵn khi đợt bắt đầu.**
- **Cả 17 đợt đều có đỉnh ≥ 200 ppm** (200–533 ppm), nên mỗi đợt đều phải lên DANGER. Dữ liệu này
  **không có đợt CO 50–200 ppm**, vì vậy chưa kiểm được trường hợp chỉ cảnh báo WARNING.
- **Không có báo động giả trong 2,32 giờ.** Với 0 sự kiện, cận trên 95% (quy tắc "rule of three") là
  khoảng **1,3 lần/giờ**. Cần thời gian đánh giá dài hơn để kết luận chặt hơn.
- **Độ trễ khoảng 19–20 s** tính từ lúc nhãn bật, chưa phải từ lúc khí vật lý xuất hiện. Độ trễ gồm thời
  gian đáp ứng của cảm biến MOX và bước lấy trung bình 5 s.
- **Ở nhịp 5 s**, mỗi cửa sổ lọc chỉ có một kết quả, nên mức cảnh báo nhảy thẳng từ normal lên DANGER
  (ít bước chuyển WARNING → DANGER hơn).

**Cửa sổ khởi động** (59 cửa sổ đầu, đệm như gateway): quyết định có khí trùng với nhãn 100% cho cả hai
khí. Kết quả này là do khoảng đó không có khí trong dữ liệu, chưa phải phép thử khó.

**D02, giảm 32 → 4 thành viên** (INT8, cùng dữ liệu; phép trừ = INT8-4 − INT8-32):
- ΔF1: CO **+0,0006**; C₂H₄ **−0,0090**; F1 macro −0,0085.
- ΔRMSE: CO +0,29 ppm; C₂H₄ +0,10 ppm.
- ΔPICP90: CO −0,0006; C₂H₄ +0,0008.
- FA/h và độ trễ phát hiện không đổi.

**D01 (FP32 → INT8): không đo được.** Repo chỉ có mô hình INT8 (`TabM_seed42_int8_static.onnx`); cần
checkpoint FP32 từ phiên huấn luyện.
