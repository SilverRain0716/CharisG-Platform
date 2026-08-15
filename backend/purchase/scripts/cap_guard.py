#!/usr/bin/env python3
"""쿠팡 일일 한도(A00353099) 소진 가드.
- 최근 6분 A00353099 폭증 감지 → group-worker(w1,w2) 정지 (spinning/DB경합/대시보드 사고 방지).
- 한도 리셋(00:00 KST=15:00 UTC) 넘어가면 → w1+w2 자동 재개.
- 5분마다 systemd timer로 실행. root로 동작(systemctl 직접 호출).
2026-06-08 신설 (사용자 지시: 한도 소진시 워커 자동 정지).
2026-06-09 보강: 정지 유지 중 Restart=always/timer 로 되살아난 워커 재정지(스핀 사각지대 봉합).
2026-06-10 보강: 재개 시 w1 뿐 아니라 w2 도 함께 재개(멀티워커 부활 — w2가 06-07 stop 후 영영 안돌던 갭 봉합).
"""
import subprocess, os
from datetime import datetime, timezone, timedelta

MARKER = "/home/ubuntu/.cap_exhausted_marker"
W1 = "charisg-pa-group-worker.service"
W2 = "charisg-pa-group-worker2.service"
THRESH = 15  # 6분내 A00353099 이 이상이면 소진으로 판정

def cap_day(dt):
    # 쿠팡 한도 리셋 15:00 UTC(=00:00 KST). cap-day = 윈도우 시작 날짜.
    return (dt - timedelta(hours=15)).date()

def cap_err_count():
    o = subprocess.run(["journalctl", "--since", "6 min ago", "--no-pager"],
                       capture_output=True, text=True).stdout
    return o.count("A00353099")

def is_active(svc):
    return subprocess.run(["systemctl", "is-active", "--quiet", svc]).returncode == 0

def log(msg):
    print(f"{datetime.now(timezone.utc).isoformat()} cap_guard: {msg}", flush=True)

def main():
    now = datetime.now(timezone.utc)
    if os.path.exists(MARKER):
        # 정지 상태 — 한도 리셋(새 cap-day) 넘어가면 w1+w2 재개
        marker_day = open(MARKER).read().strip()
        if str(cap_day(now)) != marker_day:
            for w in (W1, W2):
                subprocess.run(["systemctl", "reset-failed", w])
                subprocess.run(["systemctl", "start", w])
            os.remove(MARKER)
            log(f"한도 리셋 (marker={marker_day} → {cap_day(now)}) — w1+w2 재개")
        else:
            # 정지 유지 — Restart=always/timer 로 되살아난 워커가 있으면 재정지(스핀 차단)
            revived = [w for w in (W1, W2) if is_active(w)]
            if revived:
                for w in revived:
                    subprocess.run(["systemctl", "stop", w])
                log(f"한도소진 정지 유지 — 되살아난 워커 재정지: {revived} (marker={marker_day})")
            else:
                log(f"한도소진 정지 유지 (marker={marker_day})")
    else:
        errs = cap_err_count()
        if errs >= THRESH:
            stopped = []
            for w in (W1, W2):
                if is_active(w):
                    subprocess.run(["systemctl", "stop", w])
                    stopped.append(w)
            open(MARKER, "w").write(str(cap_day(now)))
            log(f"★한도 소진 감지 (A00353099={errs}/6분) — 정지: {stopped} marker={cap_day(now)}")
        else:
            log(f"정상 (A00353099={errs}/6분)")

if __name__ == "__main__":
    main()
