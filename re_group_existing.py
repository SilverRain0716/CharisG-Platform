"""기존 listed 쿠팡 상품 재그룹화 드라이버 (저우선/유휴, 안전필터, DB락격리). 2026-06-03.

판매중인 것만 재그룹 — 판매중지/임시저장/품절/검토중/반려/삭제 + 제외목록(WING 수동중단)은 절대 재등록 안 함.
★DB락 격리: regroup_scan/regroup_exclude 는 별도 regroup.db. purchase.db 는 읽기전용(WAL 동시읽기)으로만.

  scan    : 등록순 listed child → 부모발굴(SP-API) + 판매중 스냅샷(status='listed' AND 쿠팡 statusName='승인완료' AND 재고>0, 1회조회) → regroup.db 저장
  enqueue : ★스캔완료 후. parent별: listed 자식 전부 selling AND 미제외 AND 주문0 AND 2+ → group_registration_queue 투입 (라이브, 감독)
  exclude : regroup_exclude 에 ASIN 추가
  status  : 현황

유휴게이트: 청소(최우선) 큐 있으면 SCAN 대기. --force 무시.
사용: python re_group_existing.py scan|enqueue|exclude|status [--limit N] [--apply] [--force]
"""
import sys, time, sqlite3
from dotenv import load_dotenv
load_dotenv(".env"); load_dotenv("backend/purchase/.env")

BASE = "/home/ubuntu/CharisG-Platform/charisg-platform"
PURCHASE = f"{BASE}/backend/purchase/purchase.db"
HOT = f"{BASE}/backend/purchase/purchase_hot.db"
REGROUP = f"{BASE}/regroup.db"

ARGV = sys.argv
MODE = ARGV[1] if len(ARGV) > 1 else "status"
APPLY = "--apply" in ARGV
FORCE = "--force" in ARGV
LIMIT = 100000
for i, a in enumerate(ARGV):
    if a == "--limit" and i + 1 < len(ARGV):
        LIMIT = int(ARGV[i + 1])

def log(m): print(m, flush=True)

