"""PA Automation 탭 — 파이프라인 진척도 + 게이트 차단 + 워커 상태 + 에러 로그.

Endpoints:
  GET /api/pa/automation/pipeline   — 각 단계 카운트
  GET /api/pa/automation/gates      — 게이트별 차단 통계 (24h / 7d / total)
  GET /api/pa/automation/workers    — systemd 워커/타이머 상태
  GET /api/pa/automation/errors     — 에러 로그 (단계 UNION, 필터링/페이지네이션)
"""
from __future__ import annotations
import subprocess
import json as _json
from fastapi import APIRouter, Depends, Query

from backend.purchase.auth import current_user
from backend.purchase.database import get_db

router = APIRouter(prefix="/api/pa/automation", tags=["pa-automation"])


# ─── 게이트 패턴 (error_message / skip_reason 매칭) ──────────────
GATE_PATTERNS = [
    ("margin_lt_15k",       "마진 < 15K",      "%마진 차단%"),
    ("optical_medical",     "도수 광학",       "%광학 의료기기%"),
    ("kc_required",         "KC 어린이제품",    "%KC%"),
    ("dtc_genetic",         "DTC 유전자",      "%유전자%"),
    ("apparel_shoes",       "의류·신발",        "%의류%"),
    ("prohibited_ingredient", "금지 성분",     "%금지 성분%"),
    ("korean_mfr",          "한국 제조사",      "%한국 제조사%"),
    ("category_blocked",    "금지 카테고리",    "%금지 카테고리%"),
    ("splits_overflow",     "splits > 8",     "%splits%"),
    ("placeholder_title",   "placeholder title", "%placeholder%"),
    ("axis_collapse",       "옵션축 붕괴",     "%옵션축%"),
    ("noise_drop",          "노이즈 옵션 drop", "%노이즈%"),
    ("mandatory_attr",      "MANDATORY 속성",  "%MANDATORY%"),
    ("brand_blocklist",     "브랜드 블랙리스트", "%블랙리스트%"),
    ("cost_unavailable",    "cost 책정 불가",  "%cost 책정 불가%"),
]


# ─── 1. Pipeline 진척도 ──────────────────────────────────────────
@router.get("/pipeline")
def get_pipeline(user: dict = Depends(current_user)):
    """M1~M22 진행 현황.

    ★2026-08-15 전면 교체 — 이전 단계는 없어진 흐름(sheet_queue·sourcing_candidates)을
      보고 있었고, 등록 단계는 **쿠팡만** 셌다. 네이버에서 19건이 팔리는데 화면에 없었다.
      지금 진실은 import_* 테이블과 전 채널 listings_pa 다.
    """
    # ★목록을 여기 적지 않는다 — 방금 stopped 를 빠뜨려 11번가 4건이 새어 들어왔다.
    from backend.purchase.listing_status import DEAD, SELLING
    q = ",".join("?" * len(DEAD))
    qs = ",".join("'%s'" % x for x in SELLING)

    with get_db() as conn:
        def one(sql, args=()):
            r = conn.execute(sql, args).fetchone()
            return r[0] if r else 0

        staged = one("SELECT COUNT(*) FROM import_staging")
        batches = one("SELECT COUNT(DISTINCT batch) FROM import_staging")
        picked = one("SELECT COUNT(*) FROM import_pick")
        routed = one("SELECT COUNT(*) FROM import_route")
        grouped = one("SELECT COUNT(*) FROM import_route WHERE route='group'")

        # 리스크 3축 — 차단은 '오류'가 아니라 정상 작동이다. 따로 센다.
        risk_rows = {r["verdict"]: r["n"] for r in conn.execute(
            "SELECT verdict, COUNT(*) n FROM import_risk GROUP BY verdict")}

        detailed = one("SELECT COUNT(*) FROM import_detail")

        # 등록 — ★전 채널. 채널 원문 상태가 있으면 그걸 우선한다(우리 판정은 부풀 수 있다).
        by_ch = {}
        for r in conn.execute(
                "SELECT channel, status, COUNT(*) n FROM listings_pa"
                " WHERE channel_product_id IS NOT NULL GROUP BY channel, status"):
            by_ch.setdefault(r["channel"], {})[r["status"]] = r["n"]
        live = one("SELECT COUNT(*) FROM listings_pa WHERE channel_product_id IS NOT NULL"
                   " AND status NOT IN (%s)" % q, DEAD)
        selling = one("SELECT COUNT(*) FROM listings_pa WHERE channel_status IN (%s)" % qs)
        drifted = one("SELECT COUNT(*) FROM listings_pa WHERE status='listed'"
                      " AND channel_status IS NOT NULL"
                      " AND channel_status NOT IN (%s)" % qs)

        # 옵션 회수 — 주문이 왔을 때 어느 자식인지 아는가
        opt_rows = one("SELECT COUNT(*) FROM listing_options lo JOIN listings_pa l"
                       " ON l.id=lo.listing_id WHERE l.status NOT IN (%s)" % q, DEAD)
        opt_id = one("SELECT COUNT(*) FROM listing_options lo JOIN listings_pa l"
                     " ON l.id=lo.listing_id WHERE l.status NOT IN (%s)" % q
                     + " AND lo.channel_option_id IS NOT NULL AND lo.channel_option_id<>''", DEAD)
        try:
            queue = {r["kind"]: r["n"] for r in conn.execute(
                "SELECT kind, COUNT(*) n FROM listing_repair_queue"
                " WHERE resolved_at IS NULL GROUP BY kind")}
        except Exception:      # noqa: BLE001
            queue = {}

    stages = [
        {
            "key": "import", "label": "M1~M5 수집·적재",
            "total": staged, "done": picked, "in_progress": max(staged - picked, 0), "error": 0,
            "detail": {"배치": batches, "적재 ASIN": staged, "선별": picked},
        },
        {
            "key": "select", "label": "M6~M9 선별·그룹",
            "total": picked, "done": routed, "in_progress": max(picked - routed, 0), "error": 0,
            "detail": {"경로확정": routed, "그룹": grouped, "단품": routed - grouped},
        },
        {
            "key": "gate", "label": "M12~M14 리스크 3축",
            "total": sum(risk_rows.values()),
            "done": risk_rows.get("통과", 0) + risk_rows.get("비대상", 0),
            "in_progress": risk_rows.get("보류", 0) + risk_rows.get("사람검토", 0),
            # ★차단은 오류가 아니다. 게이트가 제 일을 한 것이다.
            "error": 0,
            "detail": dict(risk_rows, 상세수집=detailed),
        },
        {
            "key": "upload", "label": "M18~M20 채널 업로드",
            "total": live, "done": selling, "in_progress": max(live - selling - drifted, 0),
            "error": drifted,
            "detail": {ch: st for ch, st in sorted(by_ch.items())},
        },
        {
            "key": "verify", "label": "M21~M22 옵션·자식 회수",
            "total": opt_rows, "done": opt_id, "in_progress": 0,
            "error": sum(queue.values()),
            # ★옵션ID 가 없으면 주문이 와도 어느 자식인지 모른다 → 오배송
            "detail": dict({"옵션행": opt_rows, "ID확보": opt_id}, **queue),
        },
    ]
    return {"stages": stages}


