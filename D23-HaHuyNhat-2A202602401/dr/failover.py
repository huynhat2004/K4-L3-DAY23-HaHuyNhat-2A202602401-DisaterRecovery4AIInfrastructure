"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time
from datetime import datetime, timezone

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append one timestamped failover event and mirror it to stdout."""
    now = time.time()
    event = {
        "ts": now,
        "iso": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        **kw,
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as log:
        log.write(json.dumps(event, ensure_ascii=False) + "\n")
    print(json.dumps(event, ensure_ascii=False), flush=True)
    return event


def state_of(region: str) -> dict:
    """Read target state without treating readiness failure as transport failure."""
    response = httpx.get(f"{URL[region]}/v1/state", timeout=3.0)
    response.raise_for_status()
    return response.json()


def failover(target: str, backend: str, wait: float) -> dict:
    """Restore and warm the target, cutting over only after it is ready."""
    result = {"ok": False, "target": target, "backend": backend}

    try:
        before = state_of(target)
    except Exception as exc:
        before = {"error": type(exc).__name__, "detail": str(exc)}
    emit(step="1_verify_target", target=target, state=before)
    result["state_before"] = before

    try:
        restored = snapshot.get(target, backend)
        rpo = snapshot.rpo(
            pathlib.Path("state/region-a/vectors.sqlite"),
            pathlib.Path(f"state/region-{target}/vectors.sqlite"),
        )
    except BaseException as exc:
        emit(step="2_restore_snapshot", target=target, ok=False,
             error=type(exc).__name__, detail=str(exc))
        result.update(error="restore_failed", detail=str(exc))
        return result

    restore_event = emit(
        step="2_restore_snapshot",
        target=target,
        ok=True,
        snapshot_at=restored.get("snapshot_at"),
        restored_at=restored.get("restored_at"),
        rpo_seconds=rpo.get("rpo_seconds"),
        docs_lost=rpo.get("docs_lost"),
        embed_model_version=restored.get("embed_model_version"),
    )
    result["restore"] = restore_event

    pool_file = pathlib.Path(f"state/region-{target}/pool_state")
    pool_file.parent.mkdir(parents=True, exist_ok=True)
    pool_file.write_text("full\n")
    emit(step="3_scale_pool", target=target, pool_state="full", ok=True)

    ready_started = time.monotonic()
    deadline = ready_started + max(0.0, wait)
    last_reason = "not_probed"
    ready_body = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{URL[target]}/readyz", timeout=2.0)
            ready_body = response.json()
            if response.status_code == 200 and ready_body.get("ready") is True:
                waited = round(time.monotonic() - ready_started, 2)
                emit(step="4_wait_ready", target=target, ok=True,
                     waited_s=waited, readiness=ready_body)
                break
            last_reason = ",".join(ready_body.get("reasons") or
                                   [f"http_{response.status_code}"])
        except Exception as exc:
            last_reason = type(exc).__name__
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
    else:
        waited = round(time.monotonic() - ready_started, 2)
        emit(step="4_wait_ready", target=target, ok=False,
             waited_s=waited, reason=last_reason)
        result.update(error="target_not_ready", reason=last_reason, waited_s=waited)
        return result

    pathlib.Path("edge").mkdir(parents=True, exist_ok=True)
    pathlib.Path("edge/active_region").write_text(target)
    cutover_event = emit(step="5_dns_cutover", target=target, active_region=target, ok=True)

    result.update(
        ok=True,
        state_after=ready_body,
        cutover=cutover_event,
        rpo_seconds=rpo.get("rpo_seconds"),
        docs_lost=rpo.get("docs_lost"),
        embed_model_version=restored.get("embed_model_version"),
    )
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
