# -*- coding: utf-8 -*-
"""ai_ledger.py — AI 호출을 전부 가로채 토큰·비용을 원장에 남긴다.

목적: 상품 1건을 임포트~등록까지 돌릴 때 AI 비용이 정확히 얼마인지 재기 위함.
      Google 청구 콘솔은 수 시간 지연 반영이라 단건 측정에 못 쓴다.

방법: requests.post 를 몽키패치해서 generativelanguage / openai / anthropic 호출을
      가로채 usageMetadata(또는 usage)를 뽑아 JSONL 로 적는다.
      ★thoughtsTokenCount 를 반드시 포함한다 — 2.5 계열은 사고토큰이 출력으로 과금되는데
        candidatesTokenCount 에 잡히지 않아, 이걸 빼면 실제의 1/3로 과소집계된다(실측).

사용: 파이프라인 스크립트 맨 앞에서 import 후 install() 호출.
      LEDGER=/tmp/ai_ledger.jsonl 환경변수로 경로 지정 가능.
"""
import json
import os
import time
import traceback
from pathlib import Path

LEDGER = Path(os.environ.get("AI_LEDGER", "/tmp/ai_ledger.jsonl"))

# 1M 토큰당 USD (입력, 출력)
PRICES = {
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-embedding": (0.15, 0.0),
    "text-embedding": (0.15, 0.0),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
}
DEFAULT_PRICE = (0.30, 2.50)
USD_KRW = 1380.0


def _price(model: str):
    for k, v in PRICES.items():
        if k in model:
            return v
    return DEFAULT_PRICE


def _caller():
    """호출 스택에서 우리 코드 프레임을 찾아 '어느 단계'인지 남긴다."""
    for fr in reversed(traceback.extract_stack()[:-3]):
        f = fr.filename
        if "/backend/purchase/" in f or "/backend_shared/" in f or "/home/ubuntu/" in f:
            if "ai_ledger" in f:
                continue
            return f"{Path(f).stem}.{fr.name}"
    return "?"


def _record(provider, model, pin, pout, ok=True, note=""):
    ip, op = _price(model)
    usd = pin * ip / 1e6 + pout * op / 1e6
    rec = {"ts": round(time.time(), 3), "provider": provider, "model": model,
           "caller": _caller(), "in": pin, "out": pout,
           "usd": round(usd, 8), "krw": round(usd * USD_KRW, 4), "ok": ok, "note": note}
    try:
        with LEDGER.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return rec


_installed = False


def install():
    global _installed
    if _installed:
        return
    import requests
    orig_post = requests.post
    orig_sess_post = requests.Session.post

    def _extract(url, resp):
        try:
            u = str(url)
            if "generativelanguage" in u:
                model = u.split("/models/")[1].split(":")[0] if "/models/" in u else "gemini?"
                if resp.status_code != 200:
                    return _record("gemini", model, 0, 0, ok=False, note=f"HTTP {resp.status_code}")
                j = resp.json()
                if "embedContent" in u or "embedding" in model.lower():
                    return _record("gemini", "gemini-embedding",
                                   j.get("usageMetadata", {}).get("promptTokenCount", 0), 0)
                um = j.get("usageMetadata", {}) or {}
                pin = um.get("promptTokenCount", 0)
                # ★사고 토큰은 출력 과금 — candidatesTokenCount 에 없다
                pout = um.get("candidatesTokenCount", 0) + um.get("thoughtsTokenCount", 0)
                return _record("gemini", model, pin, pout)
            if "openai.com" in u:
                if resp.status_code != 200:
                    return _record("openai", "gpt?", 0, 0, ok=False, note=f"HTTP {resp.status_code}")
                us = (resp.json().get("usage") or {})
                return _record("openai", resp.json().get("model", "gpt?"),
                               us.get("prompt_tokens", 0), us.get("completion_tokens", 0))
            if "anthropic.com" in u:
                if resp.status_code != 200:
                    return _record("claude", "claude?", 0, 0, ok=False, note=f"HTTP {resp.status_code}")
                us = (resp.json().get("usage") or {})
                return _record("claude", resp.json().get("model", "claude?"),
                               us.get("input_tokens", 0), us.get("output_tokens", 0))
        except Exception:
            pass
        return None

    def post(url, *a, **k):
        r = orig_post(url, *a, **k)
        _extract(url, r)
        return r

    def sess_post(self, url, *a, **k):
        r = orig_sess_post(self, url, *a, **k)
        _extract(url, r)
        return r

    requests.post = post
    requests.Session.post = sess_post
    _installed = True
    print(f"[ai_ledger] 설치됨 → {LEDGER}", flush=True)


def report(path=None):
    p = Path(path or LEDGER)
    if not p.exists():
        print("원장 없음"); return
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    ok = [r for r in rows if r["ok"]]
    fail = [r for r in rows if not r["ok"]]
    print(f"\n{'='*78}")
    print(f"AI 호출 원장 — 총 {len(rows)}콜 (성공 {len(ok)} / 실패 {len(fail)})")
    print(f"{'='*78}")
    print(f"{'단계(caller)':<42}{'콜':>4}{'입력':>9}{'출력':>9}{'원':>9}")
    print("-" * 78)
    agg = {}
    for r in ok:
        k = r["caller"]
        a = agg.setdefault(k, {"n": 0, "i": 0, "o": 0, "krw": 0.0, "m": set()})
        a["n"] += 1; a["i"] += r["in"]; a["o"] += r["out"]; a["krw"] += r["krw"]; a["m"].add(r["model"])
    for k, a in sorted(agg.items(), key=lambda x: -x[1]["krw"]):
        print(f"{k:<42}{a['n']:>4}{a['i']:>9,}{a['o']:>9,}{a['krw']:>9.2f}")
    tot = sum(a["krw"] for a in agg.values())
    ti = sum(a["i"] for a in agg.values()); to = sum(a["o"] for a in agg.values())
    print("-" * 78)
    print(f"{'합계':<42}{len(ok):>4}{ti:>9,}{to:>9,}{tot:>9.2f}")
    if fail:
        from collections import Counter
        print(f"\n실패 {len(fail)}콜: {dict(Counter(r['note'] for r in fail))}")
    print(f"\n모델별:")
    mm = {}
    for r in ok:
        m = mm.setdefault(r["model"], {"n": 0, "krw": 0.0})
        m["n"] += 1; m["krw"] += r["krw"]
    for m, v in sorted(mm.items(), key=lambda x: -x[1]["krw"]):
        print(f"  {m:<28}{v['n']:>4}콜  {v['krw']:>8.2f}원")
    print(f"\n★ 상품 1건 총 AI 비용: {tot:.2f}원")


if __name__ == "__main__":
    import sys
    report(sys.argv[1] if len(sys.argv) > 1 else None)