# ─── 2. 게이트별 차단 통계 ────────────────────────────────────────
@router.get("/gates")
def get_gates(user: dict = Depends(current_user)):
    out = []
    with get_db() as conn:
        for key, label, pattern in GATE_PATTERNS:
            row = conn.execute(
                """SELECT
                     SUM(CASE WHEN last_synced_at >= datetime('now','-1 day') THEN 1 ELSE 0 END) AS h24,
                     SUM(CASE WHEN last_synced_at >= datetime('now','-7 days') THEN 1 ELSE 0 END) AS d7,
                     COUNT(*) AS total
                   FROM listings_pa
                   WHERE channel='coupang' AND status='excluded' AND error_message LIKE ?""",
                (pattern,),
            ).fetchone()
            # queue 도 합산
            q_row = conn.execute(
                """SELECT
                     SUM(CASE WHEN finished_at >= datetime('now','-1 day') THEN 1 ELSE 0 END) AS h24,
                     SUM(CASE WHEN finished_at >= datetime('now','-7 days') THEN 1 ELSE 0 END) AS d7,
                     COUNT(*) AS total
                   FROM group_registration_queue
                   WHERE status IN ('skipped','error')
                     AND COALESCE(skip_reason, error_message) LIKE ?""",
                (pattern,),
            ).fetchone()
            out.append({
                "key": key,
                "label": label,
                "h24": (row["h24"] or 0) + (q_row["h24"] or 0),
                "d7":  (row["d7"]  or 0) + (q_row["d7"]  or 0),
                "total": (row["total"] or 0) + (q_row["total"] or 0),
            })
    out.sort(key=lambda x: -x["total"])
    return {"gates": out}


# ─── 3. 워커/Timer 상태 ──────────────────────────────────────────
def _systemctl_show(unit: str) -> dict:
    """systemctl show --property=... 로 unit 상태 파싱."""
    props = ["ActiveState", "SubState", "MemoryCurrent", "ExecMainStartTimestamp",
             "NextElapseUSecRealtime", "LastTriggerUSec"]
    try:
        r = subprocess.run(
            ["systemctl", "show", unit, "--property=" + ",".join(props)],
            capture_output=True, text=True, timeout=5,
        )
        out: dict = {}
        for line in r.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k] = v
        return out
    except Exception:
        return {}


