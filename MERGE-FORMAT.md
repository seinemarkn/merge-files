# MERGE-FORMAT.md

Consumer specification for the output of `merge-files.py` (this directory).
A downstream agent reading merged files needs only this document to parse,
verify, and reassemble the originals — no source code lookup required.

---

## 1. What the tool produces

`merge-files.py` concatenates one or more input files into one or more
**merge files**. Each input file is preceded by a 2-line **banner** that
identifies it and (when split) its position within the original.

When the total banner+content for a batch would exceed a per-merge-file
**line cap** (default 3,500 lines, configurable via `--max-lines`), the
output is split across multiple merge files. Splitting is automatic — there
is no opt-in flag.

---

## 2. Merge-file header

Every merge file starts with a single header line that identifies its
position within the batch:

```
=== merge-file P/Q
```

- Always present at line 1 of the merge file (when banners are enabled).
- `P/Q` — this is merge file `P` out of `Q`. Both 1-based.
- Lets a consumer order an unordered pile of merge files from content
  alone: sort by `P` ascending.
- Costs 1 line off the per-merge-file cap (default 3,500 → effective
  3,499 for banners and content combined).
- Suppressed in `--no-banners` mode (no reassembly metadata exists there
  anyway).

Regex: `^=== merge-file (\d+)/(\d+)$`. Distinguishable from a file banner
by the absence of `[` after the `=== ` prefix.

---

## 3. Banner format

Every banner is **exactly 2 lines**. Two shapes exist:

### Whole-file banner (non-split)

```
=== [N/M] <full/path/to/file> · K lines
=== sha=<64hex>  sha-norm=<64hex>
```

- `N/M` — this file's 1-based position out of `M` successfully-read files
  in the batch. (Files that were skipped — missing, unreadable, or not
  regular files — are not counted; the consumer sees a gap-free sequence.)
- `<full/path/to/file>` — the file's **display path**. This is a trimmed
  form of the on-disk path designed to keep banner lines short:
    - The tool maintains a list of *anchor* component names (initially
      just `app`, for Rails-style layouts). More anchors may be added in
      future versions.
    - If any path component matches an anchor, the display path starts at
      the directory immediately before the first matching component and
      drops everything to the left. So
      `/Users/x/Projects/Checkers/checkers/app/view/foo.py` displays as
      `checkers/app/view/foo.py`.
    - If no anchor matches, the display path is the path as the tool saw
      it (unchanged).
  The display path is a label, not a filesystem identifier — two distinct
  files could in principle produce the same display path. Use `sha=` (§4)
  for content identity, not the path.
- `K lines` — number of content lines that follow this banner in this
  merge file. The file content begins on the line immediately after line 2
  of the banner and runs for exactly `K` lines.
- `sha=<64hex>` — SHA-256 of the file's raw bytes (hex, lowercase).
- `sha-norm=<64hex>` — SHA-256 of `(text.strip() + "\n").encode("utf-8")`.
  Useful for matching content that differs only in leading/trailing
  whitespace.

### Split-chunk banner

```
=== [N/M] <full/path/to/file> · K lines
=== sha=<64hex>  part=P/Q  range=A-B/T
```

- Line 1 is **identical in shape** to the whole-file form. `K` is the
  number of content lines in **this chunk only**.
- `sha=<64hex>` — SHA-256 of the **whole original file** (not this chunk).
  Every chunk of a split file carries the same `sha=` value. This is the
  reassembly anchor.
- `part=P/Q` — this chunk is part `P` of `Q` parts. Both 1-based.
- `range=A-B/T` — these are lines `A` through `B` (inclusive, 1-based) of
  the original file, which has `T` total lines.
- `sha-norm=` is **omitted** in split chunks (a partial chunk's normalized
  hash has no clear semantics; the whole-file `sha=` is enough).

---

## 4. Regexes for parsing

The two banner lines use stable, regex-friendly shapes.

```python
import re

# Merge-file header (always at line 1 of each merge file):
MERGE_FILE_HEADER_RE = re.compile(r"^=== merge-file (\d+)/(\d+)$")

# Line 1 is identical for split and non-split files:
BANNER_LINE1_RE = re.compile(
    r"^=== \[(\d+)/(\d+)\] (.+) · (\d+) lines$"
)

# Line 2 — whole file:
BANNER_LINE2_WHOLE_RE = re.compile(
    r"^=== sha=([a-f0-9]{64})  sha-norm=([a-f0-9]{64})$"
)

# Line 2 — split chunk:
BANNER_LINE2_SPLIT_RE = re.compile(
    r"^=== sha=([a-f0-9]{64})  part=(\d+)/(\d+)  range=(\d+)-(\d+)/(\d+)$"
)
```

