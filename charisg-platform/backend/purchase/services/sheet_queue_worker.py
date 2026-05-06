"""sheet_queue_worker.py — 시트 큐 자동 파이프라인.

흐름 (시트 1개당):
  1. importing       — sheet_url → sourcing_candidates
  2. promoting       — products INSERT (dedup + 형제발견 + 카테고리매핑)
  3. detailing       — Phase 1 (이미지+HTML) + Phase 2 (AI 번역/SEO)
  4. channelsending  — listings_pa INSERT (smartstore + coupang)
  5. uploading_ss    — 스마트스토어 등록
  6. uploading_cu    — 쿠팡 등록
  7. cleaning        — image_cache 정리 + 디스크 회복
  8. done            — 다음 시트로

설정:
  - 동시 1 시트만 처리 (직렬화)
  - 디스크 < DISK_MIN_GB 이면 cleanup 후 5분 대기 (최대 10회 재시도)
  - 시트 간 SHEET_INTERVAL_SEC 대기

lifespan 에서 asyncio.create_task(run_forever()) 로 기동.
"""
import asyncio
import json
import logging
import shutil
import uuid
from datetime import datetime, timezone

from backend.purchase.database import get_db

logger = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 30      # 큐 polling 주기
INITIAL_DELAY_SEC = 60
SHEET_INTERVAL_SEC = 30     # 시트 간 대기 (cleanup 시간)
DISK_MIN_GB = 3.0           # 이 미만이면 cleanup + 대기
DISK_RETRY_INTERVAL_SEC = 300
DISK_RETRY_MAX = 10


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _disk_free_gb() -> float:
    try:
        usage = shutil.disk_usage("/")
        return usage.free / (1024 ** 3)
    except Exception:
        return 99.0


def _update(sid: int, **fields) -> None:
    if not fields:
        return
    keys = list(fields.keys())
    sets = ", ".join(f"{k}=?" for k in keys)
    vals = [fields[k] for k in keys]
    with get_db() as conn:
        conn.execute(f"UPDATE sheet_queue SET {sets} WHERE id=?", (*vals, sid))


def _next_queued() -> dict | None:
    """다음 처리 대상 큐 1건. queued 상태 중 가장 오래된 것."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT * FROM sheet_queue WHERE status='queued'
               ORDER BY queued_at LIMIT 1"""
        ).fetchone()
    return dict(row) if row else None


async def _wait_for_disk() -> bool:
    """디스크 < DISK_MIN_GB 면 cleanup + 대기. 회복 안 되면 False."""
    from backend.purchase.services.image_downloader import cleanup_expired_images

    for attempt in range(DISK_RETRY_MAX):
        free = _disk_free_gb()
        if free >= DISK_MIN_GB:
            return True
        logger.warning(
            f"[sheet-queue] 디스크 부족 ({free:.1f}GB < {DISK_MIN_GB}GB) — "
            f"cleanup 시도 ({attempt+1}/{DISK_RETRY_MAX})"
        )
        # archived/excluded only product 이미지 강제 정리
        try:
            await asyncio.to_thread(_force_cleanup_unused_images)
            await asyncio.to_thread(cleanup_expired_images)
        except Exception:
            logger.exception("[sheet-queue] cleanup 예외")
        await asyncio.sleep(DISK_RETRY_INTERVAL_SEC)
    return False


def _force_cleanup_unused_images() -> None:
    """등록 가능한 채널 (listed/pending/paused) 0개인 product 의 이미지 정리 마킹."""
    now = _now()
    with get_db() as conn:
        conn.execute(
            """UPDATE image_cache SET scheduled_delete_at=?
               WHERE (scheduled_delete_at IS NULL OR scheduled_delete_at > ?)
                 AND NOT EXISTS (
                   SELECT 1 FROM listings_pa l
                   WHERE l.product_id=image_cache.product_id
                     AND l.status IN ('listed','pending','paused')
                 )""",
            (now, now),
        )


