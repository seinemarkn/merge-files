#!/usr/bin/env python3
"""
merge-files.py — Concatenate code/config/text files (in the order given) into one
or more merge files, splitting at a configurable line cap.

Usage:
    python merge-files.py file1.py file2.rb config.yml
    python merge-files.py src/ -o all_python.txt
    python merge-files.py --max-lines 2000 *.py
    python merge-files.py a.md b.md c.md --no-banners

Directory arguments are expanded recursively into their contained files
(sorted alphabetically by path). Hidden entries (dot-prefixed) ARE included
because they're often project config (.env.example, .eslintrc, .github/,
etc.). Symlinks are skipped during expansion to avoid loops, and macOS
.DS_Store files are always excluded (see EXCLUDED_FILENAMES below).

Output format (see MERGE-FORMAT.md for the full consumer-facing spec):
  - Each file is preceded by a 2-line banner.
  - When total content exceeds --max-lines, output is split across multiple
    merge files. Splits occur strictly between lines; reassembly is plain
    concatenation of parts in part= order.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Each banner is always exactly 2 lines. This is part of the output contract
# (see MERGE-FORMAT.md): the bin-packer must subtract this from the cap to
# know how much room is available for content in each merge file.
BANNER_LINES = 2

# Default line cap per merge file (banners included). 3,500 is sized to keep
# each merge file inside the single-agent comfort zone for downstream
# consumption — adjustable via --max-lines.
DEFAULT_MAX_LINES = 3500

# Minimum allowed --max-lines. Below this the 2-line banner overhead leaves
# almost no room for content, and the split logic produces absurd outputs.
MIN_MAX_LINES = 10

# Files always skipped during folder expansion regardless of dot-prefix rules.
# .DS_Store is macOS Finder metadata that's never intentional project content,
# so we drop it even though we otherwise include hidden files. Add to this set
# if more "always-junk" filenames come up.
EXCLUDED_FILENAMES = frozenset({".DS_Store"})


def make_banner(
    path: Path,
    index: int,
    total: int,
    lines: int,
    sha: str,
    sha_normalized: str,
) -> str:
    """Return the 2-line banner for a whole (unsplit) file.

    Line 1: `=== [N/M] /full/path · K lines`
    Line 2: `=== sha=<64hex>  sha-norm=<64hex>`
    """
    return (
        f"=== [{index}/{total}] {path} · {lines} lines\n"
        f"=== sha={sha}  sha-norm={sha_normalized}\n"
    )


def make_split_banner(
    path: Path,
    index: int,
    total: int,
    chunk_lines: int,
    sha: str,
    part_n: int,
    part_total: int,
    range_start: int,
    range_end: int,
    file_total_lines: int,
) -> str:
    """Return the 2-line banner for one chunk of a split file.

    Line 1: `=== [N/M] /full/path · K lines`  (K = lines in this chunk)
    Line 2: `=== sha=<64hex>  part=P/Q  range=A-B/T`
      sha is the WHOLE file's SHA, identical across all parts.
      part=P/Q is this chunk's position among the file's chunks.
      range=A-B/T is the 1-indexed inclusive line range A..B within a
      file of T total lines. Invariant: K == B - A + 1.
    """
    return (
        f"=== [{index}/{total}] {path} · {chunk_lines} lines\n"
        f"=== sha={sha}  part={part_n}/{part_total}  "
        f"range={range_start}-{range_end}/{file_total_lines}\n"
    )


@dataclass
class FileRecord:
    """Decoded contents of one input file, plus identity info for banners.

    `lines` holds the content as a list of line strings *with their trailing
    newlines preserved* (output of splitlines(keepends=True) after we've
    forced a trailing newline). Splitting a file = slicing this list.
    """
    path: Path
    index: int
    total: int
    lines: list[str]
    sha: str
    sha_normalized: str


@dataclass
class Chunk:
    """One unit of (banner + content) destined for a single merge file.

    For whole-file chunks, part_n/part_total/range_* are all None.
    For split chunks, they're all set. The is_split property is the
    canonical way to distinguish the two.
    """
    record: FileRecord
    content_lines: list[str]
    part_n: int | None = None
    part_total: int | None = None
    range_start: int | None = None
    range_end: int | None = None

    @property
    def total_lines(self) -> int:
        return BANNER_LINES + len(self.content_lines)

    @property
    def is_split(self) -> bool:
        return self.part_n is not None

    def render(self) -> str:
        if self.is_split:
            assert self.part_total is not None
            assert self.range_start is not None
            assert self.range_end is not None
            banner = make_split_banner(
                path=self.record.path,
                index=self.record.index,
                total=self.record.total,
                chunk_lines=len(self.content_lines),
                sha=self.record.sha,
                part_n=self.part_n,
                part_total=self.part_total,
                range_start=self.range_start,
                range_end=self.range_end,
                file_total_lines=len(self.record.lines),
            )
        else:
            banner = make_banner(
                path=self.record.path,
                index=self.record.index,
                total=self.record.total,
                lines=len(self.content_lines),
                sha=self.record.sha,
                sha_normalized=self.record.sha_normalized,
            )
        return banner + "".join(self.content_lines)


def expand_paths(paths: list[Path]) -> list[Path]:
    """Expand any directory entries in `paths` into their contained files.

    Files are returned as-is, preserving the order in which they appear in
    `paths`. Directories are walked recursively (depth-first); the files
    discovered inside each directory are sorted alphabetically by path so the
    output ordering is deterministic regardless of filesystem iteration order.

    Hidden (dot-prefixed) files and directories ARE included — they're often
    legitimate project config (.env.example, .eslintrc, .github/, etc.).
    Symlinks are skipped during expansion to dodge cycles, and filenames in
    EXCLUDED_FILENAMES (e.g. .DS_Store) are always dropped. A non-existent
    path passed directly is left in the list so read_records() can warn about it.
    """
    result: list[Path] = []
    for p in paths:
        if p.is_dir() and not p.is_symlink():
            files_in_dir: list[Path] = []
            for dirpath, dirnames, filenames in os.walk(p, followlinks=False):
                dirnames.sort()
                for fname in sorted(filenames):
                    if fname in EXCLUDED_FILENAMES:
                        continue
                    fpath = Path(dirpath) / fname
                    if fpath.is_symlink():
                        continue
                    files_in_dir.append(fpath)
            files_in_dir.sort()
            result.extend(files_in_dir)
        else:
            result.append(p)
    return result


def read_records(files: list[Path]) -> list[FileRecord]:
    """Read each file, decode UTF-8 (lossily), compute SHAs, and number them.

    Files that don't exist, aren't regular files, or fail to read are skipped
    with a stderr warning. The returned list is renumbered so index/total
    reflect the successfully-read set — consumers never see gaps from skips.
    """
    raw: list[tuple[Path, list[str], str, str]] = []
    for path in files:
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
        # Force a trailing newline so split boundaries are always between
        # complete lines. Without this, a file whose last line has no \n
        # would put part-2's first line on the same physical line as the
        # part-1 banner's preceding content during reassembly.
        if text and not text.endswith("\n"):
            text += "\n"
        lines = text.splitlines(keepends=True)
        sha = hashlib.sha256(data).hexdigest()
        normalized_bytes = (text.strip() + "\n").encode("utf-8")
        sha_norm = hashlib.sha256(normalized_bytes).hexdigest()
        raw.append((path, lines, sha, sha_norm))

    total = len(raw)
    return [
        FileRecord(path=p, index=i, total=total, lines=ls, sha=s, sha_normalized=n)
        for i, (p, ls, s, n) in enumerate(raw, start=1)
    ]


def plan_chunks(records: list[FileRecord], cap: int) -> list[list[Chunk]]:
    """Bin-pack records into merge files, splitting files that exceed cap.

    Rules (all locked in via the design Q&A; see MERGE-FORMAT.md):
      - A whole-file chunk (banner + content) that fits in the current merge
        file's remaining space is appended to it.
      - A whole-file chunk that doesn't fit closes the current merge file and
        starts a new one.
      - A file that doesn't fit even in a fresh merge file (i.e. needs
        splitting) ALWAYS starts at the beginning of a fresh merge file. The
        current one is closed first if it has content.
      - Non-final parts of a split file each fully occupy their own merge
        file (banner + content == cap).
      - The FINAL part of a split file (part=N/N) can be followed by more
        whole-file chunks in the same merge file, subject to the cap.
    """
    if cap <= BANNER_LINES:
        raise ValueError(
            f"cap must exceed banner overhead ({BANNER_LINES}); got {cap}"
        )

    merge_files: list[list[Chunk]] = [[]]
    content_per_part = cap - BANNER_LINES

    def current_used() -> int:
        return sum(c.total_lines for c in merge_files[-1])

    def start_new() -> None:
        merge_files.append([])

    for record in records:
        whole_size = BANNER_LINES + len(record.lines)
        if whole_size <= cap:
            if whole_size <= cap - current_used():
                merge_files[-1].append(Chunk(record=record, content_lines=record.lines))
            else:
                start_new()
                merge_files[-1].append(Chunk(record=record, content_lines=record.lines))
        else:
            # Must split. Split files always start in a fresh merge file.
            if merge_files[-1]:
                start_new()
            n_lines = len(record.lines)
            num_parts = (n_lines + content_per_part - 1) // content_per_part
            for part_idx in range(num_parts):
                if part_idx > 0:
                    start_new()
                start = part_idx * content_per_part
                end = min(start + content_per_part, n_lines)
                merge_files[-1].append(Chunk(
                    record=record,
                    content_lines=record.lines[start:end],
                    part_n=part_idx + 1,
                    part_total=num_parts,
                    range_start=start + 1,
                    range_end=end,
                ))

    return [mf for mf in merge_files if mf]


def output_paths(base: Path, count: int) -> list[Path]:
    """Generate one output path per merge file.

    - count == 1: return `[base]` unchanged (zero behavior change from the
      pre-split tool — small merges keep their familiar filename).
    - count > 1: insert `-partNN` before `base.suffix` (the final extension).
      Padding width is `max(2, len(str(count)))` — so a 5-part batch gets
      `-part01..-part05` and a 250-part batch gets `-part001..-part250`,
      both internally consistent.

    Multi-dot extension caveat (e.g. `-o foo.tar.gz`): pathlib's `.suffix`
    only catches the final dot, so `foo.tar.gz` splits to `foo.tar-part01.gz`.
    That's the documented, unambiguous rule — users with compound extensions
    can pass `-o foo` (no extension) and let the tool generate plain names.
    """
    if count <= 1:
        return [base]
    width = max(2, len(str(count)))
    stem = base.stem
    suffix = base.suffix
    parent = base.parent
    return [
        parent / f"{stem}-part{str(i + 1).zfill(width)}{suffix}"
        for i in range(count)
    ]


def merge(
    files: list[Path],
    output_base: Path,
    max_lines: int = DEFAULT_MAX_LINES,
    no_banners: bool = False,
) -> list[Path]:
    """Read input files, plan chunks, write one or more merge files.

    Returns the list of output paths actually written. Single-output runs
    return a one-element list using `output_base` unchanged.

    `--no-banners` mode forces a single output file with no banners and
    no splitting — splits without banners can't be reassembled, so the cap
    is ignored in that mode.
    """
    records = read_records(files)
    if not records:
        print("\n⚠️  No files successfully read.", file=sys.stderr)
        return []

    input_count = len(files)
    merged_count = len(records)

    if no_banners:
        with output_base.open("w", encoding="utf-8") as out:
            for rec in records:
                out.writelines(rec.lines)
        total_lines = sum(len(r.lines) for r in records)
        print(f"\n✅ Merged {merged_count}/{input_count} file(s), "
              f"{total_lines:,} content lines (no banners)")
        print(f"   Output: {output_base}")
        return [output_base]

    plan = plan_chunks(records, cap=max_lines)
    paths = output_paths(output_base, len(plan))

    for path, chunks in zip(paths, plan):
        with path.open("w", encoding="utf-8") as out:
            for chunk in chunks:
                out.write(chunk.render())
        used = sum(c.total_lines for c in chunks)
        n_split = sum(1 for c in chunks if c.is_split)
        marker = f", {n_split} split chunk(s)" if n_split else ""
        print(f"  ✓ {path.name} — {used:,} lines, {len(chunks)} chunk(s){marker}")

    print(
        f"\n✅ Merged {merged_count}/{input_count} file(s) into "
        f"{len(paths)} merge file(s) (cap {max_lines:,} lines)"
    )
    return paths


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Concatenate text/code/config files into one or more merge files. "
            "Output is split when total content exceeds --max-lines."
        ),
    )
    parser.add_argument(
        "files",
        nargs="+",
        type=Path,
        help=(
            "Files and/or directories to merge, in order. Directories are "
            "expanded recursively (alphabetically), including hidden "
            "(dot-prefixed) entries; symlinks and .DS_Store files are "
            "skipped."
        ),
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=None,
        help=(
            "Output file path. Defaults to ~/tmp/merges/file-merge-<timestamp>.txt. "
            "When the merge splits, parts are named by inserting -partNN before the "
            "final '.' extension."
        ),
    )
    parser.add_argument(
        "--max-lines",
        type=int,
        default=DEFAULT_MAX_LINES,
        help=(
            f"Maximum lines per merge file, banners included. Default "
            f"{DEFAULT_MAX_LINES}. If merged content would exceed this, output "
            f"splits across multiple files. Minimum {MIN_MAX_LINES}."
        ),
    )
    parser.add_argument(
        "--no-banners",
        action="store_true",
        help=(
            "Don't insert section header banners between files. Forces a "
            "single output file (splits require banners for reassembly)."
        ),
    )
    args = parser.parse_args()
    if args.max_lines < MIN_MAX_LINES:
        parser.error(
            f"--max-lines must be >= {MIN_MAX_LINES} "
            f"(got {args.max_lines}); banner overhead leaves no room for content below this."
        )
    return args


def main() -> int:
    """Entry point: parse args, resolve default output path, run merge, open outputs."""
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

    print(f"Merging {len(files)} file(s) → {args.output.parent}/\n")

    written = merge(
        files=files,
        output_base=args.output,
        max_lines=args.max_lines,
        no_banners=args.no_banners,
    )

    if not written:
        return 1

    if sys.platform == "darwin":
        try:
            subprocess.run(["open", *[str(p) for p in written]], check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as err:
            print(f"  ⚠️  Could not open output(s) automatically: {err}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
