#!/usr/bin/env python3
"""Block commits that would publish secrets or personal data.

  python scripts/check_private.py          check files staged for commit (the pre-commit hook)
  python scripts/check_private.py --all    check every tracked file (CI, or a full manual scan)

Checks:
  1. Files that must never be committed (.env, databases, traces, local notes).
  2. Secret patterns: API keys, tokens, private keys.
  3. Personal terms listed in .private-terms (gitignored, one term per line, # for comments).
  4. Real memories: the text of every memory in memories.db, if the file exists.

Checks 3 and 4 only run where those local files exist, so CI checks 1 and 2.
Standard library only, so it runs without the project's virtualenv.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TERMS_FILE = ROOT / ".private-terms"
MEMORY_DB = ROOT / "memories.db"

FORBIDDEN = re.compile(r"(^|/)(\.env|CLAUDE\.md|local_run\.md|\.private-terms)$"
                       r"|\.(db|db-wal|db-shm|sqlite3?)$|(^|/)\.phoenix/")
SECRETS = {
    "Anthropic API key": r"sk-ant-[A-Za-z0-9_-]{20,}",
    "OpenAI API key": r"sk-(?:proj-)?[A-Za-z0-9_-]{32,}",
    "GitHub token": r"gh[pousr]_[A-Za-z0-9]{30,}",
    "AWS access key": r"AKIA[0-9A-Z]{16}",
    "private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "key assignment": r"(?i)\b[A-Z_]*(?:API_KEY|SECRET|TOKEN|PASSWORD)\s*=\s*['\"]?"
                      r"(?!your|sk-ant-\.\.\.|\.\.\.|<|\$)[A-Za-z0-9_\-]{16,}",
}


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                          check=True).stdout


def files_to_check(all_files: bool) -> list[tuple[str, str]]:
    """(path, content) pairs: the staged version, or the working copy with --all."""
    if all_files:
        paths = git("ls-files").splitlines()
        read = lambda p: (ROOT / p).read_bytes()  # noqa: E731
    else:
        paths = git("diff", "--cached", "--name-only", "--diff-filter=ACMR").splitlines()
        read = lambda p: subprocess.run(["git", "show", f":{p}"], cwd=ROOT,  # noqa: E731
                                        capture_output=True, check=True).stdout
    out = []
    for p in paths:
        try:
            data = read(p)
        except (OSError, subprocess.CalledProcessError):
            continue
        if b"\0" in data[:8000]:
            out.append((p, ""))  # binary: only the filename rule applies
        else:
            out.append((p, data.decode("utf-8", errors="replace")))
    return out


def private_patterns() -> dict[str, re.Pattern]:
    patterns = {}
    if TERMS_FILE.exists():
        for line in TERMS_FILE.read_text().splitlines():
            term = line.split("#", 1)[0].strip()
            if term:
                patterns[f"private term #{len(patterns) + 1}"] = re.compile(
                    rf"(?<!\w){re.escape(term)}(?!\w)", re.I)
    if MEMORY_DB.exists():
        try:
            db = sqlite3.connect(f"file:{MEMORY_DB}?mode=ro", uri=True)
            rows = db.execute("SELECT id, content FROM memories").fetchall()
        except sqlite3.Error:
            rows = []
        for mid, content in rows:
            text = " ".join(content.split())
            if len(text) >= 12:
                patterns[f"text of memory {mid}"] = re.compile(re.escape(text), re.I)
    return patterns


def main() -> int:
    all_files = "--all" in sys.argv
    checks = {name: re.compile(p) for name, p in SECRETS.items()} | private_patterns()
    problems = []
    for path, text in files_to_check(all_files):
        if FORBIDDEN.search(path):
            problems.append(f"{path}: this file must never be committed")
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for name, pattern in checks.items():
                if pattern.search(line):
                    problems.append(f"{path}:{lineno}: {name}")
    if problems:
        print("Blocked: possible secrets or personal data.\n", file=sys.stderr)
        print("\n".join(f"  {p}" for p in problems), file=sys.stderr)
        print("\nReplace real details with synthetic ones, then commit again.", file=sys.stderr)
        return 1
    scope = "tracked" if all_files else "staged"
    extra = [n for n, f in (("private terms", TERMS_FILE), ("memories", MEMORY_DB)) if f.exists()]
    print(f"check_private: {scope} files clean (secrets{''.join(', ' + e for e in extra)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
