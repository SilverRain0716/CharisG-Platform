# -*- coding: utf-8 -*-
"""램프 그룹 라우팅 — 판별기(classify) + 대기큐(enqueue) + 그룹워커(process_pending_groups).
   램프는 단품만 즉시 등록(빠름), 그룹은 큐에 적재→별도 워커가 자기 속도로 멀티옵션 등록.
   이미지 다운로드 타임아웃으로 행(hang) 방지."""
import os, sys, json, sqlite3, asyncio, time, re
BASE = "/home/ubuntu/CharisG-Platform/charisg-platform"
if BASE not in sys.path: sys.path.insert(0, BASE)
import logging; logging.disable(logging.WARNING)
# ★2026-08-02: 구계정 병행 지원 — 실행 환경의 COUPANG_ACTIVE 를 따른다(기본 new).
_ACCT = (__import__('os').environ.get('PA_IMPORT_ACCOUNT') or 'new').strip().lower()
import requests
from backend.purchase.database import get_db

_DB = f"{BASE}/backend/purchase/purchase.db"
_QUEUE = f"{BASE}/scripts/migrate/state/pending_groups.json"


# ── 판별기 ──
def _cached_children(parent):
    with get_db() as c:
        r = c.execute("SELECT child_asins_json FROM variation_groups WHERE parent_asin=?", (parent,)).fetchone()
    if r and r["child_asins_json"]:
        try: return json.loads(r["child_asins_json"])
        except Exception: return None
    return None


def classify(pid):
    """(verdict, parent_asin, n). 'group'=변형>=2, 'single'=그외. variation_groups 캐시 우선."""
    with get_db() as c:
        row = c.execute("SELECT parent_asin FROM products WHERE id=?", (pid,)).fetchone()
    if not row or not row["parent_asin"]:
        return ("single", None, 0)
    parent = row["parent_asin"]
    ch = _cached_children(parent)
    if ch is None:
        try:
            from backend.purchase.services.sp_api_group_discovery import discover_group
            ch = (discover_group(parent) or {}).get("child_asins") or []
        except Exception:
            ch = []
    n = len(set(ch))
    # 변형 2~100 만 그룹. 100 초과(과대그룹/노이즈, 예 131·240·1510)는 인라인 SP-API 적재 비용
    # (자식당 1 RPS → 수분 + DB 락 충돌) + 어차피 옵션 한도(MAX_GROUP_OPTIONS=100)로 단품폴백 → 바로 단품.
    return (("group" if 2 <= n <= 100 else "single"), parent, n)


# ── 대기큐 ──
def _load_queue():
    try: return json.load(open(_QUEUE))
    except Exception: return []


def _save_queue(q):
    os.makedirs(os.path.dirname(_QUEUE), exist_ok=True)
    json.dump(q, open(_QUEUE, "w"), ensure_ascii=False)


def enqueue_group(parent, trigger_pid=None):
    """그룹 parent를 대기큐에 적재(dedup). 이미 있으면 무시."""
    q = _load_queue()
    if any(e.get("parent") == parent for e in q):
        return False
    q.append({"parent": parent, "trigger_pid": trigger_pid, "status": "pending"})
    _save_queue(q)
    return True


# ── 그룹 워커 (heavy, 별도 실행) ──
def _keys():
    ks = []
    for n in ("GEMINI_API_KEY_5", "GEMINI_API_KEY_FALLBACK", "GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3"):
        v = os.environ.get(n)
        if v and v not in ks: ks.append(v)
    return ks


# ★번역은 단일 출처 translate_service 공유 — 리스팅 경로/백필 잡 드리프트 방지(2026-07-01).
#   clean_ko_title/is_title_garbage 도 동일 모듈. (구 인라인 구현 제거)
from backend.purchase.services.translate_service import (  # noqa: E402
    translate_ko, clean_ko_title, is_title_garbage)
from backend_shared.ai.service import generate_seo  # noqa: E402  # seo_tags 임포트시 생성(2026-07-24)


def _dl_with_timeout(pid, ij, sec=45):
    from backend.purchase.services.image_downloader import download_product_images
    async def _run(): return await asyncio.wait_for(download_product_images(pid, ij), timeout=sec)
    try: asyncio.run(_run()); return True
    except Exception: return False


