#!/usr/bin/env python3
"""
merge-files.py — Concatenate code/config/text files (in the order given) into one
or more merge files, splitting at a configurable line cap.

Usage:
    python merge-files.py file1.py file2.rb config.yml
    python merge-files.py src/ -o all_python.txt
    python merge-files.py --max-lines 2000 *.py
    python merge-files.py a.md b.md c.md --no-banners
    python merge-files.py src/ --track myproj   # merge only files changed since last run

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
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

# Each banner is always exactly 2 lines. This is part of the output contract
# (see MERGE-FORMAT.md): the bin-packer must subtract this from the cap to
# know how much room is available for content in each merge file.
BANNER_LINES = 2

# Every merge file (when banners are enabled) starts with a single-line
# "=== merge-file P/Q" header that lets a consumer order the merge files
# from content alone, without relying on filenames. This costs 1 line off
# the cap per merge file. Suppressed in --no-banners mode (where there's
# no reassembly metadata to anchor anyway).
MERGE_FILE_HEADER_LINES = 1

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

# Path components that anchor the banner display path. If any file's path
# contains a component matching one of these, the displayed path starts at
# the directory immediately before the first matching component (see
# display_path_for). The initial entry — 'app' — is the Rails/Laravel
# convention for source-tree roots; add more anchors here as new project
# layouts come up. Beware of common names like 'src' or 'lib': they appear
# in many unrelated paths and would over-trigger trimming.
DISPLAY_PATH_ANCHORS = ("app",)

# ---------------------------------------------------------------------------
# Configuration & change-tracking (the --track feature).
#
# Two distinct on-disk locations, following the XDG split:
#   - CONFIG  (~/.config/merge-files/config.json): user-editable settings.
#   - STATE   (~/.local/state/merge-files/baselines/<channel>.json): the
#     machine-generated per-channel baselines. Never hand-edited.
# Both honor their XDG_* env overrides. Neither ever touches source files
# or merge outputs, so tracking is entirely non-destructive.
# ---------------------------------------------------------------------------

# Config schema version. Bumped only on a breaking change to the shape below.
CONFIG_VERSION = 1

# Defaults used when the config file is absent or a key is omitted. A future
# skip-by-extension feature will read `skip_extensions` from here — the key is
# present (and documented) now so the file shape stays stable, but it is not
# yet enforced during expansion.
DEFAULT_CONFIG: dict = {
    "config_version": CONFIG_VERSION,
    # How the local baseline advances after a successful --track run. Only
    # "optimistic" (advance every run) is implemented today; the key exists so
    # the policy can change later without a new flag.
    "advance": "optimistic",
    # Record deleted files (present in the baseline, gone from the tree) in the
    # merge output's deletions section. Informational for the consumer only;
    # set false to omit the section entirely.
    "report_deletions": True,
    # File extensions to skip during folder expansion, e.g. [".log", ".min.js"].
    # Matched case-insensitively; a leading dot is optional in config (".log" and
    # "log" are equivalent). Compound extensions work (".min.js" matches
    # foo.min.js). Only applies when a directory is expanded — files named
    # explicitly on the command line are always kept.
    "skip_extensions": [],
}

# Baseline manifest schema version, independent of CONFIG_VERSION.
BASELINE_VERSION = 1

# Deletions manifest bumps the merge-format version (MERGE-FORMAT.md §10). The
# marker line only appears when a track run actually has deletions, so a track
# merge with no deletions is byte-identical to a plain (1.0) merge.
DELETIONS_FORMAT_VERSION = "1.1"

# Channel names become filenames under the baselines dir, so keep them to a
# safe, predictable character set (no path separators, no leading dot/dash).
CHANNEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _xdg_dir(env_var: str, default_parts: tuple[str, ...]) -> Path:
    """Resolve an XDG base dir (honoring its env override) + the tool subdir."""
    override = os.environ.get(env_var)
    base = Path(override) if override else Path.home().joinpath(*default_parts)
    return base / "merge-files"


def config_path() -> Path:
    """Path to the user config file (~/.config/merge-files/config.json)."""
    return _xdg_dir("XDG_CONFIG_HOME", (".config",)) / "config.json"


def baseline_path(channel: str) -> Path:
    """Path to a channel's baseline manifest under the state dir."""
    return _xdg_dir("XDG_STATE_HOME", (".local", "state")) / "baselines" / f"{channel}.json"


