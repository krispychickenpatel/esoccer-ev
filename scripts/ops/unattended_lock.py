"""v0.3.7D.4 Task 2: single-instance file lock for unattended orchestration.

Contains PID + run identifier + creation timestamp + repository path, so a
stale lock can be told apart from a live one without ever deleting a lock
file blindly. Import-only module -- no side effects at import time, no
`input()`, no network/API calls.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


class AlreadyRunning(Exception):
    def __init__(self, holder: dict):
        self.holder = holder
        super().__init__(f"another unattended run is already active: {holder}")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists, just owned by someone else -- still alive
    else:
        return True


class UnattendedLock:
    """Usage:
        lock = UnattendedLock(lock_path, run_id=run_id, repo_path=repo_dir)
        lock.acquire()
        try:
            ...
        finally:
            lock.release()
    """

    def __init__(self, path: Path, run_id: str, repo_path: str):
        self.path = Path(path)
        self.run_id = run_id
        self.repo_path = repo_path
        self._acquired = False

    def _read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def _write_atomic(self) -> None:
        payload = {
            "pid": os.getpid(),
            "run_id": self.run_id,
            "repo_path": self.repo_path,
            "created_at": time.time(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # O_CREAT|O_EXCL is the atomic, race-free "create only if absent"
        # primitive on a local filesystem -- this is what makes concurrent
        # invocations safe rather than merely usually-safe.
        fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            os.write(fd, json.dumps(payload).encode())
        finally:
            os.close(fd)

    def acquire(self) -> None:
        try:
            self._write_atomic()
            self._acquired = True
            return
        except FileExistsError:
            pass

        holder = self._read()
        if holder is None:
            # Unreadable/corrupt lock file -- do not assume it is safe to
            # remove; a concurrent writer could be mid-write. Fail closed.
            raise AlreadyRunning({"error": "lock file exists but is unreadable"})

        holder_pid = holder.get("pid")
        if isinstance(holder_pid, int) and _pid_alive(holder_pid):
            raise AlreadyRunning(holder)

        # Stale lock: recorded PID is confirmed dead. Safe to clean up and
        # retry exactly once -- never loop indefinitely, never remove a lock
        # without having verified liveness first.
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        try:
            self._write_atomic()
            self._acquired = True
        except FileExistsError:
            # Lost a race against another process cleaning up the same
            # stale lock at the same moment -- fail closed rather than retry
            # forever.
            raise AlreadyRunning(self._read() or {"error": "lock re-created by a concurrent process"})

    def release(self) -> None:
        if not self._acquired:
            return
        holder = self._read()
        # Only ever remove a lock file that is provably ours -- never a
        # blind unlink, even in the common/expected case.
        if holder is not None and holder.get("pid") == os.getpid() and holder.get("run_id") == self.run_id:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self._acquired = False