@router.get("/workers")
def get_workers(user: dict = Depends(current_user)):
    # ★2026-06-02 실재 타이머 전체로 동기화 (누락 8개 추가). 신규 추가 시 여기 등록.
    units = [
        # 상시 서비스
        ("charisg-pa-api.service",                "PA API (sheet worker 포함)"),
        ("charisg-sheet-stuck-watcher.service",   "Stuck Watcher (1h 자동 재개)"),
        # 그룹(옵션) 워커
        ("charisg-pa-group-worker.service",       "Group Worker (야간)"),
        ("charisg-pa-group-worker.timer",         "Group Worker Timer"),
        # 쿠폰 (발행/적용 분리)
        ("charisg-coupon-monthly-publish.timer",  "쿠폰 월간 발행 (1일 03:00)"),
        ("charisg-coupon-catchup.timer",          "쿠폰 적용 catchup (매일 04:00)"),
        # 가격/마진
        ("charisg-refresh-landed-prices.timer",   "SP-API Landed 갱신"),
        ("charisg-fill-discount.timer",           "할인 PUT"),
        ("charisg-margin-monitor.timer",          "마진 모니터 (관찰)"),
        ("charisg-price-reconcile.timer",         "정가 reconcile"),
        ("charisg-fx-refresh.timer",              "환율 갱신"),
        # 리스팅/카테고리 유지
        ("charisg-coupang-quota-retry.timer",     "쿠팡 quota retry"),
        ("charisg-search-tags-backfill.timer",    "검색태그 백필"),
        ("charisg-category-backfill.timer",       "카테고리 백필 (17:00)"),
        ("charisg-classify-stale-pending.timer",  "stale pending 분류"),
        ("charisg-reconcile-product-status.timer","상품상태 reconcile"),
        ("charisg-verify-unchecked.timer",        "KR직배 미검증 검증"),
        ("charisg-pipeline-audit.timer",          "파이프라인 감사 (funnel)"),
    ]
    out = []
    for unit, label in units:
        info = _systemctl_show(unit)
        out.append({
            "unit": unit,
            "label": label,
            "active": info.get("ActiveState") == "active",
            "sub_state": info.get("SubState"),
            "memory_mb": (int(info.get("MemoryCurrent") or 0) // (1024*1024)) if info.get("MemoryCurrent", "0").isdigit() else None,
            "started_at": info.get("ExecMainStartTimestamp") or None,
            "last_trigger": info.get("LastTriggerUSec") or None,
        })
    return {"workers": out}


# ─── 5. 실시간 진행률 (in-flight + rate + ETA) ───────────────────
@router.get("/live")
def get_live(user: dict = Depends(current_user)):
    with get_db() as conn:
        # 현재 처리 중 (registering / pre_scanning)
        in_flight_row = conn.execute(
            """SELECT id, parent_asin, status,
                      CAST((julianday('now') - julianday(COALESCE(finished_at, started_at, queued_at)))*86400 AS INT) AS dur_sec
               FROM group_registration_queue
               WHERE status IN ('registering','pre_scanning')
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        in_flight = None
        if in_flight_row:
            in_flight = {
                "queue_id": in_flight_row["id"],
                "parent_asin": in_flight_row["parent_asin"],
                "status": in_flight_row["status"],
                "duration_sec": in_flight_row["dur_sec"] or 0,
            }

        # 최근 5분 처리율
        rate_done = conn.execute(
            "SELECT COUNT(*) c FROM group_registration_queue "
            "WHERE status='done' AND finished_at >= datetime('now','-5 minutes')"
        ).fetchone()["c"]
        rate_skipped = conn.execute(
            "SELECT COUNT(*) c FROM group_registration_queue "
            "WHERE status='skipped' AND finished_at >= datetime('now','-5 minutes')"
        ).fetchone()["c"]
        per_min = (rate_done + rate_skipped) / 5.0

        # 큐 잔여
        queued = conn.execute(
            "SELECT COUNT(*) c FROM group_registration_queue WHERE status='queued'"
        ).fetchone()["c"]
        done_total = conn.execute(
            "SELECT COUNT(*) c FROM group_registration_queue WHERE status='done'"
        ).fetchone()["c"]
        total = conn.execute(
            "SELECT COUNT(*) c FROM group_registration_queue"
        ).fetchone()["c"]

    # ETA
    eta_sec = None
    eta_human = None
    if per_min > 0 and queued > 0:
        eta_sec = int(queued / per_min * 60)
        h, rem = divmod(eta_sec, 3600)
        m = rem // 60
        eta_human = f"약 {h}시간 {m}분" if h else f"약 {m}분"

    # 워커 상태
    worker_info = _systemctl_show("charisg-pa-group-worker.service")
    timer_info = _systemctl_show("charisg-pa-group-worker.timer")
    worker_active = worker_info.get("ActiveState") == "active"

    # 다음 가동 — systemctl 버전에 따라 NextElapseUSecRealtime 가 마이크로초(숫자) 또는
    # "Tue 2026-06-02 22:00:00 UTC" 형식 문자열로 옴. 둘 다 처리 (2026-06-02 버그수정: 문자열형 → null 이던 것).
    from datetime import datetime, timezone
    next_trigger = (timer_info.get("NextElapseUSecRealtime") or "").strip()
    next_trigger_iso = None
    if next_trigger and next_trigger not in ("0", "n/a", "N/A"):
        try:
            if next_trigger.isdigit():
                next_trigger_iso = datetime.fromtimestamp(int(next_trigger) / 1_000_000, tz=timezone.utc).isoformat()
            else:
                _dt = datetime.strptime(next_trigger.replace(" UTC", "").strip(), "%a %Y-%m-%d %H:%M:%S")
                next_trigger_iso = _dt.replace(tzinfo=timezone.utc).isoformat()
        except Exception:
            next_trigger_iso = None

    # ★진행률 — queued(대기) 기준. skipped/error 는 '처리완료'이므로 미완으로 치면 안 됨.
    # (버그수정 2026-06-02: done/total 이라 skipped 를 미완 취급 → 큐 0인데도 '워커 대기 88%'로 오표시)
    processed = max(total - queued, 0)
    return {
        "in_flight": in_flight,
        "rate": {
            "last_5min_done": rate_done,
            "last_5min_skipped": rate_skipped,
            "per_minute": round(per_min, 1),
            "per_hour": int(per_min * 60),
        },
        "eta_seconds": eta_sec,
        "eta_human": eta_human,
        "worker_active": worker_active,
        "queue_total": total,
        "queue_done": processed,           # 처리완료(done+skipped+error). 등록성공은 registered_ok.
        "registered_ok": done_total,       # 그중 실제 그룹등록 성공 건수
        "queue_remaining": queued,
        "progress_pct": round(processed / total * 100, 1) if total else 100.0,
        "next_trigger": next_trigger_iso,
    }


# ─── 4. 에러 로그 ─────────────────────────────────────────────────
@router.get("/errors")
def get_errors(
    user: dict = Depends(current_user),
    stage: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    with get_db() as conn:
        # UNION 으로 단계별 에러
        q_listings = """
          SELECT 'uploading' AS stage, p.asin, l.error_message AS msg,
                 COALESCE(l.last_synced_at, '') AS ts
          FROM listings_pa l JOIN products p ON p.id = l.product_id
          WHERE l.channel='coupang' AND l.status='excluded' AND l.error_message IS NOT NULL
        """
        q_group = """
          SELECT 'group_queue' AS stage, q.parent_asin AS asin,
                 COALESCE(q.skip_reason, q.error_message) AS msg,
                 COALESCE(q.finished_at, '') AS ts
          FROM group_registration_queue q
          WHERE q.status IN ('skipped','error')
            AND COALESCE(q.skip_reason, q.error_message) IS NOT NULL
        """
        q_batch = """
          SELECT 'batch' AS stage, '' AS asin, bj.error_message AS msg,
                 COALESCE(bj.finished_at, '') AS ts
          FROM batch_jobs bj
          WHERE bj.status='error' AND bj.error_message IS NOT NULL
        """
        q_sheet = """
          SELECT 'sheet' AS stage, sq.sheet_label AS asin, sq.error_message AS msg,
                 COALESCE(sq.finished_at, '') AS ts
          FROM sheet_queue sq
          WHERE sq.status='error' AND sq.error_message IS NOT NULL
        """
        all_q = f"""
          SELECT * FROM (
            {q_listings}
            UNION ALL {q_group}
            UNION ALL {q_batch}
            UNION ALL {q_sheet}
          )
          {('WHERE stage = ?' if stage else '')}
          ORDER BY ts DESC
          LIMIT ? OFFSET ?
        """
        params: list = []
        if stage:
            params.append(stage)
        params.extend([limit, offset])
        rows = conn.execute(all_q, params).fetchall()

    return {
        "errors": [
            {
                "stage": r["stage"], "asin": r["asin"] or "",
                "msg": (r["msg"] or "")[:300], "ts": r["ts"],
            }
            for r in rows
        ],
        "stage_filter": stage,
        "limit": limit, "offset": offset,
    }