def prepare_and_register_group(parent_asin, discover_only=False):
    from backend.purchase.services.group_lister import (
        fetch_and_insert_children, register_new_group_listing, _group_path_blocking_gates,
        extend_master_with_group)
    from backend.purchase.services.image_classifier import classify_reliable
    from backend.purchase.services.pricing_service_pa import calculate_sale_krw
    from backend.purchase.services.coupang_service import coupang_account
    # ★동시등록 클레임(2026-07-25): 병렬 워커가 같은 부모를 동시 등록하는 중복 방지.
    #   INSERT OR IGNORE 원자성 — 먼저 잡은 워커만 진행, 나머지는 claimed_other 스킵.
    #   Stage2가 실행 시작 시 pa_reg_claims 를 비움(실패분은 다음 실행에서 재시도).
    # ★버그수정(2026-07-26): discover_only 는 claim 하지 않음. 발굴전용은 자식삽입만(중복위험 없음)인데
    #   claim 하면 기차식(발굴→등록)에서 발굴이 부모를 선점해 뒤이은 등록이 claimed_other 로 전부 스킵됨.
    #   claim 은 "동시 register 워커 중복등록" 방지용이므로 register 경로에서만 잡는다.
    if not discover_only:
        _clm = sqlite3.connect(_DB, timeout=180); _clm.execute('PRAGMA busy_timeout=180000')
        try:
            _clm.execute("CREATE TABLE IF NOT EXISTS pa_reg_claims(parent_asin TEXT PRIMARY KEY, claimed_at TEXT)")
            _cur = _clm.execute("INSERT OR IGNORE INTO pa_reg_claims(parent_asin, claimed_at) VALUES(?, datetime('now'))", (parent_asin,))
            _clm.commit(); _got = (_cur.rowcount == 1)
        finally:
            _clm.close()
        if not _got:
            return {'registered': False, 'status': 'claimed_other', 'reason': '동시 워커 처리중', 'spid': None, 'n_options': None}
    # ★멱등(2026-07-24): 이미 등록된 그룹이면 발굴·등록 모두 스킵 — 청크/재스윕/재실행 중복등록 방지.
    #   master 자식만 listings_pa 행을 받아 precut이 형제를 못 걸러서, 여기서 부모 기준으로 차단.
    _cid = sqlite3.connect(_DB, timeout=180); _cid.execute('PRAGMA busy_timeout=180000')
    _reg = _cid.execute("SELECT 1 FROM listings_pa l JOIN products p ON p.id=l.product_id "
        "WHERE p.parent_asin=? AND l.channel='coupang' AND l.coupang_account=? "
        "AND l.channel_product_id IS NOT NULL AND COALESCE(l.status,'')!='removed' LIMIT 1", (parent_asin, _ACCT)).fetchone()
    _cid.close()
    # ★2026-08-01: 부모가 등록됐어도 '미등록 자식'이 남아 있으면 extend(옵션추가) 로 진행해야 한다.
    #   기존엔 무조건 스킵해서 전체의 53.8%(자식 26,158건)가 영원히 등록되지 않았다.
    #   완전히 등록된 그룹(미등록 자식 0)만 멱등 스킵한다.
    if _reg:
        _cpend = sqlite3.connect(_DB, timeout=180); _cpend.execute('PRAGMA busy_timeout=180000')
        # ★2026-08-03: listings_pa 만 보면 안 된다. 그룹 등록 시 listings_pa 행을 받는 건
        #   마스터 자식 1개뿐이고, 나머지 자식은 listing_options 에 기록된다.
        #   그래서 이미 옵션으로 등록된 자식이 "미등록"으로 잡혀 extend 를 재시도하고,
        #   쿠팡이 "중복된 옵션값이 있습니다"(PUT 400)로 거부 → 단품 폴백으로 흩어졌다.
        #   (실측 B07MS1CB6Y: 자식 25개 전부 옵션 등록 완료인데 24개가 미등록으로 판정)
        _pend = _cpend.execute(
            "SELECT COUNT(*) FROM products p WHERE p.parent_asin=? AND p.status='draft' "
            "AND p.cost_usd>0 AND p.title_ko IS NOT NULL AND p.title_ko!='' "
            "AND NOT EXISTS(SELECT 1 FROM listings_pa l2 WHERE l2.product_id=p.id AND l2.channel='coupang') "
            # child_product_id 를 먼저 타야 한다. JOIN 형태로 쓰면 SQLite 가 listings_pa 를
            # channel 인덱스로 훑어(14만행) 단건 407ms → 재구성 후 1.4ms (idx_listing_options_child).
            "AND NOT EXISTS(SELECT 1 FROM listing_options o "
            "               WHERE o.child_product_id=p.id "
            "                 AND EXISTS(SELECT 1 FROM listings_pa l3 WHERE l3.id=o.listing_id "
            "                            AND l3.channel='coupang' AND COALESCE(l3.status,'')!='removed'))",
            (parent_asin,)).fetchone()[0]
        _cpend.close()
        if not _pend:
            return {'registered': False, 'status': 'already_registered', 'reason': '그룹 이미 등록(미등록 자식 0)',
                    'spid': None, 'n_options': None}
        print(f"[group-extend] {parent_asin} 부모 등록됨 + 미등록 자식 {_pend}건 → extend 진행", flush=True)
    # ★효율: 정책 차단(KC/브랜드/금지성분 등)은 자식 SP-API 적재+이미지 다운로드(수십분) 전에 먼저 확인.
    #   register_new_group_listing 내부(1582)에도 같은 게이트가 있지만 거긴 무거운 prep 이후라
    #   차단건이 헛돈 수십분을 쓰고 실패했음(pid448=27분). 앞단에서 즉시 빠진다.
    _blk = _group_path_blocking_gates(parent_asin, channel="coupang")
    if _blk:
        return {"registered": False, "status": "blocked", "reason": _blk, "spid": None, "n_options": None}
    fetch_and_insert_children(parent_asin)
    if discover_only:
        # ★발굴 전용(2026-07-24): variation_groups 채우고 자식 삽입까지만. 등록(이미지·AI·업로드)은 후속 등록패스에서.
        from backend.purchase.services.sp_api_group_discovery import discover_group as _dg
        _dg(parent_asin)                          # classify_asin 이 VG 미채움 → 여기서 upsert
        fetch_and_insert_children(parent_asin)    # 실제 자식 INSERT (VG 채워진 뒤)
        _c0 = sqlite3.connect(_DB, timeout=180); _c0.execute('PRAGMA busy_timeout=180000')
        _n0 = _c0.execute('SELECT COUNT(*) FROM products WHERE parent_asin=?', (parent_asin,)).fetchone()[0]
        _c0.close()
        return {'registered': False, 'status': 'discovered', 'reason': None, 'spid': None, 'n_options': _n0}
    con = sqlite3.connect(_DB, timeout=180); con.execute("PRAGMA busy_timeout=180000")
    rows = con.execute("SELECT id, asin, images_json, title_en, title_ko, cost_usd, sale_price_krw FROM products WHERE parent_asin=?", (parent_asin,)).fetchall()
    child_pids = [r[0] for r in rows]

    # ★자식별 처리 병렬화 (2026-06-30, K=5): 다운로드+분류+번역+가격은 자식간 독립 → ThreadPool.
    #   SP-API fetch_and_insert_children(위, 1RPS)는 직렬 유지(외부 하드제한). Gemini는 키로테이션+재시도
    #   내장이라 동시호출 429는 자동 흡수. 스레드별 독립 sqlite 커넥션(공유 금지)+busy_timeout.
    #   대형그룹(26변형) 자식루프 ~15분 → ~3분.
    def _prep_child(row):
        pid, asin, ij, ten, tko, cost, sale = row
        lc = sqlite3.connect(_DB, timeout=180); lc.execute("PRAGMA busy_timeout=180000")
        try:
            if ij and ij not in ("[]", ""):
                cnt = lc.execute("SELECT COUNT(*) FROM image_cache WHERE product_id=?", (pid,)).fetchone()[0]
                if cnt == 0:
                    _dl_with_timeout(pid, ij)            # ★타임아웃 (행 방지)
            try:
                classify_reliable(pid, force=False)   # ★캐시 우선(2026-07-01): 재처리·재시작 시 재분류 스킵. 신규만 실호출
            except Exception:
                pass
            if (not sale or sale < 1000) and cost and cost > 0:
                try:
                    r = calculate_sale_krw(cost_usd=float(cost), channel="coupang"); sk = (r.get("sale_krw") if isinstance(r, dict) else 0) or 0
                    if sk >= 1000:
                        lc.execute("UPDATE products SET sale_price_krw=? WHERE id=?", (int(sk), pid)); lc.commit()
                except Exception:
                    pass
            # ★검증 게이트(2026-07-01): title_ko 없거나 오염(##/xx/에코)이면 재번역, 오염 결과는 저장 안 함.
            if is_title_garbage(tko):
                ko = translate_ko(ten)
                if ko and not is_title_garbage(ko):
                    lc.execute("UPDATE products SET title_ko=? WHERE id=?", (ko, pid)); lc.commit()
        finally:
            lc.close()
    from concurrent.futures import ThreadPoolExecutor
    # ★병렬도 3→5 (2026-07-01): flash-lite 전환(별도 쿼터+키 로테이션)으로 동시 버스트 흡수 가능 →
    #   위기 때 낮춘 3을 복원. 자식 prep(다운로드+분류+번역)이 병목이라 병렬도가 직접 처리량에 반영.
    with ThreadPoolExecutor(max_workers=2) as _ex:  # 2026-07-24 AI키 제한으로 5→2
        list(_ex.map(_prep_child, rows))
    con.commit()
    # ★seo_tags/seo_title 생성 (2026-07-24: 메인스레드 — ThreadPool내 asyncio.run은 공유 AI클라이언트 손상시켜
    #   병렬 translate_ko 실패시킴. 메인스레드 asyncio.run은 단일 이벤트루프라 안전. 게이트 통과 위해 임포트시 채움).
    for _cpid in child_pids:
        try:
            _r = con.execute("SELECT title_ko, seo_tags FROM products WHERE id=?", (_cpid,)).fetchone()
            _tk = _r[0] if _r else None; _st = _r[1] if _r else None
            if _tk and re.search(r"[가-힣]", _tk) and (not _st or _st in ("", "[]")):
                _seo = asyncio.run(generate_seo(product_name=_tk, category="", market="KR", platform="coupang", description=""))
                _tags = _seo.get("tags") or _seo.get("keywords") or []
                if _tags:
                    con.execute("UPDATE products SET seo_tags=?, seo_title=? WHERE id=?",
                                (json.dumps(_tags, ensure_ascii=False), _seo.get("optimized_title") or _tk, _cpid)); con.commit()
        except Exception:
            pass
    # ★대표이미지 커버 — 깨끗한 사진이 0장(합성/인물/라이프스타일뿐)인 자식만 Gemini design_cut 생성.
    #   0장이면 select_representative_image가 only_size 폴백(라이프스타일 흰배경)으로 대표를 뽑아 부적절 →
    #   generate_shared_editorial→gen_design_cut(Nano Banana)으로 깨끗한 디자인컷 생성→대표로 사용.
    #   ★==0 만(1장 이상은 rep_nuki가 이미 정상 = 사장 확정 A'). 0-clean은 드물어 속도영향 최소.
    #   직렬 실행(t3.micro OOM 방지), force=False=캐시. 단품경로(migrate_old prep) 대응. (2026-07-01)
    try:
        from backend.purchase.services.coupang_lister import _get_product_images as _gpi_dc
        import sys as _s_dc, os as _os_dc
        _HERE_DC = _os_dc.path.dirname(_os_dc.path.abspath(__file__))   # scripts/migrate (detail_b.py 위치)
        if _HERE_DC not in _s_dc.path:
            _s_dc.path.insert(0, _HERE_DC)
        from detail_b import generate_shared_editorial as _gse
        from backend.purchase.services.design_cut import gen_design_cut as _gdc, is_zero_clean as _izc
        for _cpid in child_pids:
            try:
                if _izc(_cpid):   # ★진짜 0-clean(photo 0장). _gpi_dc는 폴백1장 반환해 ==0이 안 걸림(2026-07-01 버그수정)
                    _gdc(_cpid, force=False)   # AI 디자인컷만. Gemini 소진 시 rembg 미사용(그룹 홀드는 후속)
                    _gse(_cpid, force=False)   # 상세 에디토리얼 섹션
            except Exception:
                pass
    except Exception as _e_dc:
        pass
    os.environ["PA_SKIP_GEMINI"] = "1"
    with coupang_account(_ACCT):
        # ★2026-08-01: 부모가 이미 등록된 그룹(전체의 53.8%)은 신규생성이 아니라
        #   기존 마스터에 옵션추가(extend)로 가야 한다. mode='auto' 가 master 유무로 자동분기.
        out = extend_master_with_group(parent_asin, channels=["coupang"], dry_run=False,
                                       mode="auto", requested=False, skip_archive=True,
                                       fast_detail=True)
    spid = status = None; nopt = None
    # ★2026-08-01: register 모드는 list, extend 모드는 dict 를 반환한다.
    #   기존엔 list 만 가정해 dict 를 순회하면 키(str)에 .get() 을 호출해 AttributeError 로 터졌다.
    _ch = out.get("channels", {}).get("coupang")
    _items = _ch if isinstance(_ch, list) else ([_ch] if isinstance(_ch, dict) else [])
    for sp in _items:
        if not isinstance(sp, dict):
            continue
        # extend 는 action(extended/dry_run/skip/error), register 는 status 를 쓴다.
        status = sp.get("status") or sp.get("action")
        if status in ("registered", "single_fallback", "extended"):
            spid = sp.get("seller_product_id") or sp.get("channel_product_id") or sp.get("spid")
            nopt = sp.get("options_count") or sp.get("added")
    # ★그룹 실패 시 자식 단품폴백 (2026-07-11): SP-API 변형 노후화(자식0개) 등으로 그룹을 못 만들면
    #   products 자식들을 개별 단품으로 등록. 정책 blocked 는 제외(정당차단 보존).
    if status not in ("registered", "single_fallback", "extended") and child_pids \
            and "blocked" not in str(out.get("error", "")):
        try:
            _fb = _children_single_fallback(child_pids, parent_asin)
        except Exception as _e_fb:
            _fb = {"listed": 0, "skipped": 0, "failed": len(child_pids), "err": str(_e_fb)[:80]}
        if _fb.get("listed", 0) > 0 or _fb.get("skipped", 0) > 0:
            return {"registered": False, "status": "single_fallback", "spid": None,
                    "n_options": _fb.get("listed", 0), "child_pids": child_pids,
                    "fallback": _fb, "orig_error": str(out.get("error", ""))[:120]}
    return {"registered": status == "registered", "status": status, "spid": spid, "n_options": nopt, "child_pids": child_pids}