def load_config() -> dict:
    """Return config settings, overlaying config.json onto DEFAULT_CONFIG.

    A missing file, an unreadable file, or invalid JSON all fall back to the
    defaults (with a stderr warning for the latter two — a truly-absent file is
    the normal case and stays silent). Unknown keys are kept so forward-compat
    settings survive a round-trip, but only the documented keys are consulted.
    """
    config = dict(DEFAULT_CONFIG)
    path = config_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return config
    except OSError as err:
        print(f"  ⚠️  Could not read config ({path}): {err}; using defaults",
              file=sys.stderr)
        return config
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as err:
        print(f"  ⚠️  Invalid config JSON ({path}): {err}; using defaults",
              file=sys.stderr)
        return config
    if isinstance(data, dict):
        config.update(data)
    else:
        print(f"  ⚠️  Config ({path}) is not a JSON object; using defaults",
              file=sys.stderr)
    return config


def normalize_skip_extensions(raw) -> tuple[str, ...]:
    """Normalize a config `skip_extensions` value to a tuple of match suffixes.

    Each entry is lowercased, whitespace-trimmed, and given a leading dot if it
    lacks one (so both "log" and ".log" work). Blank and non-string entries are
    dropped. Duplicates are collapsed while preserving first-seen order.
    """
    if not isinstance(raw, list):
        return ()
    seen: dict[str, None] = {}
    for item in raw:
        if not isinstance(item, str):
            continue
        ext = item.strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = "." + ext
        seen.setdefault(ext, None)
    return tuple(seen)


def _matches_skip_extension(filename: str, skip_exts: tuple[str, ...]) -> bool:
    """True if `filename` ends with one of `skip_exts` (case-insensitive).

    Requires a non-empty stem before the extension, so a file literally named
    ".log" is NOT matched by the ".log" rule (it has no base name), while
    "app.log" is. The leading dot on each suffix keeps "catalog" from matching
    ".log", and matching the full suffix supports compound forms (".min.js").
    """
    lower = filename.lower()
    return any(len(lower) > len(ext) and lower.endswith(ext) for ext in skip_exts)


def load_baseline(channel: str) -> dict:
    """Return a channel's baseline file-map ({display_path: {...}}), or {}.

    A missing baseline (first run on a channel) returns an empty map. A corrupt
    or unreadable baseline warns and returns empty — treating every file as new
    is a safe, recoverable failure mode (the run just re-merges everything and
    rewrites a clean baseline).
    """
    path = baseline_path(channel)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as err:
        print(f"  ⚠️  Could not read baseline for '{channel}' ({path}): {err}; "
              f"treating all files as new", file=sys.stderr)
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as err:
        print(f"  ⚠️  Corrupt baseline for '{channel}' ({path}): {err}; "
              f"treating all files as new", file=sys.stderr)
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    return files if isinstance(files, dict) else {}


