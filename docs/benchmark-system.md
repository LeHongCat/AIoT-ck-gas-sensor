# Benchmark hệ thống AIoT — phiên 24/10/2026

Phạm vi:
- **Hệ thống:** đường dữ liệu S01–S17, nhịp và tài nguyên gateway D08–D12, chính sách điều khiển
  C01–C11, phục hồi R01–R07.
- **Triển khai mô hình** (mục 6): D03–D08, tức độ khớp ONNX–C++, dung lượng, Flash/RAM firmware và tốc
  độ suy luận.

Độ chính xác của mô hình (Q01–Q13, D01–D02) không thuộc phạm vi báo cáo này; kết quả đã đo được để
riêng trong [`benchmark-model.md`](benchmark-model.md).

Dữ liệu gốc của mọi bảng nằm trong [`docs/bench/system-2026-10-24/`](bench/system-2026-10-24/). Mỗi
công cụ ghi một `run_manifest_*.json` gồm commit, các tệp chưa commit, SHA-256 của mô hình và dataset,
thông tin máy, giới hạn CPU/RAM và biến môi trường của từng container, cùng độ lệch đồng hồ.
[`tables.md`](bench/system-2026-10-24/tables.md) là toàn bộ bảng do `scripts/bench/report.py` sinh từ
các tệp JSON.

## 1. Môi trường và cách đo

| Mục | Giá trị |
|---|---|
| Máy | Windows 11, Docker Desktop 29.8 (WSL2, kernel 6.18), 20 CPU, 15,3 GiB cho Docker |
| Mã nguồn | commit `fb739ea`, cộng bản sửa No Local của `4308b9b` (chưa commit; xem `code.uncommitted_files` trong manifest) |
| Cấu hình | `compose.yaml` của repo, giữ nguyên mọi giới hạn: `edge-inference` 0,5 CPU / 192 MiB, Bridge 0,5 CPU, Kafka 1 CPU / 768 MiB… |
| Mô hình gateway | TabM INT8, 32 thành viên, suy luận mỗi 1 s (mặc định); biến thể 4 thành viên/1 s và 32 thành viên/5 s |
| Dữ liệu | `demo_data/demo_1hz.csv` (SHA-256 trong manifest), mỗi nút bắt đầu ở một dòng khác nhau |
| Đồng hồ | Bộ đo chạy trên host và đóng dấu `observed_at` bằng đồng hồ host. Các histogram do container xuất ra lấy đồng hồ container trừ `observed_at`. Độ lệch host–container đo bằng `time()` của Prometheus qua HTTP: **+0,4 ms ± 2,8 ms**, nên bỏ qua được. |
| Phân vị | Lấy từ mẫu thô (`statistics.quantiles`). Thử tải dùng `method="exclusive"`; các phép đo khác dùng `inclusive`. Với hàng nghìn mẫu, hai cách chênh nhau không đáng kể. Phân vị lấy từ histogram Prometheus (InfluxDB, Controller, lệnh→ACK) là **ước lượng theo bucket**. |

Tiêu chí đạt được ghi vào [`acceptance_criteria.json`](bench/system-2026-10-24/acceptance_criteria.json)
**trước** các lượt đo sạch. Đây là đề xuất cho nguyên mẫu, không phải tiêu chuẩn an toàn được chứng
nhận.

## 2. Phát hiện chính

1. **Số series Prometheus tăng không giới hạn theo `node_id`.** Các metric gắn nhãn `node_id` không bao
   giờ xóa node cũ. Lượt thử tải đầu tiên chạy 3 lượt liên tiếp không khởi động lại dịch vụ, tổng cộng
   693 node khác nhau. Kết quả:
   - `/metrics` của Bridge lên **29 107 series (2,5 MiB), mất 2,3 s để dựng mỗi lần scrape** (5 s một lần).
   - CPU trung bình của Bridge ở 100 nút tăng qua các lượt: 0,26 → 0,33 → 0,44 lõi (giới hạn 0,5).
   - Kafka p95 tăng: 0,72 → 1,73 → 3,82 s. Lưu InfluxDB p95: 1,24 → 2,08 → 4,46 s.
   - Lượt 3 chỉ lưu được 98,8% reading.

   Ở trạng thái sạch, Bridge xuất khoảng **44 series cho mỗi nút** (4 448 series ở 100 nút). Mọi số liệu
   thử tải bên dưới đều khởi động lại 4 dịch vụ Python trước mỗi bậc (`--reset-services`). Lượt tích lũy
   được giữ làm bằng chứng: `load_default-cumulative.json`.
2. **Gateway là nút cổ chai.** Với 0,5 CPU, gateway đạt tối đa khoảng 46 dự đoán/s ở 32 thành viên. Theo
   tiêu chí đã chốt:
   - S17 đạt đến **20 nút**. Từ 25 nút, dữ liệu vẫn lưu đủ 100% nhưng gateway p95 vượt ngưỡng 1 s (25 nút: 0,85–1,21 s; 40 nút: 1,37 s).
   - D11 (≤ 1% khoảng cách vượt 1,5 chu kỳ) chỉ đạt đến **10 nút**.
