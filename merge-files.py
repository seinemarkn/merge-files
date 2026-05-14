#!/usr/bin/env python3
"""
merge_files.py — Concatenate code/config/text files (in the order given) into one output.

Usage:
    python merge_files.py file1.py file2.rb config.yml
    python merge_files.py src/*.py -o all_python.txt
    python merge_files.py a.md b.md c.md --no-banners
    python merge_files.py my_project/        # whole folder, recursive

Directory arguments are expanded recursively into their contained files
(sorted alphabetically by path). Hidden entries (dot-prefixed) ARE included
because they're often project config (.env.example, .eslintrc, .github/,
etc.). Symlinks are skipped during expansion to avoid loops.

Each file gets a header banner showing its name and path so sections are easy to spot
when you paste the merged output into another tool (LLM context, code review, etc.).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def make_banner(
    path: Path,
    index: int,
    total: int,
    lines: int,
    sha: str,
    sha_normalized: str,
) -> str:
    """Return a section header banner for a single file in the merged output.

    `sha` is the full SHA-256 hex of the file's raw bytes.
    `sha_normalized` is SHA-256 of (text.strip() + "\\n") — lets consumers
    detect transcription that's correct modulo leading/trailing whitespace.
    """
    bar = "=" * 72
    return (
        f"\n{bar}\n"
        f"# File {index} of {total}: {path.name}\n"
        f"# Path: {path}\n"
        f"# Lines: {lines:,}\n"
        f"# SHA-256: {sha}\n"
        f"# SHA-256-normalized: {sha_normalized}\n"
        f"{bar}\n\n"
    )


def expand_paths(paths: list[Path]) -> list[Path]:
    """Expand any directory entries in `paths` into their contained files.

    Files are returned as-is, preserving the order in which they appear in
    `paths`. Directories are walked recursively (depth-first); the files
    discovered inside each directory are sorted alphabetically by path so the
    output ordering is deterministic regardless of filesystem iteration order.

    Hidden (dot-prefixed) files and directories ARE included — they're often
    legitimate project config (.env.example, .eslintrc, .github/, etc.).
    Symlinks are skipped during expansion to dodge cycles. A non-existent
    path passed directly is left in the list so merge() can warn about it.
    """
    result: list[Path] = []
    for p in paths:
        if p.is_dir() and not p.is_symlink():
            files_in_dir: list[Path] = []
            for dirpath, dirnames, filenames in os.walk(p, followlinks=False):
                dirnames.sort()
                for fname in sorted(filenames):
                    fpath = Path(dirpath) / fname
                    if fpath.is_symlink():
                        continue
                    files_in_dir.append(fpath)
            files_in_dir.sort()
            result.extend(files_in_dir)
        else:
            result.append(p)
    return result


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
                sha = hashlib.sha256(data).hexdigest()
                normalized_bytes = (text.strip() + "\n").encode("utf-8")
                sha_normalized = hashlib.sha256(normalized_bytes).hexdigest()
                banner = make_banner(
                    path, index, total, line_count, sha, sha_normalized
                )
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
    parser.add_argument(
        "files",
        nargs="+",
        type=Path,
        help=(
            "Files and/or directories to merge, in order. Directories are "
            "expanded recursively (alphabetically), including hidden "
            "(dot-prefixed) entries; symlinks are skipped to avoid cycles."
        ),
    )
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

    files = expand_paths(args.files)
    if not files:
        print("No files to merge (after expanding any folder arguments).", file=sys.stderr)
        return 1

    print(f"Merging {len(files)} file(s) → {args.output}\n")
    merge(files=files, output=args.output, no_banners=args.no_banners)

    if sys.platform == "darwin":
        try:
            subprocess.run(["open", str(args.output)], check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as err:
            print(f"  ⚠️  Could not open file automatically: {err}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
