#!/usr/bin/env python3
"""
merge_files.py — Concatenate code/config/text files (in the order given) into one output.

Usage:
    python merge_files.py file1.py file2.rb config.yml
    python merge_files.py src/*.py -o all_python.txt
    python merge_files.py a.md b.md c.md --no-banners

Each file gets a header banner showing its name and path so sections are easy to spot
when you paste the merged output into another tool (LLM context, code review, etc.).
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def make_banner(path: Path, index: int, total: int, lines: int, sha: str) -> str:
    """Return a section header banner for a single file in the merged output."""
    bar = "=" * 72
    return (
        f"\n{bar}\n"
        f"# File {index} of {total}: {path.name}\n"
        f"# Path: {path}\n"
        f"# Lines: {lines:,}\n"
        f"# SHA-256: {sha}\n"
        f"{bar}\n\n"
    )


def merge(files: list[Path], output: Path, no_banners: bool = False) -> None:
    """Concatenate `files` into `output`, optionally inserting header banners."""
    total = len(files)
    written_bytes = 0
    written_files = 0

    with output.open("w", encoding="utf-8") as out:
        for index, path in enumerate(files, start=1):
            if not path.exists():
                print(f"  ⚠️  Skipping (not found): {path}", file=sys.stderr)
                continue
            if not path.is_file():
                print(f"  ⚠️  Skipping (not a file): {path}", file=sys.stderr)
                continue

            try:
                data = path.read_bytes()
            except OSError as err:
                print(f"  ⚠️  Read error on {path}: {err}", file=sys.stderr)
                continue
            text = data.decode("utf-8", errors="replace")

            if not no_banners:
                line_count = len(text.splitlines())
                sha = hashlib.sha256(data).hexdigest()[:12]
                banner = make_banner(path, index, total, line_count, sha)
                out.write(banner)
                written_bytes += len(banner.encode("utf-8"))

            if not text.endswith("\n"):
                text += "\n"

            out.write(text)
            written_bytes += len(text.encode("utf-8"))
            written_files += 1
            print(f"  ✓ [{index}/{total}] {path.name} ({len(text):,} chars)")

    print(f"\n✅ Merged {written_files}/{total} file(s), {written_bytes:,} bytes")
    print(f"   Output: {output}")


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Concatenate text/code/config files in order into one output file.",
    )
    parser.add_argument("files", nargs="+", type=Path, help="Files to merge, in order.")
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=None,
        help="Output file path. Defaults to ~/tmp/merges/file-merge-<timestamp>.txt",
    )
    parser.add_argument(
        "--no-banners",
        action="store_true",
        help="Don't insert section header banners between files.",
    )
    return parser.parse_args()


def main() -> int:
    """Entry point: parse args, resolve default output path, run merge."""
    args = parse_args()

    if args.output is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        merges_dir = Path.home() / "tmp" / "merges"
        args.output = merges_dir / f"file-merge-{timestamp}.txt"

    args.output.parent.mkdir(parents=True, exist_ok=True)

    print(f"Merging {len(args.files)} file(s) → {args.output}\n")
    merge(files=args.files, output=args.output, no_banners=args.no_banners)

    if sys.platform == "darwin":
        try:
            subprocess.run(["open", str(args.output)], check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as err:
            print(f"  ⚠️  Could not open file automatically: {err}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