def _children_single_fallback(child_pids, parent_asin=""):
    """그룹 등록 실패(SP-API 변형 노후화·자식0개 등) 시 자식들을 개별 단품으로 등록.
       무등록보다 낫다. list_product 가드로 이미등록/중복 자동 스킵, refresh_dual_price 로 배송비포함가."""
    from backend.purchase.services.coupang_lister import list_product, _get_product_images
    from backend.purchase.services.dual_pricing import refresh_dual_price
    from backend.purchase.services.coupang_service import coupang_account
    con = sqlite3.connect(_DB, timeout=180); con.execute("PRAGMA busy_timeout=180000")
    listed = skipped = failed = 0
    with coupang_account(_ACCT):
        for pid in child_pids:
            try:
                r = con.execute("SELECT asin, images_json FROM products WHERE id=?", (pid,)).fetchone()
                imgs = _get_product_images(pid)
                if not imgs and r and r[1] and r[1] not in ("[]", ""):
                    _dl_with_timeout(pid, r[1])           # 이미지 캐시 채움(단품등록 필수)
                    imgs = _get_product_images(pid)
                if not imgs:
                    failed += 1; continue
                if r and r[0]:
                    try: refresh_dual_price(pid, r[0])     # 배송비포함 정상가
                    except Exception: pass
                out = list_product(pid, image_urls=imgs, requested=False)
                if out.get("ok"): listed += 1
                elif out.get("skip"): skipped += 1
                else: failed += 1
            except Exception:
                failed += 1
    con.close()
    return {"listed": listed, "skipped": skipped, "failed": failed}


