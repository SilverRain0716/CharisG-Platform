"""Gemini 실제 구글 쿼터 가용 여부 테스트 (새 프로세스 = limiter 새 9000 budget).
성공 → 구글 쿼터 남음(같은 프로젝트 아님/잔여 있음) → 지금 재개 가능.
None  → 두 키 모두 429(같은 프로젝트 공유쿼터 소진) → 태평양 자정까지 대기."""
import logging, os
from dotenv import load_dotenv
load_dotenv("/home/ubuntu/CharisG-Platform/charisg-platform/.env")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
from backend_shared.ai.service import _call_gemini, gemini_limiter
print(f"RPD cap={gemini_limiter._rpd}  fresh_daily_remaining={gemini_limiter.daily_remaining}")
r = _call_gemini("Reply with exactly one word: OK", max_tokens=10)
print(f"RESULT={r!r}")
print("VERDICT:", "GOOGLE_QUOTA_AVAILABLE — 지금 재개 가능" if r else "EXHAUSTED — 두 키 모두 429, 대기 필요")