async def _process_sheet(item: dict) -> None:
    """시트 1개 전체 파이프라인 처리."""
    sid = item["id"]
    label = item.get("sheet_label") or item["sheet_url"][:40]
    logger.info(f"[sheet-queue] 시작 — sid={sid} label='{label}'")

    _update(sid, status="importing", started_at=_now(), current_step="시트 import 중")

    # ── 1. import ────────────────────────────────────────
    try:
        from backend.purchase.services.sheet_importer import import_from_sheet_url
        r = await asyncio.to_thread(import_from_sheet_url, item["sheet_url"])
        if r.get("error"):
            _update(sid, status="error", error_message=str(r), finished_at=_now())
            return
        imported = sum((t.get("imported") or 0) for t in (r.get("tabs") or []))
        _update(sid, imported=imported, current_step=f"import 완료 — {imported}건")
        if imported == 0:
            _update(sid, status="done", finished_at=_now(),
                    current_step="import 0건 — skip")
            return
    except Exception as e:
        logger.exception(f"[sheet-queue] sid={sid} import 실패")
        _update(sid, status="error", error_message=f"import: {str(e)[:300]}",
                finished_at=_now())
        return

    # 신규 promote 된 product 식별용: 현재 sourcing_candidates ID 들
    with get_db() as conn:
        src_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM sourcing_candidates"
        ).fetchall()]

    # ── 2. promote ──────────────────────────────────────
    _update(sid, status="promoting", current_step="promote 진행 중")
    try:
        from backend.purchase.services.sourcing_promote import (
            create_promote_job, run_promote_background, get_promote_job,
        )
        promote_job = create_promote_job(imported)
        await run_promote_background(promote_job)
        job = get_promote_job(promote_job)
        result = {}
        if job and job.get("result_json"):
            try:
                result = json.loads(job["result_json"])
            except Exception:
                pass
        promoted = result.get("new", 0)
        duplicates = result.get("duplicate_skipped", 0)
        _update(sid, promoted=promoted, duplicates=duplicates,
                current_step=f"promote 완료 — 신규 {promoted}/중복 {duplicates}")
    except Exception as e:
        logger.exception(f"[sheet-queue] sid={sid} promote 실패")
        _update(sid, status="error", error_message=f"promote: {str(e)[:300]}",
                finished_at=_now())
        return

    if promoted == 0:
        _update(sid, status="done", finished_at=_now(),
                current_step=f"신규 0건 (중복 {duplicates}) — 다음 단계 skip")
        return

    # 신규 product_ids 조회 (sourcing_id 로 lookup, OR IGNORE 통과한 것만)
    if not src_ids:
        _update(sid, status="done", finished_at=_now(), current_step="src_ids 없음")
        return
    with get_db() as conn:
        ph = ",".join("?" * len(src_ids))
        new_pids = [r["id"] for r in conn.execute(
            f"SELECT id FROM products WHERE sourcing_id IN ({ph})", src_ids
        ).fetchall()]
    # 형제 발견으로 추가된 product 도 포함 (parent_asin 기준)
    if new_pids:
        with get_db() as conn:
            ph2 = ",".join("?" * len(new_pids))
            sib_pids = [r["id"] for r in conn.execute(
                f"""SELECT DISTINCT p2.id FROM products p1
                    JOIN products p2 ON p2.parent_asin = p1.parent_asin
                    WHERE p1.id IN ({ph2}) AND p1.parent_asin IS NOT NULL""",
                new_pids,
            ).fetchall()]
        new_pids = list(set(new_pids) | set(sib_pids))

    if not new_pids:
        _update(sid, status="done", finished_at=_now(), current_step="신규 pid 0건")
        return

    logger.info(f"[sheet-queue] sid={sid} 신규 product {len(new_pids)}건 (형제 포함)")

    # ── 3. 상세 생성 (Phase 1 + 2) ───────────────────────
    _update(sid, status="detailing",
            current_step=f"상세생성 진행 중 — 대상 {len(new_pids)}건")
    try:
        from backend.purchase.services.ai_processor import run_two_stage_batch
        detail_job_id = uuid.uuid4().hex[:12]
        with get_db() as conn:
            conn.execute(
                """INSERT INTO batch_jobs (id, job_type, status, total, created_at)
                   VALUES (?, 'ai_detail', 'pending', ?, ?)""",
                (detail_job_id, len(new_pids), _now()),
            )
        await run_two_stage_batch(detail_job_id, new_pids)
        _update(sid, detailed=len(new_pids),
                current_step=f"상세생성 완료 — {len(new_pids)}건")
    except Exception as e:
        logger.exception(f"[sheet-queue] sid={sid} 상세생성 실패 (계속 진행)")
        _update(sid, error_message=f"detailing: {str(e)[:200]}")

    # ── 4. 채널 보내기 (listings_pa INSERT) ─────────────
    channels_csv = item.get("target_channels") or "smartstore,coupang"
    channels = [c.strip() for c in channels_csv.split(",") if c.strip()]
    _update(sid, status="channelsending",
            current_step=f"채널 보내기 중 ({','.join(channels)})")
    try:
        from backend.purchase.services.channel_listing_service import send_to_channels
        sent = 0
        for pid in new_pids:
            try:
                await asyncio.to_thread(send_to_channels, pid, channels)
                sent += 1
            except Exception as e:
                logger.warning(f"[sheet-queue] sid={sid} pid={pid} send 실패: {e}")
        _update(sid, current_step=f"채널 보내기 완료 — {sent}/{len(new_pids)} ({','.join(channels)})")
    except Exception as e:
        logger.exception(f"[sheet-queue] sid={sid} channelsending 실패")

    # ── 5+6. 스마트스토어 + 쿠팡 업로드 (병렬) ───────────
    # 두 채널 API 별개라 병렬 처리해서 시간 단축. 스마트스토어 ~3시간 + 쿠팡 ~10분 →
    # max(3시간, 10분) = 3시간 (쿠팡은 일찍 끝나도 워커는 양쪽 await 후 다음 단계).
    _update(sid, status="uploading_smartstore",
            current_step="스마트스토어 + 쿠팡 업로드 병렬 중")

    async def _run_smartstore():
        with get_db() as conn:
            ph = ",".join("?" * len(new_pids))
            ss_pids = [r["product_id"] for r in conn.execute(
                f"""SELECT product_id FROM listings_pa
                    WHERE channel='smartstore' AND status='pending'
                    AND product_id IN ({ph})""",
                new_pids,
            ).fetchall()]
        if not ss_pids:
            return 0, 0

        # ── 한도 회전 (rotation) — 신규 N건 등록 시 10000 한도 초과 예상이면
        # 가장 오래된 무매출 listed 상품 swap 처리 후 진행.
        try:
            from backend.purchase.services.listing_rotation import (
                calculate_swap_needed, swap_oldest_no_sales,
            )
            from backend.purchase.services.notifier import notify_swap_complete
            needed = calculate_swap_needed(len(ss_pids))
            if needed > 0:
                _update(sid, current_step=f"한도 회전 — 무매출 오래된 {needed}건 swap 중")
                rot = await swap_oldest_no_sales(needed)
                logger.info(
                    f"[sheet-queue] sid={sid} rotation: 요청 {rot['requested']} / "
                    f"후보 {rot['candidates']} / 성공 {rot['ok']} / 실패 {rot['fail']}"
                )
                try:
                    notify_swap_complete(rot["requested"], rot["ok"], rot["fail"])
                except Exception as e:
                    logger.warning(f"[sheet-queue] swap 알림 실패 (무시): {e}")
        except Exception as e:
            logger.exception(f"[sheet-queue] sid={sid} rotation 실패 (계속 진행): {e}")

        from backend.purchase.routers.smartstore import _run_upload_background
        ss_job = uuid.uuid4().hex[:12]
        with get_db() as conn:
            conn.execute(
                """INSERT INTO batch_jobs (id, job_type, status, total, created_at)
                   VALUES (?, 'smartstore_upload', 'pending', ?, ?)""",
                (ss_job, len(ss_pids), _now()),
            )
        await _run_upload_background(ss_job, ss_pids, "smartstore")
        with get_db() as conn:
            ss_listed = conn.execute(
                f"""SELECT COUNT(*) c FROM listings_pa
                    WHERE channel='smartstore' AND status='listed'
                    AND product_id IN ({','.join('?' * len(ss_pids))})""",
                ss_pids,
            ).fetchone()["c"]
        return ss_listed, len(ss_pids) - ss_listed

    async def _run_coupang():
        with get_db() as conn:
            ph = ",".join("?" * len(new_pids))
            cu_pids = [r["product_id"] for r in conn.execute(
                f"""SELECT product_id FROM listings_pa
                    WHERE channel='coupang' AND status='pending'
                    AND product_id IN ({ph})""",
                new_pids,
            ).fetchall()]
        if not cu_pids:
            return 0, 0
        from backend.purchase.routers.coupang import _run_coupang_upload_bg
        cu_job = uuid.uuid4().hex[:12]
        with get_db() as conn:
            conn.execute(
                """INSERT INTO batch_jobs (id, job_type, status, total, created_at)
                   VALUES (?, 'coupang_upload', 'pending', ?, ?)""",
                (cu_job, len(cu_pids), _now()),
            )
        await _run_coupang_upload_bg(cu_job, cu_pids)
        with get_db() as conn:
            cu_listed = conn.execute(
                f"""SELECT COUNT(*) c FROM listings_pa
                    WHERE channel='coupang' AND status='listed'
                    AND product_id IN ({','.join('?' * len(cu_pids))})""",
                cu_pids,
            ).fetchone()["c"]
        return cu_listed, len(cu_pids) - cu_listed

    # 양 채널 병렬 실행 — return_exceptions 로 한쪽 실패해도 다른 쪽 계속
    try:
        ss_result, cu_result = await asyncio.gather(
            _run_smartstore(), _run_coupang(), return_exceptions=True,
        )
        if isinstance(ss_result, BaseException):
            logger.exception(f"[sheet-queue] sid={sid} smartstore 예외")
            ss_listed, ss_failed = 0, 0
        else:
            ss_listed, ss_failed = ss_result
        if isinstance(cu_result, BaseException):
            logger.exception(f"[sheet-queue] sid={sid} coupang 예외")
            cu_listed, cu_failed = 0, 0
        else:
            cu_listed, cu_failed = cu_result
        _update(sid,
                smartstore_listed=ss_listed, smartstore_failed=ss_failed,
                coupang_listed=cu_listed, coupang_failed=cu_failed,
                current_step=f"양 채널 병렬 업로드 완료 — SS {ss_listed} / CU {cu_listed}")
    except Exception as e:
        logger.exception(f"[sheet-queue] sid={sid} 병렬 업로드 실패")
        _update(sid, error_message=f"upload-parallel: {str(e)[:200]}")

    # ── 7. cleaning ────────────────────────────────────
    _update(sid, status="cleaning", current_step="이미지 정리 중")
    try:
        await asyncio.to_thread(_force_cleanup_unused_images)
        from backend.purchase.services.image_downloader import cleanup_expired_images
        await asyncio.to_thread(cleanup_expired_images)
    except Exception:
        logger.exception(f"[sheet-queue] sid={sid} cleanup 예외")

    # ── 8. done ────────────────────────────────────────
    _update(sid, status="done", finished_at=_now(),
            current_step="완료")
    logger.info(f"[sheet-queue] 완료 — sid={sid} label='{label}'")


async def run_forever() -> None:
    """lifespan 에서 create_task 로 기동."""
    await asyncio.sleep(INITIAL_DELAY_SEC)
    logger.info("[sheet-queue-worker] 기동")
    while True:
        try:
            item = _next_queued()
            if not item:
                await asyncio.sleep(POLL_INTERVAL_SEC)
                continue

            # 디스크 체크
            ok = await _wait_for_disk()
            if not ok:
                _update(item["id"], status="error",
                        error_message=f"디스크 부족 — {DISK_MIN_GB}GB 회복 실패",
                        finished_at=_now())
                continue

            await _process_sheet(item)
            await asyncio.sleep(SHEET_INTERVAL_SEC)
        except asyncio.CancelledError:
            logger.info("[sheet-queue-worker] 취소됨")
            raise
        except Exception:
            logger.exception("[sheet-queue-worker] 루프 예외 — 60초 대기 후 계속")
            await asyncio.sleep(60)
