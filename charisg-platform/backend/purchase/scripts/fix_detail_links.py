"""기존 상세페이지의 개인통관부호/톡톡 링크를 하이퍼링크로 일괄 수정."""
import sqlite3
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from backend.purchase.database import get_db


def fix_links():
    with get_db() as conn:
        rows = conn.execute("SELECT id, html_content FROM detail_pages").fetchall()

    updated = 0
    for r in rows:
        html = r["html_content"]
        new_html = html

        # 1. 개인통관고유부호: div → a 링크
        new_html = new_html.replace(
            '<div style="background:#F0F7FF;border-radius:10px;padding:16px 24px;margin:0 28px 24px;border:1px solid #D0E3F7;text-align:center">',
            '<a href="https://unipass.customs.go.kr/csp/persIndex.do" target="_blank" rel="noopener" style="display:block;background:#F0F7FF;border-radius:10px;padding:16px 24px;margin:0 28px 24px;border:1px solid #D0E3F7;text-align:center;text-decoration:none;cursor:pointer">',
        )
        new_html = new_html.replace(
            "unipass.customs.go.kr/per/persIndex.do</p>\n    </div>",
            "unipass.customs.go.kr</p>\n    </a>",
        )

        # 2. 네이버 톡톡: div → a 링크
        new_html = new_html.replace(
            '<div style="display:block;background:linear-gradient(135deg,#03C75A 0%,#02B550 100%);border-radius:14px;padding:24px 28px;margin-bottom:20px;text-align:center;border:1px solid #02A348">',
            '<a href="https://talk.naver.com/ct/wc4u1w" target="_blank" rel="noopener" style="display:block;background:linear-gradient(135deg,#03C75A 0%,#02B550 100%);border-radius:14px;padding:24px 28px;margin-bottom:20px;text-align:center;border:1px solid #02A348;text-decoration:none;cursor:pointer">',
        )
        new_html = new_html.replace(
            '궁금한 점이 있으시면 네이버 톡톡으로 편하게 문의해주세요!',
            '궁금한 점이 있으시면 톡톡을 눌러 편하게 문의해주세요!',
        )
        new_html = new_html.replace(
            '스토어 채팅에서 "톡톡 문의" 클릭</div>\n  </div>',
            '톡톡 문의하기 →</div>\n  </a>',
        )

        if new_html != html:
            with get_db() as conn:
                conn.execute("UPDATE detail_pages SET html_content=? WHERE id=?", (new_html, r["id"]))
            updated += 1

    print(f"Updated {updated}/{len(rows)} detail pages")


if __name__ == "__main__":
    fix_links()
