import os
import tempfile
import time
import threading
from sentinel_lock import acquire_lock, release_lock, LockPriority


def test_acquire_and_release():
    """Basic acquire and release cycle."""
    with tempfile.NamedTemporaryFile(delete=False) as f:
        lock_path = f.name
    try:
        fd = acquire_lock(lock_path, LockPriority.P2_ACCUMULATOR)
        assert fd is not None
        release_lock(fd)
    finally:
        os.unlink(lock_path)


def test_p0_never_blocks():
    """P0 (judge) acquires even when lock is held."""
    with tempfile.NamedTemporaryFile(delete=False) as f:
        lock_path = f.name
    try:
        held = threading.Event()
        release_evt = threading.Event()

        def hold_lock():
            fd = acquire_lock(lock_path, LockPriority.P2_ACCUMULATOR)
            held.set()
            release_evt.wait()
            release_lock(fd)

        t = threading.Thread(target=hold_lock)
        t.start()
        held.wait()

        fd = acquire_lock(lock_path, LockPriority.P0_JUDGE)
        assert fd is None  # P0 skips lock, proceeds anyway

        release_evt.set()
        t.join()
    finally:
        os.unlink(lock_path)


def test_p1_times_out():
    """P1 (synthesizer) times out after short wait."""
    with tempfile.NamedTemporaryFile(delete=False) as f:
        lock_path = f.name
    try:
        held = threading.Event()
        release_evt = threading.Event()

        def hold_lock():
            fd = acquire_lock(lock_path, LockPriority.P2_ACCUMULATOR)
            held.set()
            release_evt.wait()
            release_lock(fd)

        t = threading.Thread(target=hold_lock)
        t.start()
        held.wait()

        t0 = time.monotonic()
        fd = acquire_lock(lock_path, LockPriority.P1_SYNTHESIZER, timeout_s=1)
        elapsed = time.monotonic() - t0
        assert fd is None
        assert elapsed >= 0.9

        release_evt.set()
        t.join()
    finally:
        os.unlink(lock_path)
