"""
Make a clean copy of this folder to publish.

    python tools/make_release.py                 -> "<this folder> - release" beside it
    python tools/make_release.py D:\\out\\Mark58   -> that folder (must not exist yet)

WHY, WHEN .gitignore ALREADY LISTS THE PRIVATE FILES
    .gitignore only protects what goes through git. A folder dragged into a
    GitHub upload, zipped, or copied to a friend takes EVERYTHING with it: the
    API keys (config/api_keys.json), the assistant's memory of you, your
    routines, and logs/ — which holds transcripts of what you said to it.

WHAT IT DOES
    1. Copies exactly the files git would publish: tracked files plus new ones,
       minus everything .gitignore excludes (`git ls-files -co --exclude-standard`).
       Tool folders of the editor and of coding assistants (.claude, .vscode,
       .idea) are left out as well.
    2. Scans the copy for anything that still looks private: API-key shapes
       (Google "AIza…", OpenAI-style "sk-…"), token and credential files, and
       this computer's user name or home folder written into a file.
       Every hit is listed, and the copy is marked NOT READY until they are gone.

Nothing in the original folder is changed or deleted.
"""
from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SKIP_DIRS = (".git/", ".claude/", ".vscode/", ".idea/")
_SECRET_FILES = re.compile(r"(api_keys\.json|token[^/]*\.json|client_secret[^/]*\.json|"
                           r"credentials[^/]*\.json|\.env(\..*)?)$", re.IGNORECASE)
_KEY_SHAPES = [
    ("a Google API key", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    ("an OpenAI-style secret key", re.compile(r"\bsk-[A-Za-z0-9_\-]{24,}")),
]
_TEXT = {".py", ".pyw", ".txt", ".md", ".json", ".toml", ".cfg", ".ini", ".yaml",
         ".yml", ".html", ".js", ".css", ".bat", ".sh", ".ps1", ".xml", ".csv"}


def publishable_files() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"],
                             cwd=ROOT, capture_output=True, check=True)
    except FileNotFoundError:
        sys.exit("git is needed to read .gitignore: install it from https://git-scm.com")
    except subprocess.CalledProcessError as e:
        sys.exit(f"git could not list the files: {e.stderr.decode(errors='replace').strip()}")
    files = [f for f in out.stdout.decode("utf-8", "replace").split("\0") if f]
    return [f for f in files if not f.replace("\\", "/").startswith(_SKIP_DIRS)
            and (ROOT / f).is_file()]


def scan(dest: Path, files: list[str]) -> list[str]:
    """Anything in the copy that still looks private."""
    problems = []
    user = ""
    try:
        user = getpass.getuser()
    except Exception:
        pass
    home = str(Path.home())
    markers = [m for m in {home, home.replace("\\", "/"), home.replace("\\", "\\\\")} if m]
    for rel in files:
        if _SECRET_FILES.search(rel.replace("\\", "/")):
            problems.append(f"{rel}: a key or credential file")
            continue
        path = dest / rel
        if path.suffix.lower() not in _TEXT or path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for label, rx in _KEY_SHAPES:
            if rx.search(text):
                problems.append(f"{rel}: contains what looks like {label}")
        for m in markers:
            if m in text:
                problems.append(f"{rel}: contains this computer's home folder ({m})")
                break
        else:
            if user and len(user) >= 4 and re.search(rf"[\\/]{re.escape(user)}[\\/]", text):
                problems.append(f"{rel}: contains a path with your user name ({user})")
    return problems


def main() -> int:
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.with_name(ROOT.name + " - release")
    if dest.exists():
        sys.exit(f"{dest} already exists - choose another folder or delete it first.")
    files = publishable_files()
    for rel in files:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, target)
    left_out = sum(1 for _ in ROOT.rglob("*") if _.is_file()) - len(files)
    print(f"Copied {len(files)} files to {dest}")
    print(f"Left out {left_out} files: private data, caches, logs, .git and editor folders.")
    problems = scan(dest, files)
    if problems:
        print("\nNOT READY - remove or fix these in the copy before publishing:")
        for p in problems:
            print("  -", p)
        return 1
    print("\nNo keys, credential files or personal paths found. Ready to publish.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
