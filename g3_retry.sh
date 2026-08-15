#!/bin/bash
# G3 PUT FAIL 재시도 — sleep 3초로 rate limit 방지
cd /home/ubuntu/CharisG-Platform/charisg-platform
source .venv/bin/activate
for spid in 16174466978 16175094145 16229970226 16232355888 16232559867 16232568930 16232669585 16232754072 16232755410 16232757318 16232760340 16232808424; do
  echo "=== retry $spid ==="
  python3 g3_relabel.py --apply --spid "$spid" --sleep 3
  sleep 3
done