def rdb():  # regroup.db (읽기/쓰기, 경쟁 없음)
    c = sqlite3.connect(REGROUP, timeout=60); c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=60000")
    c.execute("CREATE TABLE IF NOT EXISTS regroup_scan (product_id INTEGER PRIMARY KEY, asin TEXT, parent_asin TEXT, kind TEXT, selling INTEGER DEFAULT 0, scanned_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    c.execute("CREATE TABLE IF NOT EXISTS regroup_exclude (asin TEXT PRIMARY KEY, reason TEXT, added_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    return c

def pdb_ro():  # purchase.db 읽기전용 (WAL 동시읽기, 락 안 잡음)
    c = sqlite3.connect(f"file:{PURCHASE}?mode=ro", uri=True, timeout=60); c.row_factory = sqlite3.Row
    return c

def cleaning_busy():
    with pdb_ro() as c:
        return c.execute("SELECT COUNT(*) n FROM group_registration_queue WHERE status='queued' AND sheet_id LIKE 'cleaning:%'").fetchone()["n"] > 0

# ───────── SCAN ─────────
def discover(asin):
    from backend.purchase.services.image_downloader import fetch_product_info_sp_api
    for attempt in range(3):
        try: r = fetch_product_info_sp_api(asin)
        except Exception: r = {}
        if r.get("is_parent"): return "parent", asin
        if r.get("parent_asin"): return "child", r["parent_asin"]
        if r.get("title"): return "single", None
        time.sleep(1.5 * (attempt + 1))
    return "fail", None

def is_selling(cpid, csn):
    if cpid:
        try:
            from backend.purchase.services.coupang_service import get_seller_product
            d = (get_seller_product(str(cpid)) or {}).get("data") or {}
            if d:
                if d.get("statusName") != "승인완료": return False
                return max([int(it.get("maximumBuyCount") or 0) for it in (d.get("items") or [])], default=0) > 0
        except Exception:
            pass
    return csn == "승인완료"

def run_scan():
    if not FORCE and cleaning_busy():
        log("청소(최우선) 큐 busy → 유휴 대기(저우선)."); return
    rc = rdb()
    done_ids = {r["product_id"] for r in rc.execute("SELECT product_id FROM regroup_scan")}
    with pdb_ro() as pc:
        rows = pc.execute(
            """SELECT p.id pid, p.asin, MIN(l.id) lid, MAX(l.channel_product_id) cpid,
                      MAX(CASE WHEN l.status='listed' THEN 1 ELSE 0 END) any_listed, MAX(l.coupang_status_name) csn
               FROM listings_pa l JOIN products p ON p.id=l.product_id
               WHERE l.channel='coupang' AND p.asin IS NOT NULL
               GROUP BY p.id ORDER BY MIN(l.id) ASC""").fetchall()
    rows = [r for r in rows if r["pid"] not in done_ids][:LIMIT]
    log(f"SCAN 대상 {len(rows)}건 (누적완료 {len(done_ids)})")
    n = {"parent":0,"child":0,"single":0,"fail":0}; sell = 0
    for i, r in enumerate(rows, 1):
        if not FORCE and i % 25 == 0 and cleaning_busy():
            log(f"  청소 busy → 중단(재개가능). 진행 {i}"); break
        kind, par = discover(r["asin"])
        n[kind] += 1
        selling = 1 if (r["any_listed"] and is_selling(r["cpid"], r["csn"])) else 0
        sell += selling
        rc.execute("INSERT OR REPLACE INTO regroup_scan (product_id,asin,parent_asin,kind,selling) VALUES (?,?,?,?,?)",
                   (r["pid"], r["asin"], par, kind, selling))
        rc.commit()
        time.sleep(0.55)
        if i % 100 == 0: log(f"  {i}/{len(rows)} {n} 판매중 {sell}")
    log(f"SCAN 완료/중단: {n} 판매중 {sell}")

# ───────── ENQUEUE ─────────
def scan_remaining():
    rc = rdb(); done = {r["product_id"] for r in rc.execute("SELECT product_id FROM regroup_scan")}
    with pdb_ro() as pc:
        allp = {r["pid"] for r in pc.execute("SELECT p.id pid FROM listings_pa l JOIN products p ON p.id=l.product_id WHERE l.channel='coupang' AND p.asin IS NOT NULL GROUP BY p.id")}
    return len(allp - done)

def run_enqueue():
    rem = scan_remaining()
    if rem > 0 and not FORCE:
        log(f"★스캔 미완료(미스캔 {rem}) → ENQUEUE 보류(부분뷰 위험). 스캔완료 후/또는 --force."); return
    rc = rdb()
    rc.execute(f"ATTACH DATABASE '{HOT}' AS hot")
    fams = [dict(f) for f in rc.execute(
        """SELECT parent_asin, COUNT(*) listed_cnt, SUM(selling) selling_cnt,
                  SUM(CASE WHEN asin IN (SELECT asin FROM regroup_exclude) THEN 1 ELSE 0 END) excl,
                  SUM((SELECT COUNT(*) FROM hot.orders o WHERE o.asin_cache=rs.asin OR o.child_asin=rs.asin)) ord_cnt
           FROM regroup_scan rs WHERE kind='child' AND parent_asin IS NOT NULL GROUP BY parent_asin""").fetchall()]
    cand = [f for f in fams if f["listed_cnt"] >= 2 and f["selling_cnt"] == f["listed_cnt"] and (f["excl"] or 0) == 0 and (f["ord_cnt"] or 0) == 0]
    log(f"패밀리 {len(fams)} | 후보(2+ 전부판매중·미제외·주문0): {len(cand)}")
    if not APPLY:
        for f in cand[:15]: log(f"  {f['parent_asin']}: listed {f['listed_cnt']} 판매중 {f['selling_cnt']}")
        log("(DRY-RUN. --apply 로 큐 투입)"); return
    # 큐 INSERT → purchase.db (write, busy_timeout). 중복 dedup.
    pw = sqlite3.connect(PURCHASE, timeout=120); pw.row_factory = sqlite3.Row; pw.execute("PRAGMA busy_timeout=120000")
    now = pw.execute("SELECT datetime('now')").fetchone()[0]; enq = 0
    for f in cand:
        pa = f["parent_asin"]
        if pw.execute("SELECT 1 FROM group_registration_queue WHERE parent_asin=? AND status IN ('queued','pre_scanning','registering','done','done_singles')", (pa,)).fetchone():
            continue
        pw.execute("INSERT INTO group_registration_queue (parent_asin,sheet_id,status,requested,channels,queued_at) VALUES (?,?,'queued',1,'coupang',?)", (pa, "regroup:existing", now))
        enq += 1
    pw.commit()
    log(f"큐 투입 {enq}건 (regroup:existing). 드레인이 등록+단일 archive.")

def run_exclude():
    asins = [a for a in ARGV[2:] if a.startswith("B0")]
    if not asins: log("사용: exclude B0XXX ..."); return
    rc = rdb()
    for a in asins: rc.execute("INSERT OR IGNORE INTO regroup_exclude (asin,reason) VALUES (?,'수동')", (a,))
    rc.commit(); log(f"제외목록 추가 {len(asins)}건")

def run_status():
    rc = rdb()
    sc = {r["kind"]: r["c"] for r in rc.execute("SELECT kind, COUNT(*) c FROM regroup_scan GROUP BY kind")}
    ex = rc.execute("SELECT COUNT(*) c FROM regroup_exclude").fetchone()["c"]
    with pdb_ro() as pc:
        eq = pc.execute("SELECT COUNT(*) c FROM group_registration_queue WHERE sheet_id='regroup:existing'").fetchone()["c"]
    log(f"regroup_scan: {sc} | 미스캔: {scan_remaining()} | 제외목록: {ex} | regroup큐: {eq}")

{"scan":run_scan,"enqueue":run_enqueue,"exclude":run_exclude,"status":run_status}.get(MODE, run_status)()
