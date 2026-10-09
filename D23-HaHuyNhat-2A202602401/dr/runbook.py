"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time
import math
from datetime import datetime, timezone

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr.health_checker import probe  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Append one timestamped runbook step."""
    now = time.time()
    event = {
        "ts": now,
        "iso": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "step": n,
        "name": name,
        **kw,
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as log:
        log.write(json.dumps(event, ensure_ascii=False) + "\n")
    print(json.dumps(event, ensure_ascii=False), flush=True)
    return event


def confirm(auto: bool, msg: str) -> bool:
    """Require an explicit operator decision unless CI requested --auto."""
    if auto:
        return True
    return input(f"{msg} [y/N] ").strip().lower() == "y"


def latest_outage(primary: str):
    path = pathlib.Path("chaos/chaos-events.jsonl")
    if not path.exists():
        return None
    events = []
    for line in path.read_text().splitlines():
        if line.strip():
            event = json.loads(line)
            if event.get("action") == "kill" and event.get("region") == primary:
                events.append(event)
    return events[-1] if events else None


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Execute the seven-step, operator-gated regional failover runbook."""
    started = time.time()

    observations = []
    consecutive_failures = 0
    for attempt in range(1, 4):
        primary_ready, primary_reason = probe(primary, timeout=2.0)
        target_ready, target_reason = probe(target, timeout=2.0)
        consecutive_failures = consecutive_failures + 1 if not primary_ready else 0
        observations.append({
            "attempt": attempt,
            "primary_ready": primary_ready,
            "primary_reason": primary_reason,
            "target_ready": target_ready,
            "target_reason": target_reason,
        })
        if attempt < 3:
            time.sleep(5.0)

    outage_confirmed = consecutive_failures >= 3
    step(1, "xac_nhan_outage", primary=primary, target=target,
         confirmed=outage_confirmed, consecutive_fails=consecutive_failures,
         observations=observations)
    if not outage_confirmed:
        return {"ok": False, "error": "outage_not_confirmed"}

    outage = latest_outage(primary)
    incident = step(
        2,
        "thong_bao_incident",
        primary=primary,
        target=target,
        outage_ts=outage.get("ts") if outage else None,
        outage_iso=outage.get("iso") if outage else None,
        notification_delay_s=(
            None if not outage else round(time.time() - outage["ts"], 2)
        ),
    )
    if not confirm(auto, f"Fail over region-{primary} sang region-{target}?"):
        step(7, "post_incident", ok=False, status="operator_cancelled",
             elapsed_s=round(time.time() - started, 2))
        return {"ok": False, "error": "operator_cancelled", "incident": incident}

    failover_result = fo.failover(target, backend, wait=60.0)
    step(3, "scale_gpu_pool", called_failover_once=True,
         ok=failover_result.get("ok", False), target=target)

    step(4, "verify_state_replica", target=target,
         vector_count=(failover_result.get("state_after") or {}).get("vectors", {}).get("count"),
         weights=(failover_result.get("state_after") or {}).get("ready"),
         rpo_seconds=failover_result.get("rpo_seconds"),
         docs_lost=failover_result.get("docs_lost"),
         embed_model_version=failover_result.get("embed_model_version"),
         ok=failover_result.get("ok", False))

    cutover_ok = bool(failover_result.get("ok") and failover_result.get("cutover"))
    step(5, "dns_cutover", target=target, ok=cutover_ok,
         active_region=(failover_result.get("cutover") or {}).get("active_region"))
    if not cutover_ok:
        step(7, "post_incident", ok=False, status="failover_aborted",
             elapsed_s=round(time.time() - started, 2), result=failover_result)
        return failover_result

    latencies = []
    errors = 0
    served_by = []
    for request_number in range(10):
        request_started = time.monotonic()
        try:
            response = httpx.get(
                f"{URL[target]}/v1/infer",
                params={"q": f"golden signal {request_number}"},
                timeout=3.0,
            )
            body = response.json()
            served_by.append(body.get("region"))
            if response.status_code != 200:
                errors += 1
        except Exception:
            errors += 1
        latencies.append((time.monotonic() - request_started) * 1000)

    ordered = sorted(latencies)
    p95 = ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]
    error_rate = errors / len(latencies)
    golden_ok = errors == 0 and all(region == target for region in served_by)
    step(6, "verify_golden_signals", requests=len(latencies), errors=errors,
         error_rate=round(error_rate, 3), p95_latency_ms=round(p95, 1),
         served_by=served_by, ok=golden_ok)

    summary = step(
        7,
        "post_incident",
        ok=golden_ok,
        elapsed_s=round(time.time() - started, 2),
        measure_command=(
            "python3 tools/measure_rto.py --loadgen "
            "reports/drill-2-withdr.jsonl --target-rto 300"
        ),
    )
    return {
        "ok": golden_ok,
        "incident": incident,
        "failover": failover_result,
        "golden_signals": {
            "requests": len(latencies),
            "errors": errors,
            "error_rate": round(error_rate, 3),
            "p95_latency_ms": round(p95, 1),
        },
        "summary": summary,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
