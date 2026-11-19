#!/usr/bin/env python3
"""Pre-commit / pre-push secret scan.

Scans every file git would commit (tracked + untracked-but-not-ignored) for
credential patterns, and verifies that sensitive paths are ignored.
Prints file:line and the pattern name only -- never the matched value.
Exit code 1 on any finding.

Usage:  uv run python scripts/secret_scan.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PATTERNS: dict[str, re.Pattern[str]] = {
    "anthropic_key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{30,}"),
    "x_bearer": re.compile(r"AAAAAAAAAAAAAAAAAAAA[A-Za-z0-9%]{30,}"),
    "private_key_block": re.compile(r"-----BEGIN (RSA |EC |OPENSSH |)PRIVATE KEY-----"),
    "aws_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "slack_token": re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}"),
    "assigned_secret": re.compile(
        r"(?i)\b(api[_-]?key|api[_-]?secret|access[_-]?token(_secret)?|bearer[_-]?token|"
        r"client[_-]?secret|password)\b\s*[:=]\s*['\"]?([A-Za-z0-9_\-./+=]{16,})"
    ),
    "solana_keypair_array": re.compile(r"\[\s*(\d{1,3}\s*,\s*){63}\d{1,3}\s*\]"),
}

# Paths that must be ignored by git.
MUST_BE_IGNORED = [".env", "data/liquidmexx.db", "data/backups/x.db", "logs/x.log",
                   "backups/x.db", "x.db"]

SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".lock"}


def git(*args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True  # noqa: S607
    ).stdout


def candidate_files() -> list[str]:
    out = git("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return [p for p in out.split("\0") if p]


def main() -> int:
    findings: list[str] = []

    for path in MUST_BE_IGNORED:
        result = subprocess.run(  # noqa: S603
            ["git", "check-ignore", "-q", path], cwd=ROOT, check=False  # noqa: S607
        )
        if result.returncode != 0:
            findings.append(f"NOT IGNORED: {path} would be committable")

    files = candidate_files()
    for rel in files:
        name = Path(rel).name
        if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
            findings.append(f"{rel}: env file is committable")
            continue
        path = ROOT / rel
        if path.suffix in SKIP_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "secret-scan: allow" in line:
                continue
            for label, pattern in PATTERNS.items():
                if pattern.search(line):
                    findings.append(f"{rel}:{lineno}: possible {label}")

    if findings:
        print("SECRET SCAN FAILED")
        for f in findings:
            print("  " + f)
        return 1
    print(f"secret scan OK: {len(files)} committable files checked, sensitive paths ignored")
    return 0


if __name__ == "__main__":
    sys.exit(main())
