#!/bin/bash
cd ~/CharisG-Platform/charisg-platform
source .venv/bin/activate

echo "$(date) === 가격 교정 완료 — 후처리 시작 ==="

# 1. $0 상품: cost_usd 원복 + 별도 리포트 생성 (제거 검토용)
echo "$(date) $0 가격 상품 처리"
grep "API=\$0.00" /tmp/audit_prices_v2.log | \
  sed -n "s/.*#\([0-9]*\).*DB=\$\([0-9.]*\).*/UPDATE products SET cost_usd=\2, amazon_price_usd=NULL, updated_at=datetime(\"now\") WHERE id=\1;/p" \
  > /tmp/fix_zero_prices.sql
ZERO_COUNT=$(wc -l < /tmp/fix_zero_prices.sql)
echo "$(date) $0 가격 건수: $ZERO_COUNT — cost_usd 원복 + 검토 리포트 생성"
sqlite3 -cmd ".timeout 30000" backend/purchase/purchase.db < /tmp/fix_zero_prices.sql

# $0 상품 검토 리포트 (제거 판단용)
sqlite3 -cmd ".timeout 30000" -header -csv backend/purchase/purchase.db \
  "SELECT id, asin, brand, title_en, cost_usd, status,
          (SELECT GROUP_CONCAT(channel||:||status) FROM listings_pa WHERE product_id=products.id) as listings
   FROM products
   WHERE amazon_price_usd IS NULL AND id IN (
     $(grep API=$0.00 /tmp/audit_prices_v2.log | sed -n s/.*#([0-9]*).*/1/p | paste -sd,)
   )
   ORDER BY id" > /tmp/zero_price_review.csv
echo "$(date) 검토 리포트: /tmp/zero_price_review.csv"

# 2. 스마트스토어 가격 동기화 ($0 상품 제외)
echo "$(date) 스마트스토어 가격 동기화 시작"
python3 scripts/sync_prices_smartstore.py >> /tmp/sync_smartstore.log 2>&1
echo "$(date) 스마트스토어 동기화 완료"
