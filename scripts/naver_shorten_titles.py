"""네이버 스마트스토어 상품명 일괄 축약 (50자 이내).

3단계 처리:
  1단계: DB의 title_ko를 Gemini로 45~50자로 축약 → DB 업데이트
  2단계: 네이버에 등록된 상품(listed)의 상품명을 네이버 Commerce API로 수정
  3단계: 결과 리포트

사용법 (EC2에서):
  cd ~/CharisG-Platform/charisg-platform
  .venv/bin/python -m scripts.naver_shorten_titles              # 기본: 50자 초과만
  .venv/bin/python -m scripts.naver_shorten_titles --dry-run    # DB/네이버 수정 없이 미리보기
  .venv/bin/python -m scripts.naver_shorten_titles --db-only    # DB만 업데이트 (네이버 수정 안 함)
  .venv/bin/python -m scripts.naver_shorten_titles --naver-only # DB 이미 수정됨, 네이버만 반영
"""
import json
import logging
import os
import re
import sqlite3
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# .env 로드
from pathlib import Path
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                v = v.strip()
                # 따옴표 제거 ('...' 또는 "...")
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1]
                os.environ.setdefault(k.strip(), v)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "backend-shared"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.purchase.database import DB_PATH
from backend_shared.ai.service import _call_gemini, gemini_limiter

# ── 특수문자 치환 (네이버 금지: \ * ? " < >)
_SPECIAL_CHAR_MAP = {
    '"': '인치', '\u201c': '인치', '\u201d': '인치',
    '*': 'x', '\\': ' ', '?': ' ', '<': '(', '>': ')',
}
_SPECIAL_RE = re.compile('[' + re.escape(''.join(_SPECIAL_CHAR_MAP.keys())) + ']')

MAX_TITLE_LEN = 50
TARGET_LEN = "45~50"


def clean_special_chars(name: str) -> str:
    def _replace(m):
        return _SPECIAL_CHAR_MAP.get(m.group(0), ' ')
    cleaned = _SPECIAL_RE.sub(_replace, name)
    return re.sub(r'\s+', ' ', cleaned).strip()


def shorten_title_ai(title_ko: str, title_en: str = "") -> str | None:
    """Gemini로 상품명을 45~50자로 축약."""
    prompt = f"""다음 상품명을 네이버 스마트스토어 등록정보 검토 기준에 맞게 {TARGET_LEN}자로 축약해주세요.

현재 상품명: {title_ko}
{f'영문 원본: {title_en}' if title_en else ''}

규칙:
1. 반드시 {TARGET_LEN}자 이내로 작성 (공백 포함)
2. 브랜드명은 영문 그대로 유지
3. 구성: [브랜드] + [핵심 상품명] + [주요 스펙 1~2개]
4. 제거 대상: 모델번호 나열, 호환 차종 나열, 세부 규격, 인증 설명
5. 특수문자(" * ? < > \\) 사용 금지, 괄호() 최소화
6. 불필요한 수식어(프리미엄, 고급, 최고급 등) 제거
7. 핵심 검색 키워드는 반드시 포함

축약된 상품명만 출력하세요 (추가 설명 없이)."""

    result = _call_gemini(prompt, max_tokens=200)
    if not result:
        return None
    # 따옴표 제거
    result = result.strip().strip('"').strip("'").strip()
    # 특수문자 정리
    result = clean_special_chars(result)
    # 길이 보정
    if len(result) > MAX_TITLE_LEN:
        result = result[:MAX_TITLE_LEN].strip()
    return result