The double-space between fields on line 2 is part of the format. Disambiguator
for a parser: line 2 contains `part=` if and only if it's a split chunk.

---

## 5. Invariants and verification

Every banner asserts the following invariants, which a consumer can
cross-check to detect corruption (e.g. OCR drift in screenshot-based
pipelines):

0. **Merge files are P-ordered.** Sort the input pile of merge files by
   the `=== merge-file P/Q` header's `P` field ascending; that is their
   batch order. `Q` is identical across every merge file in the batch.
1. **K equals the content-line count.** The `K lines` field on line 1 is
   the exact number of lines of file content that follow the banner. A
   parser that counts `K` lines after line 2 of the banner will land
   precisely at the next banner (or end-of-merge-file).
2. **K equals the range width.** For split chunks: `K == B - A + 1`. If
   line 1's `K lines` and line 2's `range=A-B/T` disagree, the banner has
   been corrupted — re-fetch the source merge file.
3. **All chunks of a split file share `sha=`, `part_total`, and `T`.** If
   chunks claiming the same source file disagree on any of these, the data
   is inconsistent.
4. **`P` values are dense in `1..Q`.** Across a complete batch, every
   chunk of a split file with `part=P/Q` should be present for `P` in
   `1, 2, ..., Q`. A missing `P` means a merge file is missing.
5. **`range` segments tile `[1, T]`.** Concatenating the `[A, B]` ranges
   of all chunks of a file (in `P` order) covers exactly `1..T` with no
   gaps and no overlap.
6. **The reassembled file matches `sha=`.** Joining the chunk contents
   (see §5) and SHA-256-ing the bytes must equal `sha=`. This is the
   final, authoritative correctness check.

---

## 6. Reassembling a split file

The reassembly rule is **plain concatenation** — no injected separators,
no newline normalization:

1. Collect every chunk whose `sha=` equals the target file's hash. (Or
   equivalently, every chunk with the same `path` on line 1 — but `sha=`
   is more robust because the path is just an identifier; only `sha=`
   anchors content identity.)
2. Sort by `part=P/Q` ascending on `P`.
3. Extract each chunk's content slice (the `K` lines immediately
   following line 2 of its banner).
4. Concatenate the slices in order, with no separator added between them.

```python
def reassemble(chunks):
    """chunks: list of dicts {'part_n': int, 'content_lines': list[str]}.

    Each content_lines item is one source line WITH its trailing newline
    (except possibly the file's final line, which inherits the original
    file's trailing-newline state)."""
    chunks_sorted = sorted(chunks, key=lambda c: c['part_n'])
    return "".join("".join(c['content_lines']) for c in chunks_sorted)
```

**Why plain concatenation works**: splits occur strictly between lines.
No source line is ever split across two parts. Each part's content ends
at a `\n` (with the possible exception of the final part of a file that
itself lacked a trailing `\n` in the original — in which case that part
also lacks one, and reassembly preserves the original state).

---

## 7. Discovering all parts of a batch

The tool names output files as follows:

- **One merge file** (under the cap): `<output_base>.txt` — exactly the
  name passed to `-o`, or the default `~/tmp/merges/file-merge-<ts>.txt`.
- **Multiple merge files** (split): `<base>-part<NN>.txt`,
  `<base>-part<NN+1>.txt`, …. `NN` is zero-padded to at least 2 digits;
  for batches of 100+ parts, padding widens to 3 (or more) digits, but
  width stays consistent within a single batch.

Once you have the pile of merge files, the `=== merge-file P/Q` header
(§2) on each one gives you their order directly — you do **not** need
the filenames to reconstruct sequence. The filename pattern below is
purely a discovery convenience for shell scripts and Finder.

To find every merge file from a known base:

```python
import glob, os
base, ext = os.path.splitext(output_base)
candidates = glob.glob(f"{base}-part*{ext}")
single = [output_base] if os.path.isfile(output_base) else []
all_paths = sorted(candidates) if candidates else single
```

