"""P3 — orphan 중복 쿠팡 리스팅 stop_sales (판매중지).

p3_dup_listings.txt (spid<TAB>status<TAB>asin<TAB>keep_cpid) 각 orphan spid 를
stop_sales(onSale=false). keep_cpid(추적 정규본)은 건드리지 않음. 가역(재개 가능).

resume: done 파일에 성공 spid 누적, 재실행 시 skip. 심사중/대기는 stop 불가 → retry 파일.

  python -m backend.purchase.scripts.stop_p3_dups --file /home/ubuntu/logs/p3_dup_listings.txt --apply
"""
import argparse, os, time, collections, socket
socket.setdefaulttimeout(20)
from dotenv import load_dotenv
load_dotenv()
from backend.purchase.services import coupang_service as cs

DONE = "/home/ubuntu/logs/p3_done.txt"
RETRY = "/home/ubuntu/logs/p3_retry.txt"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.8)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    rows = []
    with open(args.file) as f:
        for line in f:
            p = line.strip().split("\t")
            if p and p[0]:
                rows.append(p[0])
    # dedup, resume skip
    done = set()
    if os.path.exists(DONE):
        with open(DONE) as f:
            done = set(x.strip() for x in f if x.strip())
    todo = [s for s in dict.fromkeys(rows) if s not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"p3 총 {len(rows)} / 이미done {len(done)} / 이번 대상 {len(todo)} (apply={args.apply})")
    if not args.apply:
        print("dry — --apply 로 실행"); return

    ok = fail = hold = 0
    df = open(DONE, "a")
    rf = open(RETRY, "a")
    for i, spid in enumerate(todo):
        try:
            success, msg = cs.stop_sales(spid)
        except Exception as e:
            success, msg = False, f"예외:{e}"
        if success:
            ok += 1; df.write(spid + "\n"); df.flush()
        else:
            # 심사중/대기 = vendor item stop 불가 → 보류
            if "item 실패" in msg or "items 비어" in msg:
                hold += 1; rf.write(f"{spid}\t{msg[:60]}\n"); rf.flush()
            else:
                fail += 1; rf.write(f"{spid}\t{msg[:60]}\n"); rf.flush()
        if (i + 1) % 100 == 0:
            print(f"  ...{i+1}/{len(todo)} ok={ok} hold={hold} fail={fail}", flush=True)
        time.sleep(args.sleep)
    df.close(); rf.close()
    print(f"\n=== P3 결과 === stop성공 {ok} / 보류(심사중등) {hold} / 실패 {fail} / 총 {len(todo)}")


if __name__ == "__main__":
    main()