3. **Bridge ghép ACK chỉ theo `command_id` (C09).** Một ACK phát dưới `node_id` khác vẫn được tính là
   ACK của nút nhận lệnh, nên không sinh cảnh báo quá hạn. Theo yêu cầu C09 ("không tự xác nhận lệnh
   chưa có ACK hợp lệ"), trường hợp này **không đạt**. Giao diện không bị ảnh hưởng vì nó cập nhật theo
   topic của từng nút.
4. **Biên τ của bộ lọc cảnh báo (C01a).** Với P(CO) đúng bằng τ = 0,48, trung bình 5 giá trị bằng
   τ − 5,6·10⁻¹⁷, nên kết quả là `normal` thay vì `warning`. Phép so sánh float32 trong firmware cho cùng
   kết quả. Ảnh hưởng thực tế gần như bằng 0, nhưng quy tắc "≥ τ" không được bảo đảm tại đúng biên.
5. **Mất dữ liệu khi gián đoạn:**
   - Dừng Bridge 20 s làm **mất 24 s** ở mọi luồng, vì Bridge dùng phiên MQTT sạch.
   - Dừng Mosquitto 20 s làm mất 27 s, vì nguồn phát (bộ đo, giống simulator) không có hàng đợi lưu tạm.
   - Dừng `edge-inference` 60 s làm thiếu 323/480 dự đoán, vì không có cơ chế bù.
   - Ngược lại, dừng Influx Sink hoặc Kafka **không mất bản ghi nào**.
6. **Một batch bị InfluxDB từ chối làm dừng toàn bộ đường lưu trữ (đã sửa).** Lúc khởi động trên Wokwi,
   ESP32 publish vài chục reading và ground truth trước khi SNTP đặt đồng hồ, nên `observed_at` là năm
   1970. InfluxDB từ chối batch chứa chúng (HTTP 422, ngoài retention 30 ngày). Influx Sink ném lỗi,
   container khởi động lại, đọc lại đúng batch đó từ offset chưa commit, rồi lỗi tiếp: **52 lần khởi
   động lại**, không ghi thêm điểm nào, lag tăng lên 162 517 bản ghi. Hai lần đo trùng thời điểm này
   không hợp lệ và được giữ làm bằng chứng (`*-sink-crashloop.json`). Đã sửa ở hai phía:
   - Sink coi HTTP 400/422 là lỗi do dữ liệu: ghi log, tăng `aiot_influx_write_failures_total` rồi
     commit qua batch đó. Lỗi khi InfluxDB không sẵn sàng vẫn được thử lại như cũ (có test cho cả hai).
     Sau khi sửa, sink xả hết tồn đọng (134 053 điểm trong 31 s) và lag về 0.
   - Firmware chờ đồng hồ hợp lệ rồi mới phát reading; replay tiếp tục đúng chỗ, không mất bản ghi.
7. **Các con số không nên diễn giải theo nghĩa đen:**
   - Lag của nhóm `kitchen-demo-ui-v1` luôn vài trăm đến hơn một nghìn bản ghi vì UI đọc từ cuối phân
     vùng và không commit offset. Con số này không phản ánh UI chậm.
   - "Tuổi mẫu tại Controller" bị chặn ở bucket lớn nhất 10 s, nên ghi "> 10 s" khi bão hòa.

## 3. Đường dữ liệu (S01–S17, D10–D12)

Thử tải: N nút ảo phát reading + ground truth 1 Hz, cùng lúc mỗi giây; 30 s làm nóng + 120 s đo;
**3 lượt độc lập** cho mỗi mức. Bảng ghi trung vị của 3 lượt. Khoảng min–max nằm trong
[`tables.md`](bench/system-2026-10-24/tables.md).

| Nút | Sự kiện vào/s | Dự đoán/s | Gateway p50 / p95 | Kafka p95 (reading) | Lưu InfluxDB p95 reading / dự đoán | Lưu reading / dự đoán | Mosquitto bỏ | CPU edge (lõi) | Tổng CPU (lõi) / RAM (MiB) |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 2 | 1,0 | 14 / 18 ms | 29 ms | 0,48 / 0,73 s | 100 / 100 % | 0 | 0,04 | 0,47 / 1 564 |
| 10 | 20 | 10,0 | 91 / 300 ms | 33 ms | 0,74 / 0,84 s | 100 / 100 % | 0 | 0,14 | 0,68 / 1 584 |
| 20 | 40 | 20,0 | 309 / 581 ms | 68 ms | 0,72 / 1,06 s | 100 / 100 % | 0 | 0,23 | 0,93 / 1 584 |
| 40 | 80 | 40,0 | 558 / 1 369 ms | 103 ms | 0,73 / 1,85 s | 100 / 100 % | 0 | 0,44 | 1,27 / 1 591 |
| 60 | 120 | 46,7 | 20,4 / 23,0 s | 232 ms | 0,75 / 28,9 s | 100 / 78,1 % | 959 | 0,50 | 1,54 / 1 614 |
| 100 | 200 | 46,1 | 22,0 / 23,5 s | 462 ms | 1,12 / 29,0 s | 100 / 41,1 % | 7 090 | 0,50 | 1,62 / 1 634 |

Ở mọi mức và mọi lượt: Kafka nhận đủ 100% reading và ground truth, với **0 bản ghi trùng, 0 bản ghi
sai thứ tự**; 0 bản ghi DLQ, 0 ACK quá hạn, 0 lỗi ghi InfluxDB. Phần dự đoán thiếu ở 60–100 nút đều
nằm ở bước tạo dự đoán và hàng đợi của Mosquitto: Kafka và InfluxDB lưu đủ những dự đoán đã tới.

| Nút | Khoảng cách hai dự đoán p50 / p95 (D10) | > 1,5 chu kỳ (D11) | Ngừng dài nhất | Tuổi mẫu tại Controller p95 (S08) | Lệnh → ACK p95 (S10) | Lag đỉnh Sink / Controller (S13) |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1,00 / 1,00 s | 0 % | 1,0 s | 0,07 s | 23,5 ms | 2 / 0 |
| 10 | 1,00 / 1,12 s | 0 % | 1,4 s | 0,35 s | 9,0 ms | 29 / 0 |
| 20 | 1,00 / 1,20 s | 1,3 % (0–2,5) | 1,6 s | 0,63 s | 9,1 ms | 42 / 0 |
| 40 | 1,00 / 1,49 s | 4,9 % | 2,2 s | 1,66 s | 35,1 ms | 119 / 8 |
| 60 | 1,19 / 2,02 s | 11,4 % | 21,8 s | > 10 s | 80,8 ms | 161 / 10 |
| 100 | 1,00 / 2,16 s | 20,5 % | 109,8 s | > 10 s | 160 ms | 192 / 13 |

**Biến thể gateway ở 100 nút**, mỗi biến thể 3 lượt, cùng giới hạn 0,5 CPU:

| Cấu hình | Dự đoán/s | Gateway p50 / p95 | Dự đoán lưu / lịch | Mosquitto bỏ | > 1,5 chu kỳ | CPU edge | RAM edge TB / đỉnh |
|---|---:|---:|---:|---:|---:|---:|---:|
| 32 thành viên, 1 s (mặc định) | 46,1 | 22,0 / 23,5 s | 41,1 % | 7 090 | 20,5 % | 0,50 | 57 / 69 MiB |
| 4 thành viên, 1 s | 87,5 | 10,4 / 12,3 s | 91,9 % | 792 | 11,4 % | 0,50 | — / 68 MiB |
| 32 thành viên, 5 s | 20,0 | 1,07 / 2,24 s | 100 % | 0 (một lượt 3 674*) | 0 % | 0,28 | — / 69 MiB |

\* Lượt đầu tiên ngay sau khi tạo lại container `edge-inference` ghi nhận 3 674 bản tin Mosquitto bị bỏ
trong khi dự đoán vẫn lưu đủ 100%: các bản tin bị bỏ thuộc hàng đợi của một client khác. Hai lượt sau
bằng 0.

Mỗi lượt suy luận trong container mất trung bình 9,7 ms khi có 1 nút và 19,2 ms khi có 100 nút (32
thành viên); với 4 thành viên là khoảng 9,2 ms. Trên máy này, giảm từ 32 xuống 4 thành viên tăng thông
lượng khoảng 1,9 lần.

**S17 theo tiêu chí đã chốt** (mọi điều kiện đạt trong cả 3 lượt):

| Cấu hình | Mức đạt lớn nhất | Lý do trượt ở mức kế tiếp |
|---|---|---|
| 32 thành viên, 1 s | **20 nút** | 25 nút: gateway p95 0,85 / 1,01 / 1,21 s, hai trong ba lượt vượt 1 s; các tiêu chí khác vẫn đạt |
| 4 thành viên, 1 s | không đạt ở 100 nút | lưu dự đoán 91%, broker bỏ bản tin, p95 12 s |
| 32 thành viên, 5 s | không đạt ở 100 nút | gateway p95 2,2 s > 1 s. Ngưỡng 1 s đặt cho nhịp 1 s; với ngưỡng "≤ một chu kỳ 5 s" thì cấu hình này đạt, nhưng ngưỡng đó đặt **sau** khi có số liệu. |

**Các mức gần điểm bão hòa** (32 thành viên, 1 s, phát đồng loạt, 3 lượt mỗi mức):

| Nút | Gateway p50 | Gateway p95 (3 lượt) | Lưu dự đoán | Mosquitto bỏ | > 1,5 chu kỳ | CPU edge | Lưu InfluxDB p95 |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 25 | 456–510 ms | 0,85 / 1,01 / 1,21 s | 100 % | 0 | 2,4–3,7 % | 0,33–0,37 | 0,90–0,94 s |
| 30 | 514–545 ms | 1,26 / 1,32 / 1,73 s | 100 % | 0 | 2,8–5,5 % | 0,39–0,41 | 0,84–0,86 s |
| 35 | 667–908 ms | 1,71 / 2,03 / 4,79 s | 100 % | 0 | 5,8–9,0 % | 0,44–0,45 | 0,91–0,95 s |

Từ 25 đến 40 nút, dữ liệu vẫn lưu đủ và không có bản tin bị bỏ. Cái tăng là độ trễ xếp hàng ở gateway:
p95 vượt một chu kỳ 1 s từ khoảng 25 nút, và biến động mạnh giữa các lượt khi CPU edge tiến gần 0,5 lõi.

**Phát lệch pha** (các nút chia đều trong mỗi giây; 3 lượt, 40 và 60 nút có thêm một lượt sau khi sửa sink;
[`load_staggered.json`](bench/system-2026-10-24/load_staggered.json)):

| Nút | Gateway p50 | Gateway p95 | Lưu dự đoán | Mosquitto bỏ | > 1,5 chu kỳ | CPU edge |
|---:|---:|---:|---:|---:|---:|---:|
| 20 | 14–16 ms | 0,05 / 0,21 / 11,5 s | 99,75–100 % | 0 | 0–0,9 % | 0,28–0,30 |
| 40 | 0,09–1,86 s | 1,12 / 1,16 / 2,47 / 2,66 s | 100 % | 0 | 5,7–6,4 % | 0,49–0,50 |
| 60 | 23,5–27,3 s | 25,5 / 25,9 / 29,1 / 30,6 s | 58–68 % | 1 656–2 391 | 24–29 % | 0,50 |

Khi gateway còn dư CPU (20 nút), phát lệch pha bỏ được độ trễ xếp hàng do các đợt dồn cùng lúc: p50 15 ms
so với 309 ms khi phát đồng loạt. Lượt 1 ở 20 nút có p95 11,5 s và Kafka p95 1,9 s, tức cả hệ thống bị
khựng ngay sau khi khởi động lại dịch vụ; hai lượt còn lại bình thường. Từ 40 nút, CPU edge chạm
0,5 lõi và kết quả tương đương hoặc kém hơn phát đồng loạt, nên phát lệch pha không nâng ngưỡng bão hòa.
Lượt đầu tiên ([`load_staggered-overlapping-runner.json`](bench/system-2026-10-24/load_staggered-overlapping-runner.json))
bị một tiến trình đo khác chạy chồng trong 13:30–14:20 (Prometheus ghi nhận hai nhóm nút phát cùng lúc),
nên không dùng.

**Thử lâu dài** (20 nút, 32 thành viên, 1 s, phát đồng loạt, 30 s làm nóng + **30 phút** đo, một lượt;
[`load_long-20nodes-30min.json`](bench/system-2026-10-24/load_long-20nodes-30min.json)):

| Đại lượng | Giá trị |
|---|---|
| Gateway p50 / p95 | 348 / 714 ms |
| Kafka p95 (reading) / p99 | 69 / 132 ms |
| Lưu InfluxDB p50 / p95 (reading) | 0,51 / 0,91 s |
| Lưu reading / ground truth / dự đoán | 100 / 100 / 100 % (36 000 mỗi loại) |
| Mosquitto bỏ; DLQ; ACK quá hạn; lỗi ghi InfluxDB | 0; 0; 0; 0 |
| Khoảng cách dự đoán > 1,5 chu kỳ (D11) | 0,57 % |
| Lag đỉnh Sink / Controller | 180 / 29 bản ghi |
| Tuổi mẫu tại Controller p95; lệnh → ACK p95 | 0,78 s; 11 ms |
| CPU edge TB / đỉnh; RAM edge TB / đỉnh | 0,26 / 0,54 lõi; 57 / 70 MiB |
| Tổng CPU / RAM toàn hệ thống (TB) | 0,92 lõi / 1 594 MiB |
| Số series `/metrics` của Bridge đầu → cuối | 178 → 1 041 (khoảng 44 series/nút, không tăng thêm) |

Trong 30 phút không có suy giảm: lưu đủ, không lỗi, lag không tích lũy. Gateway p95 (714 ms) cao hơn bậc
120 s cùng mức (581 ms) vì cửa sổ dài gặp nhiều đợt xếp hàng hơn, nhưng vẫn dưới ngưỡng 1 s.

**Độ trễ tới giao diện (S06/S07)**, 1 nút, UI ghim vào nút đó, 120 mẫu:

| Mốc cuối (từ `observed_at`) | p50 | p95 | Ghi chú |
|---|---:|---:|---|
| Client SSE trên host nhận snapshot chứa reading | 127 ms | 220 ms | Gồm Kafka → backend, cộng chu kỳ đẩy SSE 200 ms; có 1 mẫu ngoại lai 24,7 s |
| Client SSE nhận snapshot chứa dự đoán | 136 ms | 232 ms | |
| Trình duyệt: `renderState()` xong (DOM đã cập nhật) | 261 ms | 1 231 ms | Edge headless |
| Trình duyệt: hai `requestAnimationFrame` sau đó | 2 234 ms | 3 211 ms | Edge headless vẽ cảnh bếp SVG bằng phần mềm; **không đại diện** cho trình duyệt có GPU |

**Khởi động nguội (S16)**, `docker compose --profile demo up -d --wait`, 3 lượt:

| Lượt | Compose sẵn sàng | Reading / dự đoán đầu tiên ở backend UI | Reading đầu tiên trong InfluxDB |
|---:|---:|---:|---:|
| 1 | 33,8 s | 36,0 s | (lỗi cách đo†) |
| 2 | 36,8 s | 37,0 s | 37,1 s |
| 3 | 33,5 s | 35,0 s | 35,1 s |

† Truy vấn `last()` lấy dòng của một node khác còn trong khoảng 5 phút, nên lượt 1 không dùng được cho
chỉ số này.

## 4. Chính sách điều khiển (C01–C11)

Dự đoán và yêu cầu operator được dựng sẵn rồi phát lên MQTT. Chúng đi đúng đường thật: Bridge → Kafka →
Safety Controller → lệnh MQTT → bộ đo đóng vai thiết bị và trả ACK. C01a, C02 và C03 chạy bộ lọc cảnh
báo thật (`TabMAlarmFilter`, cùng quy tắc với firmware) trên đầu ra mô hình dựng sẵn. Chi tiết từng bước:
[`control_cases.csv`](bench/system-2026-10-24/control_cases.csv).

| Mã | Kịch bản | Kết quả | Số đo chính |
|---|---|---|---|
| C01a | Biên τ = 0,48 và 200 ppm của bộ lọc 5 s | **6/7 bước đạt** | Đúng τ → `normal` (sai số dấu phẩy động, mục 2.4); τ ± 10⁻⁶, 199,99 / 200 / 200,01 ppm và ưu tiên `danger` đều đúng |
| C01b | Đầu ra Controller theo từng mức | Đạt | normal / warning / danger / warning / normal khớp bảng chính sách; mỗi lệnh khoảng 27 ms |
| C02 | Dao động quanh τ; bước nhảy; nhịp 1 s và 5 s | Đạt (khảo sát) | τ ± 0,05 đổi chiều mỗi 2 s: 57 lần đổi mức / 120 dự đoán (1 s), 11 / 24 (5 s). Bước nhảy: trễ 2 s (1 s) và 0 s (5 s) |
| C03 | Chỉ C₂H₄ (P = 0,99) | Đạt | 30/30 kết quả `normal` |
| C04 | Can thiệp thủ công và hết hạn | Đạt | Lệnh `manual` sau 40 ms; Auto tiếp quản sau **30,08 s** (TTL 30 s); không có dự đoán thì không có lệnh trong 40 s, dự đoán kế tiếp lấy lại Auto ngay |
| C05 | `danger` trong lúc can thiệp | Đạt | Hủy can thiệp, lệnh `danger` sau 27 ms |
| C06 | Phun dập lửa và liên động | Đạt | Phun kéo theo khóa van và bật còi; `danger` trong lúc phun vẫn giữ phun; sau hạn thì phun tắt |
| C07 | Tắt còi, `danger` mới, tắt lại, bật lại | Đạt | Tắt còi thì chỉ `alarm_on` tắt; 3 dự đoán warning tiếp theo không tạo lệnh; `danger` mới bật lại còi |
| C08 | Không lặp lệnh; ghép lệnh–ACK | Đạt | 20 dự đoán giống nhau → 1 lệnh, 19 bị chặn trùng; 24/24 lệnh có ACK đúng nút và `command_id` |
| C09 | ACK thiếu / trễ 7 s / sai mã / sai nút | **3/4 đạt yêu cầu** | Quá hạn được ghi nhận sau 6,2 / 8,2 / 8,3 s (hạn 5 s + một chu kỳ scrape); ACK sai nút **được chấp nhận** (mục 2.3) |
| C10 | Khởi động lại Controller; ACK retained | Đạt | Sau restart, trạng thái tắt còi và can thiệp mất (an toàn theo thiết kế); lệnh Auto đầu tiên sau 8,5 s; client mới nhận ACK retained đã cũ 352 s, nên ACK retained không chứng minh thiết bị đang trực tuyến |
| C11 | Thiết bị tự dự đoán; gateway nhường quyền | Đạt | Chỉ có reading: 20 dự đoán gateway / 20 s; khi thiết bị tự dự đoán: 0 dự đoán trùng; gateway chạy lại sau 25–30 s (`EDGE_ON_DEVICE_TIMEOUT_S` = 30) |

| Đại lượng (đồng hồ host) | n | p50 | p95 | max |
|---|---:|---:|---:|---:|
| S09 · dự đoán phát lên MQTT → thiết bị nhận lệnh | 60 | 27,2 ms | 44,6 ms | 88,7 ms |
| S11 · dự đoán phát lên MQTT → thiết bị phát ACK | 60 | 27,7 ms | 45,5 ms | 88,7 ms |
| Yêu cầu operator → thiết bị nhận lệnh | 9 | 27,4 ms | 64,2 ms | 72,8 ms |

Trong các phép đo trên, dự đoán được phát thẳng lên MQTT, nên S09/S11 chưa gồm thời gian suy luận ở
gateway. Vòng đầy đủ "reading → lệnh" bằng độ trễ gateway (mục 3) cộng thêm khoảng 27 ms.

## 5. Phục hồi (R01–R07)

Tải nền gồm 5 nút (10 sự kiện/s). Mọi thời điểm tính theo đồng hồ host.
- **Phát hiện**: lần đầu Prometheus báo alert `pending`/`firing`. Thời gian này gồm chu kỳ scrape 5 s và
  thời gian `for` của từng quy tắc.
- **Bản ghi mới đầu tiên**: bản ghi đầu tiên vào Kafka trong số các bản ghi phát ra sau khi lệnh
  `docker start` trả về.
- **Lệnh thử**: một dự đoán dựng sẵn cho một nút mới, để xem đường điều khiển còn chạy trong lúc lỗi không.

| Mã | Sự cố | Phát hiện | Khỏe lại | Mất trong Kafka (reading) | Mất dự đoán | Bản ghi mới đầu tiên | Hết tồn đọng | Lệnh thử trong lúc lỗi | Kết luận |
|---|---|---|---|---|---|---|---|---|---|
| R01 | Dừng Influx Sink 60 s | ServiceDown pending 10,2 s / firing 39,9 s; LagHigh pending 19,4 s | 6,2 s | 0 / 480 | 0 | 0,56 s | InfluxDB ghi bù xong sau **46,3 s** | Có lệnh, 27 ms | **Đạt**: 0 reading thiếu trong InfluxDB |
| R02 | Dừng Bridge 20 s | ServiceDown pending 13,3 s (chưa tới `firing` 30 s) | 5,4 s | **120 / 280 (24 s)** | 120 / 280 | 3,8 s | — | Không có (dự đoán bị mất) | Mất dữ liệu do phiên MQTT sạch |
| R03 | Dừng Kafka 30 s | ServiceDown pending 12,2 s / firing 41,7 s; LagHigh pending 67,3 s | 18,8 s | **0 / 330** | 0 | 65,1 s | Kafka 65,1 s; InfluxDB 68,6 s | Không có trong 15 s; lệnh sau khi phục hồi đến sau 9,7 s | Không mất: Bridge giữ tạm rồi gửi bù |
| R04 | Dừng Mosquitto 20 s | BridgeDisconnected pending 13,3 s / firing 27,6 s; SensorStale pending 23,5 s / firing 32,7 s | 1,2 s | **135 / 280 (27 s)** | 138 / 280 | 7,6 s | — | Không có | Mất ở nguồn phát (không có hàng đợi lưu tạm) |
| R05 | 5 bản tin sai lược đồ, rồi 1 bản tin hợp lệ | — | — | — | — | — | — | — | **Đạt**: 5/5 vào DLQ kèm lý do (JSON hỏng, 15 kênh, sai `node_id`, thiếu timezone, field lạ); bản tin hợp lệ vẫn đi qua |
| R06 | Dừng `edge-inference` 60 s | EdgeInferenceStale pending 16,3 s / firing 30,6 s; ServiceDown pending 11,2 s | 5,8 s | 0 / 480 | **323 / 480** | 0,8 s | — | Có lệnh, 27 ms | **Đạt** (pending trong 30 s); dự đoán trong thời gian ngừng không được bù |
| R07 | Dừng UI, Grafana, Prometheus 30 s | (Prometheus dừng) | UI 7,1 s; Grafana 0,1 s; Prometheus 2,0 s | 0 / 330 | 0 | 0,25 s | — | Có lệnh, 27 ms | **Đạt**: ghi lịch sử và điều khiển vẫn chạy |

Lượt thử đầu tiên ([`recovery_cases-first-attempt.json`](bench/system-2026-10-24/recovery_cases-first-attempt.json))
có hai lỗi của bộ đo, đã sửa trước lượt chính thức:
- Consumer đo không commit offset, nên sau khi Kafka khởi động lại nó nhảy tới cuối phân vùng và báo
  nhầm R03 mất 280 reading.
- Lệnh thử dùng chung một nút nên bị Controller chặn trùng.

## 6. Triển khai mô hình (D03–D08)

Độ chính xác của mô hình (Q01–Q13, D02) nằm riêng trong [`benchmark-model.md`](benchmark-model.md).

**D03–D04, tính nhất quán:**

| Phép đối chiếu | Phạm vi | Sai lệch đặc trưng (max) | Sai lệch đầu ra thô (max / TB) | Quyết định khác nhau |
|---|---|---:|---:|---:|
| Nhân số nguyên Python ↔ onnxruntime | 8 360 cửa sổ | 0 | 0,0018 / 4,0·10⁻⁷ | 0 / 16 720 (P lệch tối đa 2,9·10⁻⁵) |
| ↳ chỉ các cửa sổ gần ngưỡng (\|P − τ\| < 0,02) | 7 CO + 126 C₂H₄ | | | 0 |
| C++ firmware (`host_test`) ↔ numpy / onnxruntime | 96 cửa sổ golden | 1,9·10⁻⁶ | 0,0018 | 0 / 192 (32 thành viên); 0 / 192 (4 thành viên); bộ lọc cảnh báo đúng · **PASS** |

**D05–D06, dung lượng** (firmware build bằng PlatformIO, `esp32-s3-devkitc-1`, 16 MB flash, `-O2`;
xem [`firmware/sizes.json`](bench/system-2026-10-24/firmware/sizes.json)):

| Mục | Dung lượng |
|---|---:|
| ONNX INT8 QDQ (32 thành viên) | 4 017 090 B (3,83 MiB) |
| Trọng số INT8 nhúng trong firmware (`tabm_int8.bin`) | 3 986 240 B (3,80 MiB); cấu hình 4 thành viên dùng chung blob này |
| Ảnh ứng dụng `firmware.bin` | 5 081 152 B |
| Flash dùng / phân vùng ứng dụng | 5 069 785 / 6 553 600 B (**77,4%**) |
| RAM tĩnh (`.dram0.data` + `.dram0.bss`) | 141 576 / 327 680 B (**43,2%**) |
| Mã chương trình (`.flash.text`) / dữ liệu chỉ đọc (`.flash.rodata`, gồm blob mô hình) | 775 020 / 4 111 516 B |

**D08 trên host và C++** (một cửa sổ = đặc trưng + lan truyền xuôi; 1 luồng BLAS):

| Môi trường | 32 thành viên | 4 thành viên |
|---|---:|---:|
| Python, nhân số nguyên (host, 500 cửa sổ): p50 / p95 / max | 9,5 / 11,7 / 14,9 ms (104 cửa sổ/s) | 3,0 / 4,9 / 7,7 ms (299 cửa sổ/s) |
| C++ firmware trên PC (`host_test`, zig c++ `-O2`), trung bình | 21,6 ms | 2,75 ms |
| onnxruntime theo lô 1 024 cửa sổ | 2 006 cửa sổ/s | — |

Trường `windows_per_s` của INT8-32 trong `quality_metrics.json` bị vài lượt ngoại lai (lúc máy bận)
làm sai. Dùng [`model/inference_timing.json`](bench/system-2026-10-24/model/inference_timing.json).

**D07, D08 trên chip, D10–D11 trên Wokwi** (ESP32-S3 mô phỏng, 240 MHz, không PSRAM; `inference_ms`
đo bằng `esp_timer` trên board, tức thời gian mô phỏng; bản tin chẩn đoán mỗi 30 s trên `…/diagnostics`):

| Đại lượng | k = 4 (firmware mặc định) | k = 32 |
|---|---:|---:|
| Thời gian ghi (thật) | 10 phút | 65 phút |
| D08 · suy luận một cửa sổ p50 / p95 | **4 489 / 6 779 ms** | **37 746 / 39 985 ms** |
| D08 · min / max (n) | 4 480 / 6 803 ms (14) | 37 660 / 40 012 ms (13) |
| D10 · bước `sequence` giữa hai dự đoán | 5 (luôn đúng chu kỳ 5 s) | 30–35 |
| D11 · cửa sổ bị bỏ vì đang suy luận | 0 | 73 |
| D07 · heap còn trống thấp nhất / tổng heap | 89 740 / 261 176 B | 91 872 / 261 176 B |
| D07 · khối cấp phát lớn nhất | 53 236 B | 53 236 B |
| D07 · stack tác vụ suy luận còn dư (12 288 B) | 10 484 B | 10 516 B |
| D07 · stack vòng lặp còn dư | 3 528 B | 3 528 B |
| Tốc độ mô phỏng (giây mô phỏng / giây thật) | 0,117 | 0,113 |

- Tỉ lệ k = 32 / k = 4 là 8,4 lần, khớp với C++ trên PC (21,6 / 2,75 ms = 7,9 lần) và ước tính trước đó
  (khoảng 35 s). Bộ nhớ gần như không đổi theo k, vì các thành viên được tính lần lượt trên cùng buffer.
- Với k = 4, board giữ đúng chu kỳ 5 s mô phỏng (0 cửa sổ bị bỏ). Với k = 32, mỗi lượt suy luận dài hơn
  7 chu kỳ, nên đây là lý do firmware dùng k = 4.
- Ở k = 4, p95 cao hơn p50 khoảng 2,3 s vì có vài lượt chậm hơn hẳn (khoảng 6,8 s).
- Cỡ mẫu nhỏ (13–14 lượt) nên p95 gần bằng max. Wokwi không mô phỏng chính xác từng chu kỳ CPU, cache
  flash hay PSRAM; số trên chip thật có thể khác. Nên ghi là "ESP32-S3 mô phỏng trên Wokwi".
- "Khoảng cách hai dự đoán" theo đồng hồ host (41 s ở k = 4) chỉ phản ánh tốc độ mô phỏng chậm, không
  dùng làm D10. D10 dùng bước `sequence` (một reading mỗi giây mô phỏng).

Kết quả: [`wokwi_device_k4.json`](bench/system-2026-10-24/wokwi_device_k4.json),
[`wokwi_device_k32.json`](bench/system-2026-10-24/wokwi_device_k32.json). Lượt k = 32 dùng một bản build
riêng (`kActiveMembers = 32`, [`firmware/pio_build_k32.log`](bench/system-2026-10-24/firmware/pio_build_k32.log)).
Bản build hiện tại (k = 4, có chẩn đoán và chờ NTP) dùng RAM tĩnh 141 608 B (43,2%) và Flash
5 070 057 B (77,4%).

**Sửa firmware trong phiên này:** trên Arduino-ESP32 3.3, `WiFi.hostByName()` xóa cache DNS mà không giữ
khóa TCP/IP; nếu lúc đó SNTP đang chờ phân giải (NTP chưa tới được, như trên Wokwi), lwIP dừng chương
trình (`assert failed: udp_new_ip_type … Required to lock TCPIP core`). Firmware giờ phân giải broker một
lần trước khi chạy SNTP, rồi kết nối MQTT bằng IP.

## 7. Hiện trạng danh mục sau phiên này

| Mã | Trạng thái | Ghi chú |
|---|---|---|
| S01–S05, S08, S10, S12–S15 | Đã đo | 6 mức × 3 lượt, kèm hai biến thể |
| S06 | Đã đo (tới client SSE) | Backend chưa xuất mốc nội bộ; số đo gồm cả chu kỳ đẩy 200 ms |
| S07 | Một phần | Đã đo DOM; số khung hình trên Edge headless không đại diện cho trình duyệt có GPU |
| S09, S11 | Đã đo | Dự đoán phát thẳng lên MQTT; vòng từ reading xem mục 4 |
| S16 | Đã đo | 3 lượt; lượt 1 thiếu mốc InfluxDB |
| S17 | Đã đo theo tiêu chí đề xuất | 20 nút ở cấu hình mặc định |
| D08 (gateway) | Một phần | Thời gian suy luận trung bình trong container theo tải; chưa có p50/p95 riêng từng lượt |
| D09 | Không đo | Thời gian một lượt trên host và C++ có ở mục 6 (D08) |
| D10–D12 | Đã đo | Gateway theo tải; D10–D11 trên Wokwi (k = 4 và k = 32) |
| Q01–Q13, D01–D02 | Ngoài phạm vi | Độ chính xác mô hình: xem `benchmark-model.md` (D01 không đo được, thiếu FP32) |
| D03–D04 | Đã đo | Python ↔ onnxruntime (8 360 cửa sổ), C++ ↔ onnxruntime (96 cửa sổ golden, PASS) |
| D05–D06 | Đã đo | Build PlatformIO; ESP32-S3 16 MB |
| D07, D08 trên chip | Đã đo (Wokwi) | k = 4 và k = 32; chưa có phần cứng thật |
| C01–C11 | Đã đo | C01a 6/7; C09 ACK sai nút không đạt |
| R01–R07 | Đã đo | R04 chỉ ở phía broker; R04 phía ESP32 chạy cùng Wokwi (`--broker-outage`) |
| Gần điểm bão hòa, thử lâu dài | Đã đo | 25/30/35 nút × 3 lượt; 20 nút × 30 phút |
| Phát lệch pha | Đã đo | 20/40/60 nút × 3 lượt; lượt đầu bị tiến trình đo khác chạy chồng, đã bỏ |

## 8. Chạy lại

```powershell
docker compose --profile esp32 up -d --wait                                   # không có simulator
uv run python scripts/bench/load_test.py --nodes 1,10,20,40,60,100 --repeat 3 --reset-services --out-dir docs/bench/run
$env:TABM_MEMBERS = 4; docker compose --profile esp32 up -d --wait edge-inference   # biến thể
uv run python scripts/bench/load_test.py --nodes 100 --repeat 3 --reset-services --label members4-100nodes --out-dir docs/bench/run
uv run python scripts/bench/control_cases.py --out-dir docs/bench/run         # khởi động lại safety-controller (C10)
uv run python scripts/bench/recovery_cases.py --out-dir docs/bench/run        # dừng và chạy lại từng dịch vụ
$env:DEMO_NODE_ID = "bench-ui-001"; docker compose --profile esp32 up -d --wait demo-ui
uv run --with websocket-client python scripts/bench/ui_latency.py --out-dir docs/bench/run
uv run python scripts/bench/startup.py --runs 3 --out-dir docs/bench/run      # down + up profile demo
uv run python scripts/bench/report.py docs/bench/run > docs/bench/run/tables.md
uv run --no-project --with numpy python scripts/bench/inference_timing.py
.venv/Scripts/pio run -d wokwi/esp32-replay/esp32-replay -e esp32s3        # D06: RAM/Flash
```

**Wokwi (D07, D08 trên chip, D10–D11, R04 phía thiết bị):**

1. Build firmware như dòng cuối ở trên.
2. Chạy `./scripts/demo.ps1 esp32` (pipeline không có simulator; gateway không dự đoán cho
   `esp32-edge-01`).
3. Trong VS Code, mở thư mục `wokwi/esp32-replay/esp32-replay` → *Wokwi: Start Simulator*. Chờ serial in
   `MQTT connected`.
4. Chạy `uv run python scripts/bench/wokwi_device.py --wait 40 --minutes 15 --out-dir docs/bench/run`
   (D07/D08/D10/D11). Với k = 32 cần khoảng 65 phút để có hơn 10 lượt suy luận.
5. Chạy `uv run python scripts/bench/wokwi_device.py --minutes 6 --broker-outage 20 --out-dir docs/bench/run`
   (R04: dừng Mosquitto 20 s giữa chừng).
6. Giữ cửa sổ Wokwi đang chạy trong suốt hai bước trên.

Thời lượng xấp xỉ: thử tải mặc định khoảng 60 phút, mỗi biến thể khoảng 10 phút, điều khiển khoảng
7 phút, phục hồi khoảng 13 phút, giao diện khoảng 3 phút, khởi động khoảng 5 phút. Các script tự xóa ACK
retained mà chúng tạo ra. Dữ liệu của các nút `bench-*` vẫn nằm trong InfluxDB; sau phiên này đã xóa
bằng API delete của InfluxDB (1 025 node, mỗi node một predicate `node_id="…"`). InfluxDB vẫn giữ tên
node trong chỉ mục series, nên các biến Grafana liệt kê node từ dữ liệu reading thay vì `schema.tagValues`.
