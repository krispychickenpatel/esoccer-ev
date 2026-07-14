"""v0.3.7D.4 Task 12: single-instance lock -- acquire/release, stale-lock
detection, and the concurrent-invocation guarantee (only one caller ever
acquires)."""
import os
import sys
import threading
from pathlib import Path

import pytest

SCRIPTS_OPS = Path(__file__).resolve().parent.parent.parent / "scripts" / "ops"
sys.path.insert(0, str(SCRIPTS_OPS))

from unattended_lock import AlreadyRunning, UnattendedLock  # noqa: E402


def test_acquire_and_release(tmp_path):
    lock_path = tmp_path / "run.lock"
    lock = UnattendedLock(lock_path, run_id="r1", repo_path="/repo")
    lock.acquire()
    assert lock_path.exists()
    lock.release()
    assert not lock_path.exists()


def test_live_holder_blocks_second_acquire(tmp_path):
    lock_path = tmp_path / "run.lock"
    lock1 = UnattendedLock(lock_path, run_id="r1", repo_path="/repo")
    lock1.acquire()
    lock2 = UnattendedLock(lock_path, run_id="r2", repo_path="/repo")
    with pytest.raises(AlreadyRunning):
        lock2.acquire()
    lock1.release()


def test_stale_lock_is_cleaned_up_after_verifying_pid_dead(tmp_path):
    lock_path = tmp_path / "run.lock"
    import json
    import time
    # A PID that is essentially guaranteed not to exist (very large, unlikely
    # to be reused) -- simulates a crashed prior run's leftover lock.
    dead_pid = 999999
    lock_path.write_text(json.dumps({"pid": dead_pid, "run_id": "stale", "repo_path": "/repo",
                                     "created_at": time.time() - 10000}))
    lock2 = UnattendedLock(lock_path, run_id="r2", repo_path="/repo")
    lock2.acquire()  # must not raise -- stale lock is cleaned up first
    assert lock_path.exists()
    holder = __import__("json").loads(lock_path.read_text())
    assert holder["pid"] == os.getpid()
    lock2.release()


def test_never_deletes_a_lock_it_does_not_own(tmp_path):
    """release() must be a no-op (never delete) if the file on disk no
    longer matches this instance's own pid/run_id -- e.g. another process
    already cleaned it up, or (defensively) it was somehow overwritten."""
    lock_path = tmp_path / "run.lock"
    lock = UnattendedLock(lock_path, run_id="r1", repo_path="/repo")
    lock.acquire()
    # Simulate the file having been replaced by someone else since acquire().
    import json
    lock_path.write_text(json.dumps({"pid": 123456, "run_id": "not-mine", "repo_path": "/repo",
                                     "created_at": 0}))
    lock.release()
    assert lock_path.exists()  # not deleted -- it isn't ours anymore


def test_concurrent_invocation_only_one_starts(tmp_path):
    lock_path = tmp_path / "run.lock"
    results = []
    barrier = threading.Barrier(8)

    def worker(i):
        barrier.wait()
        lock = UnattendedLock(lock_path, run_id=f"r{i}", repo_path="/repo")
        try:
            lock.acquire()
            results.append(("acquired", i))
        except AlreadyRunning:
            results.append(("blocked", i))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    acquired = [r for r in results if r[0] == "acquired"]
    blocked = [r for r in results if r[0] == "blocked"]
    assert len(acquired) == 1, f"expected exactly one acquire, got {acquired}"
    assert len(blocked) == 7


def test_unreadable_lock_file_fails_closed(tmp_path):
    lock_path = tmp_path / "run.lock"
    lock_path.write_text("not json at all {{{")
    lock2 = UnattendedLock(lock_path, run_id="r2", repo_path="/repo")
    with pytest.raises(AlreadyRunning):
        lock2.acquire()