def process_pending_groups(limit=5):
    q = _load_queue()
    todo = [e for e in q if e.get("status") == "pending"][:limit]
    print("그룹 큐 처리:", len(todo), "/ 대기", sum(1 for e in q if e.get("status") == "pending"))
    for e in todo:
        try:
            res = prepare_and_register_group(e["parent"])
            e["status"] = "done" if res.get("registered") else ("fallback" if res.get("status") == "single_fallback" else "failed")
            e["result"] = {k: res.get(k) for k in ("spid", "n_options", "status")}
            print("  %s → %s spid=%s 옵션=%s" % (e["parent"], e["status"], res.get("spid"), res.get("n_options")))
        except Exception as ex:
            e["status"] = "error"; e["result"] = str(ex)[:120]
            print("  %s → error %s" % (e["parent"], str(ex)[:80]))
        _save_queue(q)
    return q


if __name__ == "__main__":
    from dotenv import load_dotenv; load_dotenv(f"{BASE}/.env")
    os.environ["COUPANG_ACTIVE"] = _ACCT
    cmd = sys.argv[1] if len(sys.argv) > 1 else "worker"
    if cmd == "worker":
        process_pending_groups(int(sys.argv[2]) if len(sys.argv) > 2 else 5)
    elif cmd == "queue":
        print(json.dumps(_load_queue(), ensure_ascii=False, indent=1))
