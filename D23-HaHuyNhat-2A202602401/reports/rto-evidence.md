# RTO/RPO Evidence — Lab 23

Mọi số liệu dưới đây được tính từ timestamp của chính lần drill này.

## 1. Drill 1 — không có DR

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---:|---|---|
| t_outage | `2026-10-09T03:34:30` | chaos kill Region A | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | `+0.0s` | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:17` |
| Request thành công sau đó | Không có | tất cả request còn lại đều lỗi | `reports/drill-1-nodr.jsonl:32` |
| RTO | `NO_RECOVERY` | không có `ok:true` sau lần lỗi đầu tiên | `reports/drill-1-nodr.jsonl:17` |

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---:|---|---|
| t_outage | `0.0s` | `action:kill`, Region A | `chaos/chaos-events.jsonl:4` |
| User thấy lỗi đầu tiên | `0.1s` | dòng `ok:false` đầu tiên | `reports/drill-2-withdr.jsonl:25` |
| Health check phát hiện | `15.0s` | `to:UNHEALTHY`, Region A | `reports/health-events.jsonl:2` |
| Snapshot restore xong | `16.2s` | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region B ready | `22.4s` | `step:4_wait_ready`, `waited_s:6.22` | `reports/failover-events.jsonl:4` |
| DNS cutover | `22.4s` | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | **`28.3s`** | request `ok:true` đầu tiên sau lỗi, phục vụ bởi B | `reports/drill-2-withdr.jsonl:39` |

| Chỉ số | Đo được | Mục tiêu | Verdict |
|---|---:|---:|---|
| RTO — Inference API | `28.3s` | `300s` | **PASS** |
| RPO — Vector DB | `4.01s / 2 documents` | `300s` | **PASS** |

RPO được tính bằng timestamp document mới nhất và số document ở primary có
`ingested_at` mới hơn bản restore. Snapshot chứa 224 vectors và đúng model version
`embed-model=vi-e5-base@v3`; xem `reports/failover-events.jsonl:2` và
`reports/failover-events.jsonl:4`.

## 3. Breakdown RTO

| Thành phần | Giây | Nguồn | Cách giảm |
|---|---:|---|---|
| Health-check detect floor | `15.0s` | `interval_s:5.0 × threshold:3` tại `reports/health-events.jsonl:2` | Giảm interval sau khi đo false-positive rate; giữ threshold chống flapping |
| Xác nhận/restore snapshot | `1.2s` | t_restore − t_detect từ `reports/health-events.jsonl:2` đến `reports/failover-events.jsonl:2` | Tự động chuẩn bị replica và tối ưu snapshot restore |
| GPU pool warm-up | `6.2s` | `waited_s:6.22` tại `reports/failover-events.jsonl:4` | Duy trì warm capacity hoặc pre-warm pool |
| DNS/LB TTL cache và request kế tiếp | `5.9s` | t_recovered − t_cutover, `reports/failover-events.jsonl:5` → `reports/drill-2-withdr.jsonl:39` | Giảm TTL có kiểm soát hoặc dùng global LB push-based |
| **Tổng** | **`28.3s`** | tổng bốn thành phần | Đúng bằng RTO đo từ trải nghiệm user |
