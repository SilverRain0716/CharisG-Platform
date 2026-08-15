#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""구계정 listed → 신계정 이관 (per-product 단일경로). 이미지다운로드+가격백필+list_product.
   대상: 구계정 listed product 중 신계정 미등록. /tmp/old_migrate_pids.json 캐시.
   usage: python migrate_old.py <N>"""
import os, sys, json, sqlite3, asyncio, time
os.environ["PA_SKIP_GEMINI"] = "1"
sys.path.insert(0, "/home/ubuntu/CharisG-Platform/charisg-platform")
import logging; logging.disable(logging.WARNING)
from dotenv import load_dotenv; load_dotenv("/home/ubuntu/CharisG-Platform/charisg-platform/.env")
from backend.purchase.services.image_downloader import download_product_images
from backend.purchase.services.coupang_lister import list_product, _get_product_images
from backend.purchase.services.coupang_service import coupang_account
from backend.purchase.services.pricing_service_pa import calculate_sale_krw
from collections import Counter

class _ClassifyDefer(Exception):
    """이미지 분류 실패 — 이번 회차 등록 보류(다음 재시도)."""
    pass
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state"); os.makedirs(STATE, exist_ok=True)

DB = "/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase.db"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 50
PIDS = os.path.join(STATE, "old_migrate_pids.json")
RES = os.path.join(STATE, "old_migrate_result.json")

# 대상 product_id 빌드 (1회 캐시): 구계정 listed product 중 신계정 미등록
if not os.path.exists(PIDS):
    con = sqlite3.connect(DB)
    old = set(r[0] for r in con.execute(
        "SELECT DISTINCT product_id FROM listings_pa WHERE channel='coupang' AND coupang_account='old' AND status='listed'").fetchall())
    new = set(r[0] for r in con.execute(
        "SELECT DISTINCT product_id FROM listings_pa WHERE channel='coupang' AND coupang_account='new'").fetchall())
    # 신계정 미등록 + B0 상품만(도서 제외)
    cand = [pid for pid in (old - new)]
    asins = {r[0]: r[1] for r in con.execute(f"SELECT id, asin FROM products WHERE id IN ({','.join('?'*len(cand))})", cand).fetchall()} if cand else {}
    pids = sorted([pid for pid in cand if (asins.get(pid) or '').startswith('B0')])
    json.dump(pids, open(PIDS, "w"))
    print(f"대상 product 빌드: {len(pids)} (구listed-신등록, B0)")
pids = json.load(open(PIDS))

# ★액자/그림/가구 카테고리 제외 (2026-07-05, 사장님 지시 — 마이그레이션 안 함). 아마존 카테고리 정확일치(펫/캠핑 오탐 0).
_EXCLUDE_CATS = ("Posters & Prints","Wall & Tabletop Frames","Poster Frames","Picture Frames","Paintings","Painting","Photo Albums, Frames & Accessories","Furniture","Living Room Furniture","Bedroom Furniture","Home Office Furniture","Kitchen & Dining Room Furniture","Mattresses","Wall-Mounted Mirrors","Makeup Mirrors","Floor & Full Length Mirrors","Mirrors","Wall-Mounted Vanity Mirrors","Mirror Sets","Wall-Mounted","Wall Décor","Shower & Wall Mounts","Wall-Mounted Wine Racks")
try:
    _exc = sqlite3.connect(DB); _ph = ",".join("?"*len(_EXCLUDE_CATS))
    _excl_ids = set(r[0] for r in _exc.execute(
        "SELECT DISTINCT p.id FROM products p, json_each(p.amazon_category_json) j "
        "WHERE p.amazon_category_json IS NOT NULL AND (j.value ->> 'name') IN (%s)" % _ph, _EXCLUDE_CATS))
    _exc.close()
    _b = len(pids); pids = [p for p in pids if p not in _excl_ids]
    print(f"액자/그림/가구 카테고리 제외: {_b - len(pids)}건 스킵 (대상 {len(pids)})", flush=True)
except Exception as _e:
    print(f"카테고리 제외 필터 오류(계속): {_e}", flush=True)

results = []; done = set()
if os.path.exists(RES):
    try:
        results = json.load(open(RES)); done = {r["pid"] for r in results}
    except Exception: pass
todo = [p for p in pids if p not in done][:N]
# ★그룹 parent 재등록 방지(2026-07-01): 같은 parent의 자식 pid마다 _prep_grp가 다시 호출돼
#   이중 임시저장(중복 등록)이 발생했음. 기존 결과에서 이미 그룹등록된 parent를 수집해 재등록 스킵.
import re as _re_p
seen_group_parents = set()
for _r in results:
    if _r.get("outcome") in ("group_listed", "group_fallback", "group_dup_skip"):
        _p = _r.get("parent")
        if not _p:
            _m = _re_p.search(r"parent=(\S+)", _r.get("detail") or "")
            _p = _m.group(1) if _m else None
        if _p:
            seen_group_parents.add(_p)
print(f"대상 {len(pids)} · 완료 {len(done)} · 이번 {len(todo)} · 기등록그룹 {len(seen_group_parents)}", flush=True)
con = sqlite3.connect(DB, timeout=180); con.execute("PRAGMA busy_timeout=180000")

def save(): json.dump(results, open(RES, "w"), ensure_ascii=False, default=str)

def prep(pid):
    row = con.execute("SELECT images_json, sale_price_krw, cost_usd FROM products WHERE id=?", (pid,)).fetchone()
    if not row: return
    ij, sale, cost = row
    cnt = con.execute("SELECT COUNT(*) FROM image_cache WHERE product_id=?", (pid,)).fetchone()[0]
    if cnt == 0 and ij and ij not in ("[]", ""):
        try: asyncio.run(download_product_images(pid, ij))
        except Exception: pass
    # ★ 강제 재분류 + 신뢰성 검증 — 분류 실패(레이트리밋 등)면 all-photo 폴백으로
    #   마케팅이 새어드는 것을 막기 위해 이번 회차 등록 보류(다음 재시도).
    from backend.purchase.services.image_classifier import classify_reliable
    _cls, _ok = classify_reliable(pid, force=False)
    if not _ok:
        raise _ClassifyDefer(pid)
    if (not sale or sale < 1000) and cost and cost > 0:
        try:
            r = calculate_sale_krw(cost_usd=float(cost), channel="coupang"); sk = (r.get("sale_krw") if isinstance(r, dict) else 0) or 0
            if sk >= 1000: con.execute("UPDATE products SET sale_price_krw=? WHERE id=?", (int(sk), pid)); con.commit()
        except Exception: pass
    # ★ 하이브리드: 깨끗한 제품사진 0장(마케팅전용)이면 B-design 생성(에디토리얼+design_cut).
    #   생성하면 select_representative_image=design_cut, build_detail_contents=ed_manifest 자동사용.
    #   ~0% 발동(구상품 대부분 실사진 보유) → 비용 거의 없음.
    try:
        # ★전 상품 에디토리얼 전환 (2026-07-05): 디자인=HTML/CSS(모델무관), 비전=flash-lite.
        #   design_cut(AI 이미지생성, Nano Banana)은 비용상 0-clean 에만 유지.
        import sys as _s
        if HERE not in _s.path: _s.path.insert(0, HERE)
        from backend.purchase.services.design_cut import gen_design_cut, is_zero_clean
        if is_zero_clean(pid):
            # ★AI design_cut(깨끗/완전)만 사용. Gemini 소진 시 rembg 미사용(저작권 위험) → list_product가 홀드 (2026-07-05)
            gen_design_cut(pid, force=False)
        from detail_b import generate_shared_editorial
        generate_shared_editorial(pid, force=True)
    except Exception: pass
    # ★구성품 가공컷 — 모든 단품에 적용: SP원본 확보(캐시우선=build_payload get_strict_facts와 동일호출 → 실질 SP-API 추가비용 0) 후 가공.
    #   진짜 2종+ 구성품일 때만 가공컷+블록 생성(비세트는 즉시 None 반환), Nano Banana도 세트에만 발동 → 비용 안전. (세트제목 게이트 제거: 무세트제목 세트도 커버)
    try:
        _r = con.execute("SELECT asin, sp_raw_json FROM products WHERE id=?", (pid,)).fetchone()
        _has_raw = bool(_r and _r[1])
        if _r and _r[0]:
            if not _has_raw:
                from backend.purchase.services.sp_api_facts import fetch_full_catalog_facts
                fetch_full_catalog_facts(_r[0])     # 캐시우선(7일 TTL) — 세트제목 아니어도 원본 확보
            from backend.purchase.services.components_image import ensure_components_cut
            ensure_components_cut(pid)               # 진짜 2종+ 일 때만 가공컷+블록(아니면 None)
    except Exception: pass

cnt = Counter(); times = []
for i, pid in enumerate(todo):
    t0 = time.time(); rec = {"pid": pid}
    try:
        # ★그룹/단품 라우터(앞단 판별기): 변형>=2면 그룹(멀티옵션) 인라인 등록, 아니면 단품 인라인.
        import sys as _s2
        if HERE not in _s2.path: _s2.path.insert(0, HERE)
        from migrate_group import classify as _classify, prepare_and_register_group as _prep_grp
        _verdict, _parent, _nv = _classify(pid)
        if _verdict == "group":
            rec["parent"] = _parent
            # ★중복 방지(2026-07-01): 이 parent가 이미 그룹등록됐으면 재등록 없이 자식 pid만 done 처리.
            if _parent in seen_group_parents:
                rec["outcome"] = "group_dup_skip"; rec["detail"] = f"parent={_parent} dup-skip"
                _dt = time.time() - t0; times.append(_dt)
                cnt[rec["outcome"]] += 1; results.append(rec); save()
                print(f"[{i+1}/{len(todo)}] pid{pid}: group_dup_skip (parent={_parent}) ({_dt:.0f}s)", flush=True)
                continue
            # children 보강+이미지(타임아웃45s)+분류+번역+멀티옵션 등록(requested=False)을 즉시 인라인 처리.
            # prepare_and_register_group 이 전역 PA_SKIP_GEMINI=1 을 세팅하므로 뒤따르는 단품에 새지 않게 복원.
            _prev_skip = os.environ.get("PA_SKIP_GEMINI")
            try:
                _g = _prep_grp(_parent) or {}
            finally:
                if _prev_skip is None: os.environ.pop("PA_SKIP_GEMINI", None)
                else: os.environ["PA_SKIP_GEMINI"] = _prev_skip
            if _g.get("registered"):
                rec["outcome"] = "group_listed"; rec["spid"] = _g.get("spid"); rec["detail"] = f"parent={_parent} \uc635\uc158{_g.get('n_options')}"
            elif _g.get("status") == "single_fallback":
                rec["outcome"] = "group_fallback"; rec["spid"] = _g.get("spid"); rec["detail"] = f"parent={_parent} \ub2e8\uc77c\ud3f4\ubc31"
            else:
                rec["outcome"] = "group_fail"; rec["detail"] = f"parent={_parent} {_g.get('status')}"
            if rec["outcome"] in ("group_listed", "group_fallback"):
                seen_group_parents.add(_parent)
            _dt = time.time() - t0; times.append(_dt)
            cnt[rec["outcome"]] += 1; results.append(rec); save()
            print(f"[{i+1}/{len(todo)}] pid{pid}: {rec['outcome']} {rec.get('spid') or ''} (parent={_parent}, \ubcc0\ud615{_nv}) ({_dt:.0f}s)", flush=True)
            continue
        prep(pid)
        imgs = _get_product_images(pid)
        with coupang_account("new"):
            out = list_product(pid, image_urls=imgs, requested=False)
        if out.get("ok"):
            rec["outcome"] = "listed"; rec["spid"] = (out.get("result") or {}).get("data")
        else:
            rec["outcome"] = "skip"; rec["detail"] = (out.get("error") or "")[:90]
    except _ClassifyDefer:
        print(f"[{i+1}/{len(todo)}] pid{pid}: defer (분류 실패 — 다음 회차 재시도)", flush=True)
        continue
    except Exception as e:
        rec["outcome"] = "exc"; rec["detail"] = str(e)[:90]
    dt = time.time() - t0; times.append(dt)
    cnt[rec["outcome"]] += 1
    results.append(rec); save()
    print(f"[{i+1}/{len(todo)}] pid{pid}: {rec['outcome']} {rec.get('spid') or rec.get('detail','')} ({dt:.0f}s)", flush=True)

avg = sum(times) / max(len(times), 1)
print(f"\n분포: {dict(cnt)} | 평균 {avg:.0f}초/건 → 단일워커 ~{int(86400/max(avg,1))}/일")
print("MIGRATE_DONE", flush=True)
