#!/usr/bin/env python3
"""Environment doctor for the ESoccer EV platform.

Checks toolchain versions, the backend virtualenv, the frontend
node_modules, and whether backend/.env exists with a BETSAPI_KEY set.
Never prints secret values -- only whether a key is present.
"""
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
FRONTEND = REPO / "frontend"
ENV_FILE = BACKEND / ".env"

ok = True


def check(label: str, passed: bool, detail: str = "") -> None:
    global ok
    status = "OK" if passed else "MISSING"
    print(f"[{status:7}] {label}" + (f" -- {detail}" if detail else ""))
    if not passed:
        ok = False


def tool_version(cmd: list[str]) -> str | None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or out.stderr.strip()
    except Exception:
        return None


def main() -> int:
    print("=== ESoccer EV doctor ===")
    print(f"Repo: {REPO}")
    print()

    py = tool_version([sys.executable, "--version"])
    check("python3", py is not None, py or "")

    node = tool_version(["node", "--version"])
    check("node", node is not None, node or "not found")

    npm = tool_version(["npm", "--version"])
    check("npm", npm is not None, npm or "not found")

    venv = BACKEND / ".venv"
    check("backend/.venv exists", venv.exists(), str(venv))

    node_modules = FRONTEND / "node_modules"
    check("frontend/node_modules exists", node_modules.exists(), str(node_modules))

    check("backend/.env exists", ENV_FILE.exists(), str(ENV_FILE))

    key_present = False
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith("BETSAPI_KEY=") or line.startswith("BETSAPI_TOKEN="):
                value = line.split("=", 1)[1].strip()
                if value and value != "PASTE_KEY_HERE":
                    key_present = True
    check("BETSAPI_KEY set (value not shown)", key_present)

    check("requirements.txt present", (BACKEND / "requirements.txt").exists())
    check("frontend package.json present", (FRONTEND / "package.json").exists())

    uvicorn_path = shutil.which("uvicorn") or (venv / "bin" / "uvicorn").exists()
    check("uvicorn available (venv or PATH)", bool(uvicorn_path))

    print()
    if ok:
        print("Doctor: all checks passed.")
    else:
        print("Doctor: some checks failed -- run `make setup` and add your BetsAPI key.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
