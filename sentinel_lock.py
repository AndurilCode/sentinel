"""
GPU coordination lock for Sentinel.

Three priority levels share one flock-based lock:
  P0 (Judge)       — non-blocking try, proceeds regardless
  P1 (Synthesizer) — blocking with short timeout
  P2 (Accumulator) — blocking with long timeout
"""

import fcntl
import os
import time
from enum import IntEnum
from typing import Optional


class LockPriority(IntEnum):
    P0_JUDGE = 0
    P1_SYNTHESIZER = 1
    P2_ACCUMULATOR = 2


_DEFAULT_TIMEOUTS = {
    LockPriority.P0_JUDGE: 0,
    LockPriority.P1_SYNTHESIZER: 5,
    LockPriority.P2_ACCUMULATOR: 30,
}


def acquire_lock(lock_path: str, priority: LockPriority,
                 timeout_s: Optional[float] = None) -> Optional[int]:
    """Acquire the GPU lock file. Returns fd on success, None on failure/skip.

    P0: Non-blocking try. Returns None if locked (caller proceeds without lock).
    P1/P2: Poll with timeout. Returns None on timeout.
    """
    if timeout_s is None:
        timeout_s = _DEFAULT_TIMEOUTS[priority]

    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)

    if priority == LockPriority.P0_JUDGE:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except (BlockingIOError, OSError):
            os.close(fd)
            return None

    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except (BlockingIOError, OSError):
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(1.0)


def release_lock(fd: Optional[int]) -> None:
    """Release and close the lock file descriptor."""
    if fd is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        except OSError:
            pass