def main():
    dry_run = "--dry-run" in sys.argv
    db_only = "--db-only" in sys.argv
    naver_only = "--naver-only" in sys.argv

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # ═══════════════════════════════════════
    # 1단계: DB title_ko 축약
    # ═══════════════════════════════════════
    if not naver_only:
        rows = conn.execute(
            """SELECT id, title_ko, title_en, seo_title
               FROM products
               WHERE title_ko IS NOT NULL AND title_ko != ''
                 AND LENGTH(title_ko) > ?""",
            (MAX_TITLE_LEN,),
        ).fetchall()

        print(f"\n{'='*60}")
        print(f"[1단계] DB 상품명 축약 — 대상: {len(rows)}건 (>{MAX_TITLE_LEN}자)")
        print(f"{'='*60}")

        if not rows:
            print("축약 대상 없음")
        else:
            updated = 0
            failed = 0
            for i, r in enumerate(rows, 1):
                old_title = r["title_ko"]
                title_en = r["title_en"] or ""

                shortened = shorten_title_ai(old_title, title_en)
                if not shortened or len(shortened) < 5:
                    print(f"[{i}/{len(rows)}] FAIL id={r['id']} ({len(old_title)}자) — AI 응답 없음")
                    failed += 1
                    continue

                print(f"[{i}/{len(rows)}] id={r['id']}: {len(old_title)}자→{len(shortened)}자")
                print(f"  전: {old_title[:60]}{'...' if len(old_title)>60 else ''}")
                print(f"  후: {shortened}")

                if not dry_run:
                    conn.execute(
                        """UPDATE products SET title_ko=?, seo_title=?,
                           updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (shortened, shortened, r["id"]),
                    )
                    if i % 50 == 0:
                        conn.commit()
                updated += 1

                # rate limit 보호
                if i < len(rows):
                    time.sleep(0.3)

            if not dry_run:
                conn.commit()

            print(f"\n[1단계 결과] 성공: {updated}, 실패: {failed}, 총: {len(rows)}")
            if dry_run:
                print("  (--dry-run 모드: DB 변경 없음)")

    # ═══════════════════════════════════════
    # 2단계: 네이버 등록 상품 상품명 수정
    # ═══════════════════════════════════════
    if not db_only:
        from backend.purchase.services.naver_commerce_service import update_product

        listed_rows = conn.execute(
            """SELECT p.id, p.title_ko, l.channel_product_id
               FROM products p
               JOIN listings_pa l ON l.product_id = p.id
               WHERE l.channel = 'smartstore'
                 AND l.channel_product_id IS NOT NULL
                 AND l.status IN ('listed', 'active')"""
        ).fetchall()

        print(f"\n{'='*60}")
        print(f"[2단계] 네이버 상품명 수정 — 대상: {len(listed_rows)}건")
        print(f"{'='*60}")

        if not listed_rows:
            print("네이버 수정 대상 없음")
        else:
            naver_ok = 0
            naver_fail = 0
            naver_skip = 0

            for i, r in enumerate(listed_rows, 1):
                product_no = r["channel_product_id"]
                new_name = clean_special_chars(r["title_ko"] or "")

                if not new_name or len(new_name) < 2:
                    print(f"[{i}/{len(listed_rows)}] SKIP id={r['id']} — 상품명 비어있음")
                    naver_skip += 1
                    continue

                if len(new_name) > 100:
                    new_name = new_name[:100]

                print(f"[{i}/{len(listed_rows)}] id={r['id']} naver={product_no}: \"{new_name[:40]}...\" ({len(new_name)}자)")

                if dry_run:
                    naver_ok += 1
                    continue

                result = update_product(product_no, {
                    "originProduct": {
                        "name": new_name,
                    }
                })

                if result:
                    naver_ok += 1
                else:
                    naver_fail += 1
                    print(f"  ⚠️ 네이버 수정 실패: id={r['id']}")

                # 네이버 API rate limit
                time.sleep(0.5)

            print(f"\n[2단계 결과] 성공: {naver_ok}, 실패: {naver_fail}, 스킵: {naver_skip}")
            if dry_run:
                print("  (--dry-run 모드: 네이버 수정 없음)")

    # ═══════════════════════════════════════
    # 리포트
    # ═══════════════════════════════════════
    stats = conn.execute(
        """SELECT
             COUNT(*) as total,
             SUM(CASE WHEN LENGTH(title_ko) <= 50 THEN 1 ELSE 0 END) as ok,
             SUM(CASE WHEN LENGTH(title_ko) > 50 THEN 1 ELSE 0 END) as over,
             ROUND(AVG(LENGTH(title_ko)), 1) as avg_len
           FROM products WHERE title_ko IS NOT NULL AND title_ko != ''"""
    ).fetchone()

    print(f"\n{'='*60}")
    print(f"[최종 현황]")
    print(f"  전체: {stats['total']}건")
    print(f"  50자 이하: {stats['ok']}건")
    print(f"  50자 초과: {stats['over']}건")
    print(f"  평균 길이: {stats['avg_len']}자")
    print(f"{'='*60}")

    conn.close()


if __name__ == "__main__":
    main()
