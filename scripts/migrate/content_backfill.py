# -*- coding: utf-8 -*-
"""콘텐츠 백필 — 리스팅 전에 AI 파생값(현재: title_ko)을 미리 채워 hot-path에서 분리.

설계(사용자 승인 방향, 2026-07-01):
- 싸고·재사용되고·안정적인 파생값(title_ko 등)은 미리 백필해 두고 리스팅은 DB 읽기.
- 검증 게이트(is_title_garbage)로 오염 결과는 저장 안 함 → 라이브 유출 차단(## / xx / 프롬프트에코 사고 예방).
- 범위: 승급 큐(리스팅 임박분)만. 전체 풀 낭비 금지.
- 이미지 생성(design_cut/infographic)은 비싸고 부분집합에만 필요 → 지연(lazy) 유지.
- 단일 출처 translate_service 공유(리스팅 경로와 드리프트 방지).

CLI:
  python content_backfill.py titles <N>   # 승급 큐 상위 N의 title_ko 백필
  python content_backfill.py stats         # 큐 title_ko 커버리지
"""
import os
import sys
import json
import time
import sqlite3
from concurrent.futures import ThreadPoolExecutor

BASE = "/home/ubuntu/CharisG-Platform/charisg-platform"
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from backend.purchase.services.translate_service import translate_ko, is_title_garbage

_DB = f"{BASE}/backend/purchase/purchase.db"
_PIDS = f"{BASE}/scripts/migrate/state/old_migrate_pids.json"
_RES = f"{BASE}/scripts/migrate/state/old_migrate_result.json"


def _con():
    c = sqlite3.connect(_DB, timeout=180)
    c.execute("PRAGMA busy_timeout=180000")
    c.row_factory = sqlite3.Row
    return c


def ensure_title_ko(pid, con=None, force=False):
    """title_ko가 유효하면 반환, 아니면 title_en/sp_title_en에서 번역해 저장 후 반환.
    실패 시 None(호출측이 영문 원제 유지). ★리스팅 경로가 공유하는 단일 진입점."""
    own = con is None
    if own:
        con = _con()
    try:
        row = con.execute(
            "SELECT title_ko, title_en, sp_title_en FROM products WHERE id=?", (pid,)
        ).fetchone()
        if not row:
            return None
        cur = row["title_ko"]
        if not force and cur and not is_title_garbage(cur):
            return cur                      # 이미 유효 — 재계산 없음(백필 목적)
        src = row["title_en"] or row["sp_title_en"]
        if not src:
            return cur if cur and not is_title_garbage(cur) else None
        new = translate_ko(src)
        if new and not is_title_garbage(new):
            con.execute("UPDATE products SET title_ko=? WHERE id=?", (new, pid))
            con.commit()
            return new
        return None                          # 번역 실패 — title_ko 갱신 안 함
    finally:
        if own:
            con.close()


def _promotion_queue_pids():
    """승급 큐(리스팅 임박분) = old_migrate_pids 중 아직 result에 없는 순서."""
    try:
        pids = json.load(open(_PIDS))
    except Exception:
        return []
    done = set()
    try:
        done = {r["pid"] for r in json.load(open(_RES))}
    except Exception:
        pass
    return [p for p in pids if p not in done]


def backfill_titles(limit=500, max_workers=5):
    """승급 큐 상위 limit 중 title_ko 없거나 오염된 것만 번역해 채움. 병렬→일괄 UPDATE."""
    con = _con()
    todo = _promotion_queue_pids()[:limit]
    # title_ko 없거나 오염된 것만
    need = []
    for pid in todo:
        r = con.execute("SELECT title_ko, title_en, sp_title_en FROM products WHERE id=?", (pid,)).fetchone()
        if not r:
            continue
        if (r["title_en"] or r["sp_title_en"]) and (is_title_garbage(r["title_ko"])):
            need.append((pid, r["title_en"] or r["sp_title_en"], r["title_ko"]))
    print(f"승급큐 상위 {len(todo)} 중 백필대상(무/오염 title_ko) {len(need)}", flush=True)

    def work(item):
        pid, src, old = item
        new = translate_ko(src)
        if new and not is_title_garbage(new):
            return (pid, new)
        return (pid, None)

    t0 = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for i, res in enumerate(ex.map(work, need), 1):
            results.append(res)
            if i % 50 == 0:
                print(f"  ..{i}/{len(need)} ({time.time()-t0:.0f}s)", flush=True)
    updates = [(new, pid) for pid, new in results if new]
    con.executemany("UPDATE products SET title_ko=? WHERE id=?", updates)
    con.commit()
    print(f"백필 완료: 채움 {len(updates)} / 실패 {len(need)-len(updates)} / {time.time()-t0:.0f}s", flush=True)
    con.close()
    return len(updates)


def stats():
    con = _con()
    todo = _promotion_queue_pids()
    n = have = garb = noen = 0
    for pid in todo[:5000]:
        r = con.execute("SELECT title_ko, title_en, sp_title_en FROM products WHERE id=?", (pid,)).fetchone()
        if not r:
            continue
        n += 1
        if not (r["title_en"] or r["sp_title_en"]):
            noen += 1
        elif is_title_garbage(r["title_ko"]):
            garb += 1
        else:
            have += 1
    print(f"승급큐 표본 {n}: 유효 title_ko {have} / 무·오염 {garb} / 영문원천없음 {noen}")
    con.close()


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(f"{BASE}/.env")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "titles":
        backfill_titles(int(sys.argv[2]) if len(sys.argv) > 2 else 500)
    else:
        stats()