**Quirk to be aware of**: multi-dot extensions (`-o foo.tar.gz`) are
split at the last `.` only — so the parts are `foo.tar-part01.gz`,
`foo.tar-part02.gz`, etc. This is intentional and documented; users who
want compound-extension-aware splitting should pass `-o foo` without an
extension.

---

## 8. End-to-end consumer workflow

```python
def consume_merge_batch(paths: list[Path]) -> dict[str, str]:
    """Parse one or more merge files and return {path: full_content}."""
    import re, hashlib
    from collections import defaultdict

    HEADER = re.compile(r"^=== merge-file (\d+)/(\d+)$")
    LINE1 = re.compile(r"^=== \[(\d+)/(\d+)\] (.+) · (\d+) lines$")
    LINE2_WHOLE = re.compile(
        r"^=== sha=([a-f0-9]{64})  sha-norm=([a-f0-9]{64})$"
    )
    LINE2_SPLIT = re.compile(
        r"^=== sha=([a-f0-9]{64})  part=(\d+)/(\d+)  range=(\d+)-(\d+)/(\d+)$"
    )

    # Order the merge files by the `merge-file P/Q` header so the consumer
    # doesn't have to trust filenames.
    def header_p(path):
        line = path.read_text(encoding="utf-8").split("\n", 1)[0]
        m = HEADER.match(line)
        return int(m.group(1)) if m else 0
    paths = sorted(paths, key=header_p)

    # First pass: collect every chunk.
    chunks_by_sha: dict[str, list[dict]] = defaultdict(list)
    whole_files: list[tuple[str, str, str]] = []  # (path, content, sha)

    for p in paths:
        text = p.read_text(encoding="utf-8")
        lines = text.split("\n")
        i = 1 if HEADER.match(lines[0]) else 0  # skip the merge-file header
        while i < len(lines):
            m1 = LINE1.match(lines[i]) if i < len(lines) else None
            if not m1:
                i += 1
                continue
            k = int(m1.group(4))
            line2 = lines[i + 1]
            content = lines[i + 2 : i + 2 + k]
            content_text = "\n".join(content) + "\n"  # restore line breaks
            ms = LINE2_SPLIT.match(line2)
            if ms:
                chunks_by_sha[ms.group(1)].append({
                    "path": m1.group(3),
                    "part_n": int(ms.group(2)),
                    "part_total": int(ms.group(3)),
                    "range_start": int(ms.group(4)),
                    "range_end": int(ms.group(5)),
                    "file_total": int(ms.group(6)),
                    "content_text": content_text,
                    "K": k,
                })
                # Invariant check: K == B - A + 1
                assert k == int(ms.group(5)) - int(ms.group(4)) + 1, (
                    f"banner invariant violated: K={k} vs range={ms.group(4)}-{ms.group(5)}"
                )
            else:
                mw = LINE2_WHOLE.match(line2)
                assert mw, f"unparseable banner line 2: {line2!r}"
                whole_files.append((m1.group(3), content_text, mw.group(1)))
            i += 2 + k

    # Reassemble split files.
    files: dict[str, str] = {}
    for sha, chunks in chunks_by_sha.items():
        chunks.sort(key=lambda c: c["part_n"])
        path = chunks[0]["path"]
        body = "".join(c["content_text"] for c in chunks)
        # Verify reassembly against the whole-file SHA.
        actual = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if actual != sha:
            raise ValueError(
                f"reassembly SHA mismatch for {path}: expected {sha}, got {actual}"
            )
        files[path] = body
    for path, body, _ in whole_files:
        files[path] = body

    return files
```

This implementation enforces the invariants from §4 and uses the
whole-file `sha=` as the final correctness check, exactly as the spec
demands.

---

## 9. Edge cases the consumer should expect

- **Files skipped during merge** (missing, unreadable, not a regular
  file, or detected as binary) never appear in any banner. The `M` in
  `[N/M]` reflects only the files that made it through. Binary detection
  uses the classic NUL-byte-in-first-8KB heuristic — PNG/JPEG/PDF/zip/
  executables are all excluded automatically, no extension list needed.
