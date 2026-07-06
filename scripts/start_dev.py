#!/usr/bin/env python3
"""Start backend (uvicorn) and frontend (vite) dev servers together.

Backend:  http://127.0.0.1:8000
Frontend: http://127.0.0.1:5173

Ctrl-C stops both.
"""
import shutil
import signal
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
FRONTEND = REPO / "frontend"


def venv_bin(name: str) -> str:
    candidate = BACKEND / ".venv" / "bin" / name
    return str(candidate) if candidate.exists() else name


def main() -> int:
    if not (BACKEND / ".venv").exists():
        print("backend/.venv not found -- run `make setup` first.", file=sys.stderr)
        return 1
    if not (FRONTEND / "node_modules").exists():
        print("frontend/node_modules not found -- run `make setup` first.", file=sys.stderr)
        return 1

    procs = []
    try:
        backend_proc = subprocess.Popen(
            [venv_bin("uvicorn"), "app.main:app", "--reload",
             "--host", "127.0.0.1", "--port", "8000"],
            cwd=BACKEND,
        )
        procs.append(backend_proc)
        print("Backend starting at http://127.0.0.1:8000")

        npm = shutil.which("npm") or "npm"
        frontend_proc = subprocess.Popen([npm, "run", "dev", "--", "--host", "127.0.0.1"], cwd=FRONTEND)
        procs.append(frontend_proc)
        print("Frontend starting at http://127.0.0.1:5173")

        for p in procs:
            p.wait()
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            if p.poll() is None:
                p.send_signal(signal.SIGINT)
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