def save_baseline(channel: str, snapshot: dict) -> None:
    """Atomically write a channel's baseline (full current snapshot).

    Writes to a temp file then os.replace()s it into place so an interrupted
    run can never leave a half-written (corrupt) baseline. The original
    `created` timestamp is preserved across updates when the file already exists.
    """
    path = baseline_path(channel)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().isoformat(timespec="seconds")
    created = now
    try:
        prev = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(prev, dict) and isinstance(prev.get("created"), str):
            created = prev["created"]
    except (OSError, json.JSONDecodeError):
        pass
    payload = {
        "baseline_version": BASELINE_VERSION,
        "channel": channel,
        "created": created,
        "updated": now,
        "files": snapshot,
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def snapshot_from_records(records: list[FileRecord]) -> dict:
    """Build a {display_path: {sha_norm, sha, lines}} map for the whole batch.

    The key is the *display path* (the trimmed banner path) — chosen for
    portability across machines/checkout locations. Display paths are not
    guaranteed unique (see display_path_for); a collision warns and last-wins,
    which at worst under-reports one file as changed on the next run.
    """
    snapshot: dict = {}
    for rec in records:
        key = str(display_path_for(rec.path))
        if key in snapshot:
            print(f"  ⚠️  Display-path collision on '{key}'; change tracking "
                  f"may be imprecise for it", file=sys.stderr)
        snapshot[key] = {
            "sha_norm": rec.sha_normalized,
            "sha": rec.sha,
            "lines": len(rec.lines),
        }
    return snapshot


@dataclass
class TrackingResult:
    """Outcome of diffing the current batch against a channel's baseline.

    `changed` is the renumbered subset of records to actually merge (new or
    content-changed by sha-norm). `deleted` is the sorted list of display paths
    that were in the baseline but are gone now. `snapshot` is the FULL current
    snapshot to persist as the next baseline (not just the changed subset).
    """
    changed: list[FileRecord]
    deleted: list[str]
    snapshot: dict


def apply_tracking(records: list[FileRecord], baseline_files: dict) -> TrackingResult:
    """Diff `records` against a baseline file-map, keyed by display path.

    A record is kept when its display path is new to the baseline OR its
    sha-norm differs from the stored one (whitespace-only churn is ignored,
    since sha-norm normalizes it). Kept records are renumbered 1..K so their
    banners show a gap-free [N/M] over the changed set only.
    """
    snapshot = snapshot_from_records(records)
    changed: list[FileRecord] = []
    for rec in records:
        key = str(display_path_for(rec.path))
        prev = baseline_files.get(key)
        if not isinstance(prev, dict) or prev.get("sha_norm") != rec.sha_normalized:
            changed.append(rec)
    total = len(changed)
    renumbered = [replace(rec, index=i, total=total)
                  for i, rec in enumerate(changed, start=1)]
    deleted = sorted(set(baseline_files) - set(snapshot))
    return TrackingResult(changed=renumbered, deleted=deleted, snapshot=snapshot)


def make_deletions_section(deleted: list[str]) -> str:
    """Return the deletions manifest that leads part 1 of a track merge.

    Shape (only emitted when `deleted` is non-empty):
        === format-version=1.1
        === deleted-files N
        === deleted <display_path>
        ...
    Every line starts `=== ` but none contains `[`, so existing consumers
    (which scan for the `=== [N/M]` banner) skip the whole block harmlessly.
    """
    lines = [
        f"=== format-version={DELETIONS_FORMAT_VERSION}\n",
        f"=== deleted-files {len(deleted)}\n",
    ]
    lines.extend(f"=== deleted {p}\n" for p in deleted)
    return "".join(lines)


def is_likely_binary(data: bytes, sample_size: int = 8192) -> bool:
    """Return True if `data` looks like a binary file rather than text.

    Uses the classic Unix NUL-byte heuristic: if a NUL (`\\x00`) appears in
    the first `sample_size` bytes, the file is binary. This is what git
    diff, grep -I, and most other tools use because it's cheap, doesn't
    depend on filename extensions, and is correct for every common binary
    format (PNG/JPEG/PDF/zip/executables — all of which have NULs in their
    headers or near-headers).

    Trade-off: legitimate UTF-16/UTF-32 text files have NULs and will be
    flagged as binary. Acceptable for the use case (merging source code,
    config, docs, etc.) where UTF-8 dominates.
    """
    return b"\x00" in data[:sample_size]


def make_merge_file_header(part_n: int, total: int) -> str:
    """Return the 1-line header that appears at the top of every merge file.

    Shape: `=== merge-file P/Q\\n`. Distinguishable from a file banner's
    line 1 by the absence of `[` after the `=== ` prefix — consumers
    parsing with `^=== merge-file (\\d+)/(\\d+)$` will not collide with the
    existing `^=== \\[...\\]` file-banner regex.
    """
    return f"=== merge-file {part_n}/{total}\n"


def display_path_for(path: Path) -> Path:
    """Trim a path to start one directory before the first anchor component.

    "Anchors" are listed in DISPLAY_PATH_ANCHORS — currently just `app` for
    Rails-style layouts. If any anchor name appears as a path component,
    the display path starts at the directory immediately before the first
    matching component and drops everything to the left. If no anchor is
    present, the path is returned unchanged.

    Examples (with `app` as an anchor):
      /Users/x/Projects/Checkers/checkers/app/view/foo.py
        → checkers/app/view/foo.py
      /Users/x/Projects/Checkers/checkers/README.md   (no anchor)
        → /Users/x/Projects/Checkers/checkers/README.md
      /app/main.py            (anchor at root — no preceding directory)
        → app/main.py

    First-occurrence wins so nested anchor folders inside a project still
    trim back to the outermost one (e.g. `proj/app/services/app/main.py`
    keeps `proj/...`, not `services/...`).
    """
    parts = path.parts
    # Skip the filesystem anchor ('/' or drive letter) when searching — we
    # never want an absolute prefix like '/app/...' in the display path.
    if parts and (parts[0] in ("/", "\\") or parts[0].endswith(":\\")):
        searchable = parts[1:]
    else:
        searchable = parts
    anchor_idx = next(
        (i for i, part in enumerate(searchable) if part in DISPLAY_PATH_ANCHORS),
        None,
    )
    if anchor_idx is None:
        return path
    start = max(anchor_idx - 1, 0)
    new_parts = searchable[start:]
    return Path(*new_parts) if new_parts else path


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
        display_path = display_path_for(self.record.path)
        if self.is_split:
            assert self.part_total is not None
            assert self.range_start is not None
            assert self.range_end is not None
            banner = make_split_banner(
                path=display_path,
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
                path=display_path,
                index=self.record.index,
                total=self.record.total,
                lines=len(self.content_lines),
                sha=self.record.sha,
                sha_normalized=self.record.sha_normalized,
            )
        return banner + "".join(self.content_lines)


def _walk_directory(directory: Path, skip_extensions: tuple[str, ...]) -> tuple[list[Path], int]:
    """Walk one directory recursively → (sorted files kept, count skipped by ext).

    Applies the same rules as expand_paths: EXCLUDED_FILENAMES and symlinks are
    always dropped; files whose name matches `skip_extensions` are dropped and
    counted.
    """
    files_in_dir: list[Path] = []
    skipped = 0
    for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
        dirnames.sort()
        for fname in sorted(filenames):
            if fname in EXCLUDED_FILENAMES:
                continue
            if _matches_skip_extension(fname, skip_extensions):
                skipped += 1
                continue
            fpath = Path(dirpath) / fname
            if fpath.is_symlink():
                continue
            files_in_dir.append(fpath)
    files_in_dir.sort()
    return files_in_dir, skipped


def expand_paths(
    paths: list[Path],
    skip_extensions: tuple[str, ...] = (),
) -> list[Path]:
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

    `skip_extensions` (from config; already normalized) drops matching files
    during expansion ONLY — a file passed explicitly on the command line is
    always kept, even if its extension is on the skip list. The count of files
    skipped this way is reported to stderr.
    """
    result: list[Path] = []
    skipped_count = 0
    for p in paths:
        if p.is_dir() and not p.is_symlink():
            files_in_dir, skipped = _walk_directory(p, skip_extensions)
            result.extend(files_in_dir)
            skipped_count += skipped
        else:
            result.append(p)
    if skipped_count:
        print(f"  ⚠️  Skipped {skipped_count} file(s) by extension "
              f"({', '.join(skip_extensions)})", file=sys.stderr)
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
        if is_likely_binary(data):
            print(f"  ⚠️  Skipping (binary content): {path}", file=sys.stderr)
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


def _write_plan(
    plan: list[list[Chunk]],
    output_base: Path,
    deletions_block: str = "",
) -> list[Path]:
    """Write each planned merge file to disk and return the paths written.

    `deletions_block`, when non-empty, is emitted once, immediately after the
    merge-file header of part 1 (the deletions manifest of a --track run).
    """
    paths = output_paths(output_base, len(plan))
    total_merge_files = len(plan)
    for part_n, (path, chunks) in enumerate(zip(paths, plan), start=1):
        lead = deletions_block if part_n == 1 else ""
        with path.open("w", encoding="utf-8") as out:
            out.write(make_merge_file_header(part_n, total_merge_files))
            if lead:
                out.write(lead)
            for chunk in chunks:
                out.write(chunk.render())
        used = (MERGE_FILE_HEADER_LINES + lead.count("\n")
                + sum(c.total_lines for c in chunks))
        n_split = sum(1 for c in chunks if c.is_split)
        marker = f", {n_split} split chunk(s)" if n_split else ""
        print(f"  ✓ {path.name} — {used:,} lines, {len(chunks)} chunk(s){marker}")
    return paths


def _write_no_banners(records: list[FileRecord], output_base: Path,
                      input_count: int) -> list[Path]:
    """Write the single, metadata-free output for --no-banners mode."""
    with output_base.open("w", encoding="utf-8") as out:
        for rec in records:
            out.writelines(rec.lines)
    total_lines = sum(len(r.lines) for r in records)
    print(f"\n✅ Merged {len(records)}/{input_count} file(s), "
          f"{total_lines:,} content lines (no banners)")
    print(f"   Output: {output_base}")
    return [output_base]


def _tracked_batch(records: list[FileRecord], track: str,
                   config: dict) -> tuple[list[FileRecord], list[str], dict]:
    """Diff `records` against channel `track`'s baseline → (changed, deleted, snapshot)."""
    result = apply_tracking(records, load_baseline(track))
    deleted = result.deleted if config.get("report_deletions", True) else []
    return result.changed, deleted, result.snapshot


def _plan_output(merged_records: list[FileRecord], deletions_block: str,
                 max_lines: int) -> list[list[Chunk]] | None:
    """Bin-pack the records to merge, reserving header + deletions overhead.

    Returns the plan, [[]] for a deletions-only run (which still needs one
    merge file to host the manifest), or None if the deletions manifest is too
    large to leave room for content at the given cap.
    """
    if not merged_records:
        return [[]]
    # The deletions block is reserved batch-wide for simplicity; on a multi-part
    # split that wastes a few lines on parts 2..Q, negligible for realistic lists.
    cap = max_lines - MERGE_FILE_HEADER_LINES - deletions_block.count("\n")
    if cap <= BANNER_LINES:
        n_deleted = max(deletions_block.count("\n") - 2, 0)
        print(f"\n⚠️  --max-lines={max_lines} is too small to hold the "
              f"{n_deleted}-entry deletions manifest plus file content; "
              f"raise --max-lines.", file=sys.stderr)
        return None
    return plan_chunks(merged_records, cap=cap)


def _print_merge_summary(track: str | None, records: list[FileRecord],
                         merged_records: list[FileRecord], deleted: list[str],
                         paths: list[Path], input_count: int, max_lines: int) -> None:
    """Print the final one-line summary (track-aware)."""
    if track is None:
        print(f"\n✅ Merged {len(merged_records)}/{input_count} file(s) into "
              f"{len(paths)} merge file(s) (cap {max_lines:,} lines)")
        return
    bits = [f"{len(merged_records)} changed/new"]
    if deleted:
        bits.append(f"{len(deleted)} deleted")
    bits.append(f"{len(records) - len(merged_records)} unchanged")
    print(f"\n✅ Channel '{track}': " + ", ".join(bits)
          + f" → {len(paths)} merge file(s) (cap {max_lines:,} lines)")


def merge(
    files: list[Path],
    output_base: Path,
    max_lines: int = DEFAULT_MAX_LINES,
    no_banners: bool = False,
    track: str | None = None,
    config: dict | None = None,
) -> list[Path] | None:
    """Read input files, plan chunks, write one or more merge files.

    Returns the list of output paths written. Returns None on hard failure
    (nothing readable to act on), and an empty list on a --track run that found
    no changes and no deletions (a success — nothing to write).

    `--no-banners` mode forces a single output file with no banners and no
    splitting — splits without banners can't be reassembled, so the cap is
    ignored in that mode. (`--track` is rejected alongside `--no-banners`.)

    `--track NAME` diffs the batch against channel NAME's local baseline and
    merges only new/changed files, records deletions in a manifest atop part 1,
    then advances the baseline to the full current snapshot.
    """
    config = config if config is not None else DEFAULT_CONFIG
    records = read_records(files)
    if not records and track is None:
        print("\n⚠️  No files successfully read.", file=sys.stderr)
        return None

    if no_banners:
        return _write_no_banners(records, output_base, len(files))

    if track is not None:
        merged_records, deleted, snapshot = _tracked_batch(records, track, config)
        if not merged_records and not deleted:
            save_baseline(track, snapshot)  # optimistic advance (no-op refresh)
            print(f"\n✅ No changes on channel '{track}' "
                  f"({len(records)} file(s) scanned, all unchanged).")
            return []
    else:
        merged_records, deleted, snapshot = records, [], None

    deletions_block = make_deletions_section(deleted) if deleted else ""
    plan = _plan_output(merged_records, deletions_block, max_lines)
    if plan is None:
        return None

    paths = _write_plan(plan, output_base, deletions_block)
    if track is not None:
        save_baseline(track, snapshot)
    _print_merge_summary(track, records, merged_records, deleted,
                         paths, len(files), max_lines)
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
    parser.add_argument(
        "--track",
        type=str,
        default=None,
        metavar="NAME",
        help=(
            "Change-tracking channel. Only files new or changed (by sha-norm) "
            "since this channel's last run are merged; deleted files are "
            "recorded in the output. A per-channel baseline is kept locally "
            "under ~/.local/state/merge-files/. Incompatible with --no-banners."
        ),
    )
    args = parser.parse_args()
    if args.max_lines < MIN_MAX_LINES:
        parser.error(
            f"--max-lines must be >= {MIN_MAX_LINES} "
            f"(got {args.max_lines}); banner overhead leaves no room for content below this."
        )
    if args.track is not None:
        if args.no_banners:
            parser.error(
                "--track cannot be combined with --no-banners "
                "(tracking needs banners to record shas and deletions)."
            )
        if not CHANNEL_NAME_RE.match(args.track):
            parser.error(
                f"--track name {args.track!r} is invalid; use letters, digits, "
                f"'.', '_' or '-' (must start with a letter or digit)."
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

    config = load_config()
    skip_extensions = normalize_skip_extensions(config.get("skip_extensions", []))

    files = expand_paths(args.files, skip_extensions=skip_extensions)
    if not files:
        print("No files to merge (after expanding any folder arguments).", file=sys.stderr)
        return 1

    print(f"Merging {len(files)} file(s) → {args.output.parent}/\n")

    written = merge(
        files=files,
        output_base=args.output,
        max_lines=args.max_lines,
        no_banners=args.no_banners,
        track=args.track,
        config=config,
    )

    if written is None:
        return 1
    if not written:
        return 0  # --track run with nothing to write is a success, not a failure

    if sys.platform == "darwin":
        try:
            subprocess.run(["open", *[str(p) for p in written]], check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as err:
            print(f"  ⚠️  Could not open output(s) automatically: {err}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