- **Lossy UTF-8 decoding**: input bytes that aren't valid UTF-8 are
  replaced with the Unicode replacement character (`U+FFFD`) during merge.
  `sha=` is computed over the **raw bytes**, so consumers verifying a
  recovered file should hash the decoded text — there may be a hash
  mismatch if the original was binary. (Don't merge binary files.)
- **Empty files** appear as banners with `0 lines` and no content
  between them and the next banner.
- **Files without trailing newlines**: `sha=` is computed over the
  original raw bytes (before any synthesis), so it is the authoritative
  identity of the source file. The merger synthesizes a trailing `\n`
  internally so banner boundaries are clean — that synthesized newline
  appears in the chunk content and is included in `K`. A consumer who
  joins the reassembled chunks and hashes the result will see a one-byte
  mismatch against `sha=` for any source file that originally lacked a
  trailing newline. To recover the original bytes exactly: if
  `sha256(reassembled) != sha=`, try `sha256(reassembled.rstrip("\n"))`
  before declaring corruption.
- **No banners mode** (`--no-banners`): the tool produces a single output
  file with concatenated content and no metadata. Reassembly and the
  invariants in §4 do not apply. This mode is intended for direct text
  ingestion, not for downstream parsing.

---

## 10. Versioning

The base format is version 1.0 (the first formal specification). A merge
file with no `=== format-version=N` line is version 1.0.

Version **1.1** adds the optional deletions manifest (§11). It is emitted
**only** by `--track` runs that actually have deletions to report; such a
merge begins with a `=== format-version=1.1` line. A `--track` run with no
deletions produces byte-identical 1.0 output (no version line). A 1.0
consumer can safely read 1.1 output: every manifest line starts with
`=== ` but none contains `[`, so the §8 parser (which scans for
`=== [N/M]` banners) skips the entire block.

---

## 11. Deletions manifest (version 1.1, `--track` mode)

`merge-files.py --track <channel>` merges only the files that are **new or
changed** since that channel's previous run (change is judged by `sha-norm`,
so whitespace-only edits don't count). Files that were present on the prior
run but are now gone from the tree are reported in a **deletions manifest**
at the top of merge file `1/Q`, immediately after its `=== merge-file P/Q`
header:

```
=== merge-file 1/1
=== format-version=1.1
=== deleted-files 2
=== deleted app/old_thing.py
=== deleted config/gone.yml
=== [1/2] app/changed.py · 12 lines
=== sha=<64hex>  sha-norm=<64hex>
...file content...
```

- `=== format-version=1.1` — present whenever a deletions manifest is.
- `=== deleted-files N` — the count of deleted paths that follow.
- `=== deleted <display_path>` — one per deleted file, using the same
  **display path** form as banners (§3). These are advisory: the tool does
  not touch the consumer's tree — a downstream process may remove them, or
  ignore the section entirely with no loss.

Regexes:

```python
DELETIONS_VERSION_RE = re.compile(r"^=== format-version=(\S+)$")
DELETED_COUNT_RE     = re.compile(r"^=== deleted-files (\d+)$")
DELETED_PATH_RE      = re.compile(r"^=== deleted (.+)$")
```

The manifest appears on **part 1 only**. Its lines count against that merge
file's cap (reserved batch-wide), so every output file still honors
`--max-lines`. A run whose *only* change is deletions still emits one merge
file — carrying just the header and the manifest — so the removal is never
silently dropped.

### Local baseline (sender-side state)

Tracking assumes **no back-channel** from the consumer: the sender's own
machine remembers what it last emitted. Per channel, a baseline manifest is
stored (atomically) under `~/.local/state/merge-files/baselines/<channel>.json`
(honoring `$XDG_STATE_HOME`), mapping each file's display path to its
`sha-norm`. After every successful run the baseline advances to the full
current snapshot (optimistic advance). This state is entirely sender-side and
never appears in merge output — a consumer needs nothing from §11 beyond the
manifest lines above.

### Configuration

User settings live in `~/.config/merge-files/config.json` (honoring
`$XDG_CONFIG_HOME`); a missing file uses built-in defaults. Recognized keys:
`report_deletions` (bool, default true — set false to omit the §11 manifest),
`advance` (baseline-advance policy; only `"optimistic"` is implemented), and
`skip_extensions` (reserved for a future skip-by-extension feature; not yet
enforced).

Generated by `merge-files.py` — see [merge-files.py](merge-files.py)
for the implementation and [test_merge_files.py](test_merge_files.py)
for the conformance tests.
