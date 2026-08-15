#!/bin/bash
cd /home/ubuntu/CharisG-Platform/charisg-platform
source .venv/bin/activate
# 32 fail spid 모두 재시도 — get_seller_product 가 일시 오류였던 26 처리됨
for spid in $(cat /tmp/g3_fail_spids.txt); do
  python3 g3_relabel.py --apply --spid "$spid" --sleep 2 2>&1 | grep -E "^\[총|PUT OK|PUT FAIL|SKIP|SAME|FAIL"
  sleep 2
done
