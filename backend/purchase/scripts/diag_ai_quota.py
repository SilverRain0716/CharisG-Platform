"""Gemini 일간한도(02:12 UTC) 시점 AI 처리 품질 진단.
- created>=05-25 의 시간대별 total/ai_done 분포 (11번가 vs sid20 구분)
- ai_processed_at >= 02:09 (한도 실패창)에서 마킹된 건 중 title_ko/seo 비어있는 '쓰레기' 수
- 쓰레기면 ai_processed_at NULL 로 리셋해야 재처리 가능."""
import os, sqlite3
DB = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform") + "/backend/purchase/purchase.db"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
P = "business_model='purchase' AND created_at>='2026-05-25'"

print("=== 시간대별 분포 (created>=05-25, purchase) ===")
for r in c.execute(f"SELECT substr(created_at,1,13) hr, COUNT(*) n, "
                   f"SUM(CASE WHEN ai_processed_at IS NOT NULL THEN 1 ELSE 0 END) ai FROM products "
                   f"WHERE {P} GROUP BY hr ORDER BY hr"):
    print(f"  {r['hr']}  total={r['n']:5d}  ai_done={r['ai']:5d}")

WIN = "ai_processed_at>='2026-05-26T02:09'"
print(f"\n=== ai_processed 마킹 건의 title_ko/seo 품질 (sid20: created>=2026-05-25T23) ===")
for label, cond in [("정상창 ai<02:09", "ai_processed_at<'2026-05-26T02:09'"),
                    ("한도창 ai>=02:09", WIN)]:
    r = c.execute(f"SELECT COUNT(*) tot, "
                  f"SUM(CASE WHEN title_ko IS NULL OR title_ko='' THEN 1 ELSE 0 END) noko, "
                  f"SUM(CASE WHEN seo_title IS NULL OR seo_title='' THEN 1 ELSE 0 END) noseo "
                  f"FROM products WHERE business_model='purchase' AND created_at>='2026-05-25T23' "
                  f"AND ai_processed_at IS NOT NULL AND {cond}").fetchone()
    print(f"  [{label}] ai_done={r['tot']}  title_ko_빔={r['noko']}  seo_title_빔={r['noseo']}")

print("\n=== 한도창 마킹 샘플 5건 ===")
for r in c.execute(f"SELECT id, substr(ai_processed_at,1,19) ai, substr(title_en,1,28) en, "
                   f"COALESCE(substr(title_ko,1,28),'<NULL>') ko, COALESCE(substr(seo_title,1,20),'<NULL>') st "
                   f"FROM products WHERE business_model='purchase' AND created_at>='2026-05-25T23' AND {WIN} LIMIT 5"):
    print(f"  id={r['id']} ai={r['ai']} en={r['en']!r} ko={r['ko']!r} seo={r['st']!r}")

n_reset = c.execute(f"SELECT COUNT(*) FROM products WHERE business_model='purchase' AND created_at>='2026-05-25T23' "
                    f"AND {WIN} AND (title_ko IS NULL OR title_ko='' OR seo_title IS NULL OR seo_title='')").fetchone()[0]
print(f"\n=== 리셋 대상(한도창 + title_ko/seo 빔): {n_reset}건 ===")
c.close()
