"""잡 간 충돌 방지 turnstile 락 (2단계 read-write). 2026-06-03.

설계 (writer-priority read-write lock, turnstile gate 패턴):
- 두 파일: LOCK(본 락) + GATE(턴스타일 mutex).
- 파이프라인 reader(드레인+시트워커): [GATE 잠금 → LOCK SHARED 획득 → GATE 해제] → 작업 → LOCK 해제.
  GATE 는 순간만 점유(통과용). SHARED 끼리는 동시 보유 가능(producer-consumer 동시 실행).
- 배치잡 writer(systemd oneshot 14개): GATE EXCLUSIVE + LOCK EXCLUSIVE 를 작업 내내 보유.
  GATE 를 잡으면 신규 reader 가 턴스타일에서 막힘 → 진행중 reader 만 빠지면 LOCK EXCLUSIVE 획득 →
  배치잡 단독 실행. ★reader 가 계속 돌아도 writer 가 굶지 않음.
  ExecStart 래핑: `/usr/bin/flock -x -w <W> <GATE> /usr/bin/flock -x <LOCK> <원래명령>`

비활성: env JOB_LOCK_ENABLED=0. 락 파일: JOB_LOCK_PATH(기본 /home/ubuntu/.charisg-job.lock) + .gate.
획득 실패 시 락 없이 진행(graceful) — 락 때문에 파이프라인이 멈추지 않음.
"""
import fcntl
import os
import time
import asyncio
import logging

logger = logging.getLogger(__name__)

LOCK_PATH = os.environ.get("JOB_LOCK_PATH", "/home/ubuntu/.charisg-job.lock")
GATE_PATH = LOCK_PATH + ".gate"
_ENABLED = os.environ.get("JOB_LOCK_ENABLED", "1").strip().lower() in ("1", "true", "yes")
_YIELD_SECS = float(os.environ.get("JOB_LOCK_YIELD_SECS", "0.5"))


def _acquire_shared(label: str = ""):
    """reader SHARED 획득 (turnstile gate 경유, 블로킹). 실패 시 None (락 없이 진행).

    [GATE EXCLUSIVE 잠금 → LOCK SHARED 획득 → GATE 해제] — GATE 는 통과용 순간 점유.
    writer 가 GATE 보유 중이면 여기서 막혀 배치잡 우선권 보장.
    """
    if not _ENABLED:
        return None
    gate = None
    try:
        gate = open(GATE_PATH, "a+")
        fcntl.flock(gate.fileno(), fcntl.LOCK_EX)      # 턴스타일 통과 (writer 있으면 블록)
        lock = open(LOCK_PATH, "a+")
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH)      # 읽기-공유 획득
        fcntl.flock(gate.fileno(), fcntl.LOCK_UN)      # 다른 reader 위해 턴스타일 개방
        gate.close()
        return lock
    except Exception as e:
        logger.warning(f"[job-lock] SHARED 획득 실패({label}) — 락 없이 진행: {e}")
        if gate is not None:
            try:
                fcntl.flock(gate.fileno(), fcntl.LOCK_UN)
                gate.close()
            except Exception:
                pass
        return None


def _release(f):
    if f is None:
        return
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()
    except Exception:
        pass


def _writer_waiting() -> bool:
    """non-blocking: GATE 가 writer(배치잡)에게 잡혀 EXCLUSIVE 를 대기중인가? 양보 판단용.

    writer 는 [GATE EXCLUSIVE 보유 → LOCK EXCLUSIVE 대기] 패턴이라, GATE 를 non-blocking
    으로 못 잡으면 writer 가 대기중인 것. 비었으면 대기 writer 없음.
    """
    if not _ENABLED:
        return False
    g = None
    try:
        g = open(GATE_PATH, "a+")
        try:
            fcntl.flock(g.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(g.fileno(), fcntl.LOCK_UN)
            return False  # GATE 비어있음 → 대기 writer 없음
        except OSError:
            return True   # GATE 잡힘 → writer 가 EXCLUSIVE 기다리는 중
    except Exception:
        return False
    finally:
        if g is not None:
            try:
                g.close()
            except Exception:
                pass


class shared_unit:
    """동기 컨텍스트매니저 — 작업단위 1개 동안 SHARED 보유, 종료 시 release + yield.

        with shared_unit("group-worker"):
            process_one()
    """
    def __init__(self, label: str = ""):
        self.label = label
        self._f = None

    def __enter__(self):
        self._f = _acquire_shared(self.label)
        return self

    def __exit__(self, *exc):
        _release(self._f)
        self._f = None
        if _ENABLED and _YIELD_SECS > 0:
            time.sleep(_YIELD_SECS)
        return False


class async_shared_unit:
    """비동기 컨텍스트매니저 — pa-api(이벤트루프) 안전판. 획득/해제를 스레드로 오프로드해
    루프 블로킹 방지. 락은 프로세스 fd 가 보유하므로 await 구간 내내 유지됨.

        async with async_shared_unit("sheet-stage:promote"):
            await heavy_stage()
    """
    def __init__(self, label: str = ""):
        self.label = label
        self._f = None

    async def __aenter__(self):
        self._f = await asyncio.to_thread(_acquire_shared, self.label)
        return self

    async def __aexit__(self, *exc):
        await asyncio.to_thread(_release, self._f)
        self._f = None
        if _ENABLED and _YIELD_SECS > 0:
            await asyncio.sleep(_YIELD_SECS)
        return False

    async def checkpoint(self):
        """긴 단계 루프 중간에 호출 — writer(배치잡)가 대기중이면 SHARED 를 잠깐 놓고
        양보 후 재획득. 대기 없으면 거의 무비용(non-blocking GATE 확인 1회).

        재획득 `_acquire_shared` 는 GATE 경유라 writer 가 EXCLUSIVE 끝내고 GATE 를 놓을
        때까지 블록 → writer 가 단독 실행할 틈을 보장. 재획득 실패 시 락 없이 진행(graceful).
        """
        if not _ENABLED or self._f is None:
            return
        if not await asyncio.to_thread(_writer_waiting):
            return
        await asyncio.to_thread(_release, self._f)
        self._f = None
        await asyncio.sleep(_YIELD_SECS)
        self._f = await asyncio.to_thread(_acquire_shared, self.label)
