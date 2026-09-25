#!/usr/bin/env python3
"""
Regenerate SOURCE_CODE_BUNDLE.py — every Python source file in one document,
for reviewers who want to read the whole project without cloning it.

    python scripts/build_bundle.py

Run this after any change to the files listed in ORDER, so the bundle never
drifts from the real code.
"""
from __future__ import annotations
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "SOURCE_CODE_BUNDLE.py")

# Reading order: entry point, config, then data in → scoring → pipeline → web.
ORDER = [
    "run.py",
    "export_static.py",
    "config/sectors.py",
    "config/settings.py",
    "config/tickers.py",
    "src/__init__.py",
    "src/collectors.py",
    "src/sentiment.py",
    "src/pipeline.py",
    "src/storage.py",
    "src/utils.py",
    "src/dashboard/__init__.py",
    "src/dashboard/app.py",
    "api/chat.py",
    "api/verify.py",
    "scripts/backfill_rescore.py",
    "scripts/refresh_once.py",
    "scripts/build_bundle.py",
]

RULE = "# " + "=" * 76
THIN = "# " + "─" * 76


def main() -> None:
    parts = [
        RULE,
        "# SentimentIQ — COMPLETE SOURCE CODE BUNDLE (read-only reference)",
        "# All Python source in one file. Run the project with: bash start.sh",
        f"# {len(ORDER)} source files. Regenerate with: python scripts/build_bundle.py",
        RULE,
        "",
    ]
    for rel in ORDER:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            print(f"  ! skipping missing {rel}")
            continue
        with open(path) as f:
            body = f.read().rstrip("\n")
        parts += ["", THIN, f"# FILE: {rel}", THIN, "", body, ""]
    with open(OUT, "w") as f:
        f.write("\n".join(parts) + "\n")
    print(f"wrote {OUT} ({len(ORDER)} files, {sum(1 for _ in open(OUT)):,} lines)")


if __name__ == "__main__":
    main()
