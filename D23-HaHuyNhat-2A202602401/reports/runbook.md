# Runbook 1 trang — Region chính down

Phạm vi: Region A là primary, Region B là standby, backend snapshot là filesystem.
Mục tiêu: RTO ≤ 300s, RPO ≤ 300s. Không cutover nếu Region B chưa trả `/readyz` 200.

| # | Bước | Lệnh copy-paste | Biết là xong khi | Owner |
|---|---|---|---|---|
| 1 | Xác nhận outage | `for i in 1 2 3; do curl -fsS --max-time 2 http://127.0.0.1:8001/readyz || true; sleep 5; done` | Region A lỗi 3 lần liên tiếp; Region B vẫn trả lời `curl -fsS http://127.0.0.1:8002/healthz` | on-call SRE |
| 2 | Mở incident và bắt đầu đồng hồ | `date -u +"%Y-%m-%dT%H:%M:%SZ"` | Incident có timestamp sau `t_outage`; runbook ghi `thong_bao_incident` | incident commander |
| 3 | Restore state và scale pool | `python3 dr/runbook.py --primary a --target b --backend fs` | Sau xác nhận `y`, `reports/failover-events.jsonl` có lần lượt `1_verify_target`, `2_restore_snapshot`, `3_scale_pool`, `4_wait_ready` | on-call SRE |
| 4 | Verify state replica | `tail -n 1 reports/runbook-run.jsonl` | Sự kiện `verify_state_replica` có `weights:true`, `vector_count>0`, `rpo_seconds` và `docs_lost` khác null | data/ML platform |
| 5 | Xác nhận DNS/LB cutover | `curl -fsS http://127.0.0.1:8080/edge/state` | `active_region` là `b`; chỉ hợp lệ sau khi B ready | network/on-call SRE |
| 6 | Verify golden signals | `tail -n 2 reports/runbook-run.jsonl` | `verify_golden_signals` có 10 request, error rate `0.0`, p95 dưới `100ms`, tất cả `served_by` là `b` | service owner |
| 7 | Đo RTO/RPO và mở postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `valid:true`, warnings rỗng, `recovered_by_region:b`, `rto_verdict:PASS`, RPO khác null | incident commander |

## Rollback về Region A

Chỉ rollback khi Region A đã trả `/readyz` 200 ổn định ít nhất 3 lần cách nhau 5 giây,
state mới nhất đã được reconcile, và golden test trực tiếp vào A có error rate 0%. Incident
commander là người duy nhất phê duyệt rollback; on-call SRE thực hiện. Nếu A chưa ổn định,
RPO chưa xác minh hoặc B vẫn đang phục vụ tốt thì giữ traffic ở B để tránh flapping.

Lệnh sau khi được phê duyệt: `python3 dr/failover.py --target a --backend fs`. Dừng và giữ
traffic ở B nếu bất kỳ bước restore/readiness nào thất bại; hàm failover sẽ không đổi DNS
khi target chưa ready.
