#!/usr/bin/env python3
"""Import a new esoccer-ev release zip as the active repo.

Backs up the current active repo, extracts the given zip, replaces the
active repo contents, then restores backend/.env and any data/seed or
data/samples CSVs the new release doesn't ship with. Never overwrites an
existing .env or database file.

Usage:
    python scripts/import_release.py --zip /path/to/esoccer-ev-vX.Y.Z.zip
"""
import argparse
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent          # .../current/esoccer-ev
ESOCCER_ROOT = REPO.parent.parent                        # .../ESoccer
BACKUPS = ESOCCER_ROOT / "backups"


def find_project_root(extract_dir: Path) -> Path:
    """A release zip may contain a top-level esoccer-ev/ wrapper, or not."""
    candidates = [p for p in extract_dir.iterdir() if p.is_dir()]
    if len(candidates) == 1 and (candidates[0] / "backend").exists():
        return candidates[0]
    if (extract_dir / "backend").exists():
        return extract_dir
    raise SystemExit(f"Could not find a backend/ dir under {extract_dir}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", required=True, help="Path to the release zip")
    args = ap.parse_args()

    zip_path = Path(args.zip).expanduser().resolve()
    if not zip_path.exists():
        print(f"Zip not found: {zip_path}", file=sys.stderr)
        return 1

    BACKUPS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    preserved_env = None
    preserved_data = []
    if REPO.exists():
        backup_path = BACKUPS / f"esoccer-ev-preimport_{stamp}.tar.gz"
        print(f"Backing up current active repo to {backup_path}")
        shutil.make_archive(str(backup_path).removesuffix(".tar.gz"), "gztar",
                             root_dir=REPO.parent, base_dir=REPO.name)

        env_file = REPO / "backend" / ".env"
        if env_file.exists():
            preserved_env = env_file.read_bytes()

        data_dir = REPO / "data"
        if data_dir.exists():
            for sub in ("seed", "samples"):
                p = data_dir / sub
                if p.exists():
                    tmp = Path(tempfile.mkdtemp()) / sub
                    shutil.copytree(p, tmp)
                    preserved_data.append((sub, tmp))

    with tempfile.TemporaryDirectory() as tmp_extract:
        tmp_extract = Path(tmp_extract)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmp_extract)
        project_root = find_project_root(tmp_extract)

        if REPO.exists():
            moved_aside = REPO.with_name(REPO.name + f".preimport-{stamp}")
            REPO.rename(moved_aside)
            print(f"Moved previous active repo aside: {moved_aside}")

        shutil.copytree(project_root, REPO)
        print(f"Installed new release at {REPO}")

    if preserved_env is not None:
        env_target = REPO / "backend" / ".env"
        if not env_target.exists():
            env_target.write_bytes(preserved_env)
            print(f"Restored preserved backend/.env -> {env_target}")
        else:
            print(f"New release already ships backend/.env -- left it untouched at {env_target}")

    for sub, tmp in preserved_data:
        target = REPO / "data" / sub
        if not target.exists():
            shutil.copytree(tmp, target)
            print(f"Restored data/{sub}")

    print("Import complete. Run `make doctor` and `make smoke` to verify.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
