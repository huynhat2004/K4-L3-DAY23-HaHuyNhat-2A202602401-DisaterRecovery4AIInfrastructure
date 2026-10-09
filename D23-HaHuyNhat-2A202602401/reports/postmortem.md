# Postmortem — DR Drill Lab 23

Đây là postmortem blameless: mục tiêu là tìm điều kiện hệ thống và quy trình cần cải
thiện, không quy trách nhiệm cho người kích hoạt chaos drill.

## 1. Timeline

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 2026-10-09T03:36:14 | Region A bị netblock, bắt đầu RTO | `chaos/chaos-events.jsonl:4` |
| 2026-10-09T03:36:14.894130Z | User đầu tiên nhận 503 | `reports/drill-2-withdr.jsonl:25` |
| 2026-10-09T03:36:29.791666Z | Health checker đánh dấu A UNHEALTHY sau 3 lỗi | `reports/health-events.jsonl:2` |
| 2026-10-09T03:36:31.032089Z | Runbook bắt đầu failover sau khi xác nhận incident | `reports/failover-events.jsonl:1` |
| 2026-10-09T03:36:37.262049Z | DNS cutover sang B | `reports/failover-events.jsonl:5` |
| 2026-10-09T03:36:43.136427Z | Request đầu tiên thành công từ B | `reports/drill-2-withdr.jsonl:39` |

## 2. RTO/RPO so với mục tiêu và gap analysis

- RTO mục tiêu: `300s`; đo được: `28.3s`; gap/headroom: `271.7s` — **PASS**.
- RPO mục tiêu: `300s`; đo được: `4.01s` và mất 2 documents; gap/headroom:
  `295.99s` — **PASS**.
- Bước tốn nhiều thời gian nhất là health-check detection floor `15.0s`, chiếm
  `53.0%` RTO. Nguyên nhân là chính sách 5 giây × 3 lỗi liên tiếp để chống flapping.
- GPU warm-up mất `6.22s`; DNS cache và thời điểm request kế tiếp đóng góp khoảng
  `5.9s`. Snapshot/khâu xác nhận từ detection đến restore hoàn tất mất khoảng `1.2s`.

## 3. Root cause — 5 whys

1. User nhận 503 vì edge vẫn route request tới Region A sau khi A bị netblock.
2. Edge chưa đổi sang B vì health checker cần đủ ba lần lỗi liên tiếp và runbook cần
   xác nhận outage.
3. Không thể đổi ngay vì Region B ban đầu ở trạng thái warm, không có vector data và
   model weights.
4. B cần restore vì kiến trúc là active-passive, snapshot được replicate theo chu kỳ
   thay vì đồng bộ từng write.
5. Nếu đây là outage thật, rủi ro lớn nhất là snapshot bị thiếu/hỏng hoặc model version
   không khớp; khi đó bước restore/readiness sẽ abort và RTO vượt mục tiêu. Guardrail
   không-cutover-khi-chưa-ready ngăn sự cố lan sang cả hai region nhưng không tự tạo
   một replica tốt.

## 4. Action items

| # | Action item | Owner | Deadline | Tác động dự kiến |
|---|---|---|---|---|
| 1 | Đánh giá interval 2s, threshold 3 và alert false-positive trong 5 game day | SRE | 2026-10-16 | Giảm detection floor tối đa 9s |
| 2 | Thêm kiểm tra định kỳ snapshot restore + checksum + model version ở Region B | ML Platform | 2026-10-23 | Giảm nguy cơ restore thất bại; giữ RPO có bằng chứng |
| 3 | Duy trì một worker B pre-warmed và đo chi phí | FinOps + ML Platform | 2026-10-30 | Giảm khoảng 6.2s GPU warm-up |

## 5. Câu hỏi bắt buộc

1. `interval × threshold = 5s × 3 = 15s`, chiếm `15 / 28.3 = 53.0%` RTO.
2. Nếu interval giảm xuống 1s và các yếu tố khác giữ nguyên, detection floor lý thuyết
   còn 3s, RTO giảm khoảng 12s. Đổi lại là tăng 5 lần probe traffic, nhạy hơn với lỗi
   thoáng qua và tăng nguy cơ false alert/flapping; timeout cũng phải được điều chỉnh.
3. Nếu primary mất vĩnh viễn sau outage 6 giờ, `docs_lost` là số document khách hàng
   đã ghi nhận ở primary nhưng không tồn tại trong snapshot được restore. Ở lần drill
   này là 2 document; trong sự cố dài, con số đó là phạm vi dữ liệu phải đối soát hoặc
   yêu cầu khách hàng gửi lại, không chỉ là một chỉ số kỹ thuật.
