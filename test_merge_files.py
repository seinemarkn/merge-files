"""Tests for merge-files.py.

Run with:
    python3 -m unittest commands/merge-files/test_merge_files.py
    # or from this directory:
    python3 -m unittest test_merge_files.py
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


def _load_script():
    """Load merge-files.py as a module (hyphenated filename → importlib).

    The sys.modules registration before exec_module is required: under Python
    3.14, @dataclass walks sys.modules[cls.__module__] internally and crashes
    if the host module isn't registered. The older importlib idiom worked only
    because the file had no dataclasses.
    """
    script_path = Path(__file__).parent / "merge-files.py"
    spec = importlib.util.spec_from_file_location("merge_files", script_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["merge_files"] = module
    spec.loader.exec_module(module)
    return module


merge_files = _load_script()


# Line-1 regex from the MERGE-FORMAT spec. Locked in: consumers parse it
# unchanged whether the file is split or not. Reproduced here so the tests
# fail loudly if the format ever drifts from the spec.
BANNER_LINE1_RE = re.compile(
    r"^=== \[(\d+)/(\d+)\] (.+) · (\d+) lines$"
)
BANNER_LINE2_WHOLE_RE = re.compile(
    r"^=== sha=([a-f0-9]{64})  sha-norm=([a-f0-9]{64})$"
)
BANNER_LINE2_SPLIT_RE = re.compile(
    r"^=== sha=([a-f0-9]{64})  part=(\d+)/(\d+)  range=(\d+)-(\d+)/(\d+)$"
)


MERGE_FILE_HEADER_RE = re.compile(r"^=== merge-file (\d+)/(\d+)$")


class TestMakeMergeFileHeader(unittest.TestCase):
    """1-line merge-file-level header at the top of each output file."""

    def test_format_matches_spec(self):
        """`=== merge-file P/Q\\n` — distinguishable from file banners by
        the absence of `[` after the `=== ` prefix."""
        header = merge_files.make_merge_file_header(2, 5)
        self.assertEqual(header, "=== merge-file 2/5\n")

    def test_matches_consumer_regex(self):
        """Header is parseable with the documented regex."""
        m = MERGE_FILE_HEADER_RE.match(merge_files.make_merge_file_header(3, 7).rstrip())
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "3")
        self.assertEqual(m.group(2), "7")

    def test_does_not_collide_with_file_banner_regex(self):
        """A merge-file header must not match the file-banner line-1 regex."""
        header = merge_files.make_merge_file_header(1, 1).rstrip()
        self.assertIsNone(BANNER_LINE1_RE.match(header))


class TestDisplayPathFor(unittest.TestCase):
    """Path-trimming rule: if any 'app' component exists, keep from one
    directory before it; otherwise leave unchanged."""

    def test_user_example_from_request(self):
        """The motivating example: keep `<dir-before-app>/app/...`."""
        result = merge_files.display_path_for(Path(
            "/Users/mark.a.nichols/Documents/Projects/Checkers/checkers/app/view/foo.py"
        ))
        self.assertEqual(result, Path("checkers/app/view/foo.py"))

    def test_no_app_component_returns_path_unchanged(self):
        """If 'app' isn't a path component, the full path is preserved."""
        original = Path("/Users/x/Documents/Projects/foo/bar.py")
        self.assertEqual(merge_files.display_path_for(original), original)

    def test_relative_path_with_app_preserves_prefix(self):
        """Relative paths already start before 'app'; no trim needed."""
        original = Path("src/app/main.py")
        self.assertEqual(merge_files.display_path_for(original), original)

    def test_app_at_root_of_relative_path(self):
        """No directory exists before 'app', so the path is returned as-is."""
        original = Path("app/main.py")
        self.assertEqual(merge_files.display_path_for(original), original)

    def test_app_at_root_of_absolute_path(self):
        """`/app/main.py` becomes `app/main.py` — the anchor is stripped but
        no preceding directory exists to keep."""
        result = merge_files.display_path_for(Path("/app/main.py"))
        self.assertEqual(result, Path("app/main.py"))

    def test_nested_app_components_anchor_on_outermost(self):
        """First-occurrence rule: with two `app/` levels, anchor on the
        outer one so the consumer sees the broader context."""
        result = merge_files.display_path_for(Path(
            "/x/y/proj/app/services/app/main.py"
        ))
        self.assertEqual(result, Path("proj/app/services/app/main.py"))

    def test_app_as_filename_at_end_of_path(self):
        """A file or dir literally named `app` at the end still triggers
        the rule (step back one)."""
        result = merge_files.display_path_for(Path("/x/y/app"))
        self.assertEqual(result, Path("y/app"))

    def test_substring_matches_do_not_trigger(self):
        """`apple`, `application`, etc. are NOT path components named 'app'
        and must not trigger trimming."""
        original = Path("/Users/x/apple/foo.py")
        self.assertEqual(merge_files.display_path_for(original), original)
        original2 = Path("/Users/x/application/bar.py")
        self.assertEqual(merge_files.display_path_for(original2), original2)

    def test_app_inside_directory_name_does_not_trigger(self):
        """A directory named like `my-app` or `app-server` is not 'app'."""
        original = Path("/Users/x/my-app/foo.py")
        self.assertEqual(merge_files.display_path_for(original), original)


class TestMakeBanner(unittest.TestCase):
    """2-line banner format for whole (unsplit) files."""

    SHA = "a" * 64
    SHA_NORM = "b" * 64

    def _banner(self, **kw):
        defaults = dict(
            path=Path("/srv/projects/foo/bar.py"),
            index=1, total=3, lines=10,
            sha=self.SHA, sha_normalized=self.SHA_NORM,
        )
        defaults.update(kw)
        return merge_files.make_banner(**defaults)

    def test_banner_is_exactly_two_lines(self):
        """Output contract: every whole-file banner consumes exactly 2 lines."""
        banner = self._banner()
        # The banner string ends with \n, so splitlines gives the 2 lines.
        self.assertEqual(len(banner.splitlines()), 2)
        self.assertEqual(banner.count("\n"), 2)

    def test_line1_matches_spec_regex(self):
        """Line 1 is parseable with the documented regex."""
        line1 = self._banner().splitlines()[0]
        m = BANNER_LINE1_RE.match(line1)
        self.assertIsNotNone(m, f"Line 1 didn't match spec: {line1!r}")

    def test_line1_carries_index_total_path_lines(self):
        """Line 1 contains index, total, full path, and line count."""
        line1 = self._banner(index=2, total=5, lines=42).splitlines()[0]
        m = BANNER_LINE1_RE.match(line1)
        assert m is not None
        index, total, path, lines = m.groups()
        self.assertEqual(index, "2")
        self.assertEqual(total, "5")
        self.assertEqual(path, "/srv/projects/foo/bar.py")
        self.assertEqual(lines, "42")

    def test_line2_carries_both_shas(self):
        """Line 2 of a whole-file banner has sha= and sha-norm=."""
        line2 = self._banner().splitlines()[1]
        m = BANNER_LINE2_WHOLE_RE.match(line2)
        self.assertIsNotNone(m, f"Line 2 didn't match spec: {line2!r}")
        self.assertEqual(m.group(1), self.SHA)
        self.assertEqual(m.group(2), self.SHA_NORM)

    def test_no_trailing_blank_line(self):
        """Banner does not emit a leading or trailing blank line — the next
        banner / file content starts immediately on the next line."""
        banner = self._banner()
        self.assertFalse(banner.startswith("\n"))
        self.assertFalse(banner.endswith("\n\n"))
        self.assertTrue(banner.endswith("\n"))


class TestMakeSplitBanner(unittest.TestCase):
    """2-line banner format for one chunk of a split file."""

    SHA = "c" * 64

    def _banner(self, **kw):
        defaults = dict(
            path=Path("/srv/big.py"),
            index=12, total=80, chunk_lines=4998,
            sha=self.SHA, part_n=2, part_total=3,
            range_start=5001, range_end=9998, file_total_lines=15000,
        )
        defaults.update(kw)
        return merge_files.make_split_banner(**defaults)

    def test_banner_is_exactly_two_lines(self):
        """Output contract: every split-chunk banner consumes exactly 2 lines."""
        banner = self._banner()
        self.assertEqual(len(banner.splitlines()), 2)
        self.assertEqual(banner.count("\n"), 2)

    def test_line1_matches_same_regex_as_whole_file(self):
        """Line 1's shape is identical for split and unsplit — consumers can
        parse it with one regex regardless."""
        line1 = self._banner().splitlines()[0]
        m = BANNER_LINE1_RE.match(line1)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(4), "4998")  # chunk's own line count

    def test_line2_omits_sha_norm_and_carries_part_and_range(self):
        """Line 2 of a split chunk: sha + part= + range= (no sha-norm)."""
        line2 = self._banner().splitlines()[1]
        m = BANNER_LINE2_SPLIT_RE.match(line2)
        self.assertIsNotNone(m, f"Line 2 didn't match split spec: {line2!r}")
        sha, part_n, part_total, r_start, r_end, file_total = m.groups()
        self.assertEqual(sha, self.SHA)
        self.assertEqual(part_n, "2")
        self.assertEqual(part_total, "3")
        self.assertEqual(r_start, "5001")
        self.assertEqual(r_end, "9998")
        self.assertEqual(file_total, "15000")
        self.assertNotIn("sha-norm=", line2)

    def test_lines_equals_range_width_invariant(self):
        """Invariant: line-1 `K lines` == range_end - range_start + 1."""
        banner = self._banner(chunk_lines=4998, range_start=5001, range_end=9998)
        line1, line2 = banner.splitlines()
        m1 = BANNER_LINE1_RE.match(line1)
        m2 = BANNER_LINE2_SPLIT_RE.match(line2)
        assert m1 is not None and m2 is not None
        chunk_lines = int(m1.group(4))
        a = int(m2.group(4))
        b = int(m2.group(5))
        self.assertEqual(chunk_lines, b - a + 1)


class _MergeTestBase(unittest.TestCase):
    """Shared scaffolding: an isolated temp dir + helpers for merge() tests."""

    # Default cap big enough that merge() never splits — keeps the legacy
    # tests focused on single-file output behavior.
    HUGE_CAP = 10 ** 9

    def setUp(self):
        self.tmpdir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.output = self.tmpdir / "out.txt"

    def _write(self, name: str, content: str) -> Path:
        path = self.tmpdir / name
        path.write_text(content, encoding="utf-8")
        return path

    def _merge(self, files, no_banners=False, max_lines=None) -> str:
        """Run merge(), expecting a single output file, return its text."""
        if max_lines is None:
            max_lines = self.HUGE_CAP
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            written = merge_files.merge(
                files=files,
                output_base=self.output,
                max_lines=max_lines,
                no_banners=no_banners,
            )
        self.assertEqual(len(written), 1,
                         f"expected single output file, got {written}")
        return written[0].read_text(encoding="utf-8")

    def _merge_paths(self, files, max_lines) -> list[Path]:
        """Run merge() and return the list of written paths (multi-file OK)."""
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return merge_files.merge(
                files=files,
                output_base=self.output,
                max_lines=max_lines,
            )


class TestMergeHappyPath(_MergeTestBase):
    """Normal-case behavior of `merge` with the new 2-line banner."""

    def test_concatenates_two_files_in_order(self):
        first = self._write("a.txt", "alpha\n")
        second = self._write("b.txt", "beta\n")
        result = self._merge([first, second])
        self.assertLess(result.index("alpha"), result.index("beta"))

    def test_banners_included_by_default(self):
        first = self._write("a.txt", "alpha\n")
        result = self._merge([first])
        self.assertIn("[1/1]", result)
        self.assertIn(str(first), result)

    def test_no_banners_flag_omits_banners(self):
        first = self._write("a.txt", "alpha\n")
        second = self._write("b.txt", "beta\n")
        result = self._merge([first, second], no_banners=True)
        self.assertNotIn("[1/2]", result)
        self.assertNotIn("===", result)
        self.assertEqual(result, "alpha\nbeta\n")

    def test_adds_trailing_newline_when_missing(self):
        """File without trailing \\n gets one so the next banner starts on a new line."""
        first = self._write("a.txt", "no-newline")
        second = self._write("b.txt", "beta\n")
        result = self._merge([first, second])
        idx = result.index("no-newline")
        self.assertEqual(result[idx + len("no-newline")], "\n")

    def test_preserves_existing_trailing_newline(self):
        first = self._write("a.txt", "alpha\n")
        result = self._merge([first], no_banners=True)
        self.assertEqual(result, "alpha\n")

    def test_banner_line_count(self):
        first = self._write("a.txt", "one\ntwo\nthree\n")
        result = self._merge([first])
        self.assertIn("· 3 lines", result)

    def test_banner_line_count_handles_missing_trailing_newline(self):
        first = self._write("a.txt", "one\ntwo\nthree")
        result = self._merge([first])
        self.assertIn("· 3 lines", result)

    def test_banner_line_count_for_empty_file(self):
        empty = self._write("empty.txt", "")
        result = self._merge([empty])
        self.assertIn("· 0 lines", result)

    def test_banner_sha_matches_file_content(self):
        content = "hello\n"
        first = self._write("a.txt", content)
        expected_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        result = self._merge([first])
        self.assertIn(f"sha={expected_sha}", result)

    def test_banner_sha_differs_for_different_content(self):
        first = self._write("a.txt", "alpha\n")
        second = self._write("b.txt", "beta\n")
        result = self._merge([first, second])
        shas = re.findall(r"sha=([a-f0-9]{64})  sha-norm=", result)
        self.assertEqual(len(shas), 2)
        self.assertNotEqual(shas[0], shas[1])

    def test_banner_normalized_sha(self):
        content = "  hello world  \n"
        first = self._write("a.txt", content)
        expected = hashlib.sha256((content.strip() + "\n").encode("utf-8")).hexdigest()
        result = self._merge([first])
        self.assertIn(f"sha-norm={expected}", result)

    def test_normalized_sha_equal_for_whitespace_only_differences(self):
        bare = self._write("bare.txt", "hello world\n")
        padded = self._write("padded.txt", "\n\n  hello world  \n\n")
        result = self._merge([bare, padded])
        norms = re.findall(r"sha-norm=([a-f0-9]{64})", result)
        self.assertEqual(len(norms), 2)
        self.assertEqual(norms[0], norms[1])

    def test_raw_sha_differs_for_whitespace_only_differences(self):
        bare = self._write("bare.txt", "hello world\n")
        padded = self._write("padded.txt", "\n\n  hello world  \n\n")
        result = self._merge([bare, padded])
        raws = re.findall(r"sha=([a-f0-9]{64})  sha-norm=", result)
        self.assertEqual(len(raws), 2)
        self.assertNotEqual(raws[0], raws[1])


class TestIsLikelyBinary(unittest.TestCase):
    """Binary-content heuristic (`is_likely_binary`)."""

    def test_pure_ascii_is_text(self):
        self.assertFalse(merge_files.is_likely_binary(b"hello world\n"))

    def test_utf8_with_non_ascii_is_text(self):
        """UTF-8 with multi-byte chars (no NULs) reads as text."""
        self.assertFalse(merge_files.is_likely_binary("héllo · wörld\n".encode("utf-8")))

    def test_lossy_utf8_without_nul_is_text(self):
        """Random non-UTF-8 bytes that don't include NUL are still 'text'
        — they'll decode lossily with U+FFFD, but that's recoverable as
        text and isn't worth excluding from the merge."""
        self.assertFalse(merge_files.is_likely_binary(b"valid \xff\xfe stuff\n"))

    def test_empty_file_is_text(self):
        """Empty content has no NUL, so it's treated as (trivial) text."""
        self.assertFalse(merge_files.is_likely_binary(b""))

    def test_png_signature_is_binary(self):
        """Real PNG headers have NULs in the IHDR chunk-length bytes."""
        self.assertTrue(merge_files.is_likely_binary(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        ))

    def test_pdf_header_with_embedded_nul_is_binary(self):
        """Synthetic PDF-like content: ASCII signature then binary stream."""
        self.assertTrue(merge_files.is_likely_binary(
            b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n4 0 obj\n<<\n/Length 5\x00\x00\x00>>"
        ))

    def test_zip_signature_is_binary(self):
        """ZIP files start with PK\\x03\\x04 then have NULs."""
        self.assertTrue(merge_files.is_likely_binary(
            b"PK\x03\x04\x14\x00\x00\x00\x08\x00"
        ))

    def test_nul_outside_sample_window_does_not_flag_as_binary(self):
        """NUL bytes past `sample_size` are not checked — the heuristic
        is intentionally bounded so a giant text file with one weird byte
        deep inside doesn't tank performance or get falsely flagged."""
        data = b"hello\n" * 2000 + b"\x00"  # NUL well past 8 KiB
        self.assertFalse(merge_files.is_likely_binary(data, sample_size=8192))


class TestMergeEdgeCases(_MergeTestBase):
    """Failure-mode behavior of `merge`."""

    def test_missing_file_is_skipped_others_still_merged(self):
        missing = self.tmpdir / "does-not-exist.txt"
        present = self._write("b.txt", "beta\n")
        result = self._merge([missing, present])
        self.assertIn("beta", result)

    def test_directory_path_is_skipped(self):
        subdir = self.tmpdir / "subdir"
        subdir.mkdir()
        present = self._write("b.txt", "beta\n")
        result = self._merge([subdir, present])
        self.assertIn("beta", result)
        # Only one file successfully read, so banner shows [1/1] not [1/2].
        self.assertIn("[1/1]", result)
        self.assertNotIn("[1/2]", result)

    def test_non_utf8_bytes_do_not_crash(self):
        binary = self.tmpdir / "binary.bin"
        binary.write_bytes(b"valid \xff\xfe invalid\n")
        result = self._merge([binary], no_banners=True)
        self.assertIn("valid", result)
        self.assertIn("invalid", result)

    def test_empty_file_contributes_nothing_in_no_banners_mode(self):
        """An empty file produces no content in --no-banners output (cleaner
        than the prior 'inject \\n for empty files' behavior)."""
        empty = self._write("empty.txt", "")
        present = self._write("b.txt", "beta\n")
        result = self._merge([empty, present], no_banners=True)
        self.assertEqual(result, "beta\n")

    def test_binary_file_is_skipped_from_merge(self):
        """A file with NUL bytes (PNG, PDF, zip, etc.) is skipped entirely —
        its content never appears in the merge file, and its presence doesn't
        affect ordering of the files that DID merge."""
        binary = self.tmpdir / "image.png"
        binary.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
        present = self._write("notes.md", "real text\n")
        result = self._merge([binary, present])
        self.assertIn("real text", result)
        # PNG signature bytes (or their lossy decoding) must not leak through.
        self.assertNotIn("PNG", result)
        self.assertNotIn("IHDR", result)
        # The merged file's banner should say [1/1], not [2/2] — the binary
        # file is counted as a skip, not a successful merge.
        self.assertIn("[1/1]", result)

    def test_binary_skip_is_reported_to_stderr(self):
        binary = self.tmpdir / "image.png"
        binary.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
        present = self._write("notes.md", "real text\n")
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            merge_files.merge(
                files=[binary, present],
                output_base=self.output,
                max_lines=self.HUGE_CAP,
            )
        self.assertIn("binary content", err.getvalue())
        self.assertIn("image.png", err.getvalue())

    def test_reports_skipped_count_in_stdout(self):
        """Final summary reports successfully-merged count vs. input count."""
        missing = self.tmpdir / "missing.txt"
        present = self._write("b.txt", "beta\n")
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            merge_files.merge(
                files=[missing, present],
                output_base=self.output,
                max_lines=self.HUGE_CAP,
            )
        self.assertIn("Merged 1/2 file(s)", buf.getvalue())


class TestPlanChunks(unittest.TestCase):
    """Bin-packing: `plan_chunks` decides which records land in which merge files."""

    def _record(self, path: str, n_lines: int, index: int = 1, total: int = 1):
        lines = [f"line{i}\n" for i in range(1, n_lines + 1)]
        return merge_files.FileRecord(
            path=Path(path), index=index, total=total,
            lines=lines, sha="x" * 64, sha_normalized="y" * 64,
        )

    def test_small_files_packed_into_one_merge_file(self):
        """Whole files that all fit under the cap share one merge file."""
        records = [
            self._record("a.txt", 10, 1, 3),
            self._record("b.txt", 20, 2, 3),
            self._record("c.txt", 30, 3, 3),
        ]
        plan = merge_files.plan_chunks(records, cap=200)
        self.assertEqual(len(plan), 1)
        self.assertEqual(len(plan[0]), 3)
        self.assertFalse(any(c.is_split for c in plan[0]))

    def test_overflow_starts_new_merge_file(self):
        """When the next whole file doesn't fit, a new merge file starts."""
        # cap=20, banner=2 → each merge file holds ~18 content lines.
        records = [
            self._record("a.txt", 10, 1, 3),  # 12 lines
            self._record("b.txt", 5, 2, 3),   # 7 lines  → fits with a (12+7=19 ≤ 20)
            self._record("c.txt", 10, 3, 3),  # 12 lines → doesn't fit (19+12 > 20)
        ]
        plan = merge_files.plan_chunks(records, cap=20)
        self.assertEqual(len(plan), 2)
        self.assertEqual(len(plan[0]), 2)  # a + b
        self.assertEqual(len(plan[1]), 1)  # c

    def test_oversized_file_is_split(self):
        """A single file > cap is split into ceil(L / (cap - banner)) parts."""
        # cap=10, banner=2 → 8 content lines per non-last part.
        records = [self._record("big.py", 25, 1, 1)]
        plan = merge_files.plan_chunks(records, cap=10)
        # 25 lines / 8 = ceil(3.125) = 4 parts (8 + 8 + 8 + 1).
        self.assertEqual(len(plan), 4)
        all_chunks = [c for mf in plan for c in mf]
        self.assertEqual(len(all_chunks), 4)
        for i, chunk in enumerate(all_chunks, start=1):
            self.assertTrue(chunk.is_split)
            self.assertEqual(chunk.part_n, i)
            self.assertEqual(chunk.part_total, 4)
        # Ranges are contiguous and cover [1, 25].
        ranges = [(c.range_start, c.range_end) for c in all_chunks]
        self.assertEqual(ranges, [(1, 8), (9, 16), (17, 24), (25, 25)])

    def test_split_part_lines_match_range_width(self):
        """Invariant from the spec: every chunk's content line count equals
        range_end - range_start + 1."""
        records = [self._record("big.py", 23, 1, 1)]
        plan = merge_files.plan_chunks(records, cap=10)
        for mf in plan:
            for c in mf:
                if c.is_split:
                    self.assertEqual(
                        len(c.content_lines),
                        c.range_end - c.range_start + 1,
                        f"Invariant violated on part {c.part_n}/{c.part_total}",
                    )

    def test_split_file_tail_can_share_merge_file_with_next_file(self):
        """The FINAL part of a split file can be followed by more whole files
        in the same merge file (Q3 locked-in rule)."""
        # cap=10, banner=2 → 8 content lines per full part.
        # big.py: 18 lines → parts 1,2 fill 2 merge files (8 each), part 3 = 2 lines.
        # small.py: 3 lines → fits with part 3 (banner+2 + banner+3 = 9 ≤ 10).
        records = [
            self._record("big.py", 18, 1, 2),
            self._record("small.py", 3, 2, 2),
        ]
        plan = merge_files.plan_chunks(records, cap=10)
        self.assertEqual(len(plan), 3)
        self.assertEqual([c.is_split for c in plan[0]], [True])
        self.assertEqual([c.is_split for c in plan[1]], [True])
        self.assertEqual(plan[0][0].part_n, 1)
        self.assertEqual(plan[1][0].part_n, 2)
        self.assertEqual(len(plan[2]), 2)
        self.assertTrue(plan[2][0].is_split)
        self.assertEqual(plan[2][0].part_n, 3)
        self.assertFalse(plan[2][1].is_split)
        self.assertEqual(plan[2][1].record.path, Path("small.py"))

    def test_split_file_always_starts_fresh_merge_file(self):
        """If the current merge file is non-empty when we encounter a file
        that needs splitting, we close current first — split files never
        share their FIRST part with prior content."""
        # cap=20, banner=2 → 18 content lines per part.
        # small.py: 5 lines → fits in merge file 1 (banner+5=7 lines).
        # big.py: 30 lines → must split. Should NOT share merge file 1.
        records = [
            self._record("small.py", 5, 1, 2),
            self._record("big.py", 30, 2, 2),
        ]
        plan = merge_files.plan_chunks(records, cap=20)
        # merge file 1: just small.py. merge file 2: big.py part 1. merge file 3: big.py part 2.
        self.assertEqual(len(plan), 3)
        self.assertEqual(len(plan[0]), 1)
        self.assertEqual(plan[0][0].record.path, Path("small.py"))
        self.assertFalse(plan[0][0].is_split)
        self.assertTrue(plan[1][0].is_split)
        self.assertEqual(plan[1][0].part_n, 1)

    def test_file_exactly_at_cap_after_banner_is_not_split(self):
        """A file whose content + banner == cap fits without splitting."""
        records = [self._record("exact.py", 8, 1, 1)]  # 8 + 2 banner = 10 = cap
        plan = merge_files.plan_chunks(records, cap=10)
        self.assertEqual(len(plan), 1)
        self.assertEqual(len(plan[0]), 1)
        self.assertFalse(plan[0][0].is_split)

    def test_file_one_line_over_cap_is_split_into_two_parts(self):
        """File with content==cap leaves no room for the banner → 2 parts,
        with a tiny tail (the documented edge-case quirk)."""
        records = [self._record("tiny-over.py", 10, 1, 1)]  # 10 + 2 = 12 > cap 10
        plan = merge_files.plan_chunks(records, cap=10)
        self.assertEqual(len(plan), 2)
        self.assertEqual(plan[0][0].part_n, 1)
        self.assertEqual(plan[1][0].part_n, 2)
        self.assertEqual(len(plan[0][0].content_lines), 8)  # cap - banner
        self.assertEqual(len(plan[1][0].content_lines), 2)  # remainder

    def test_empty_records_returns_empty_plan(self):
        plan = merge_files.plan_chunks([], cap=100)
        self.assertEqual(plan, [])

    def test_cap_too_small_raises(self):
        """A cap that leaves no room for content is rejected."""
        with self.assertRaises(ValueError):
            merge_files.plan_chunks([self._record("a.txt", 1)], cap=2)


class TestOutputPaths(unittest.TestCase):
    """Filename generation: `output_paths`."""

    def test_single_file_uses_base_unchanged(self):
        """count == 1 returns base verbatim — no -partNN suffix."""
        base = Path("/tmp/out.txt")
        self.assertEqual(merge_files.output_paths(base, 1), [base])

    def test_split_inserts_part_before_extension(self):
        """count > 1 inserts -partNN before the final '.' extension."""
        base = Path("/tmp/out.txt")
        paths = merge_files.output_paths(base, 3)
        self.assertEqual(paths, [
            Path("/tmp/out-part01.txt"),
            Path("/tmp/out-part02.txt"),
            Path("/tmp/out-part03.txt"),
        ])

    def test_padding_is_at_least_two_digits(self):
        """A 5-part batch uses 2-digit padding, not 1-digit."""
        paths = merge_files.output_paths(Path("/tmp/o.txt"), 5)
        self.assertTrue(all("-part0" in p.name for p in paths))

    def test_padding_expands_for_three_digit_counts(self):
        """A 100-part batch uses 3-digit padding throughout."""
        paths = merge_files.output_paths(Path("/tmp/o.txt"), 100)
        self.assertIn("o-part001.txt", str(paths[0]))
        self.assertIn("o-part100.txt", str(paths[-1]))

    def test_padding_consistent_within_batch(self):
        """Width stays the same across all parts of a single batch."""
        paths = merge_files.output_paths(Path("/tmp/o.txt"), 12)
        widths = {p.name.split("-part")[1].split(".")[0] for p in paths}
        self.assertTrue(all(len(w) == 2 for w in widths))

    def test_no_extension_appends_part_suffix(self):
        """`-o foo` (no extension) → `foo-part01`, etc."""
        paths = merge_files.output_paths(Path("/tmp/foo"), 2)
        self.assertEqual(paths, [Path("/tmp/foo-part01"), Path("/tmp/foo-part02")])

    def test_multi_dot_extension_splits_at_last_dot(self):
        """`-o foo.tar.gz` splits at the last '.': foo.tar-part01.gz.
        Documented rule — users with compound extensions should pass `-o foo`."""
        paths = merge_files.output_paths(Path("/tmp/foo.tar.gz"), 2)
        self.assertEqual(paths, [
            Path("/tmp/foo.tar-part01.gz"),
            Path("/tmp/foo.tar-part02.gz"),
        ])


class TestSplittingEndToEnd(_MergeTestBase):
    """End-to-end: merge() produces split outputs that reassemble correctly."""

    def _make_file(self, name: str, n_lines: int) -> Path:
        content = "".join(f"{name}-line-{i}\n" for i in range(1, n_lines + 1))
        return self._write(name, content)

    def test_split_produces_multiple_output_files(self):
        big = self._make_file("big.txt", 50)
        paths = self._merge_paths([big], max_lines=20)
        self.assertGreater(len(paths), 1)
        # All paths exist and are non-empty.
        for p in paths:
            self.assertTrue(p.exists())
            self.assertGreater(p.stat().st_size, 0)

    def test_split_paths_use_part_naming(self):
        big = self._make_file("big.txt", 50)
        paths = self._merge_paths([big], max_lines=20)
        self.assertTrue(all("-part" in p.name for p in paths))

    def test_split_reassembly_reproduces_original_content(self):
        """Concatenating all part contents (with banners stripped) reproduces
        the original file. This is the spec's primary reassembly contract."""
        n_lines = 137
        big = self._make_file("big.txt", n_lines)
        original = big.read_text(encoding="utf-8")
        paths = self._merge_paths([big], max_lines=15)

        # For each part, find its banner pair and extract the content slice.
        reassembled_parts: dict[int, str] = {}
        for p in paths:
            text = p.read_text(encoding="utf-8")
            lines = text.split("\n")
            i = 0
            while i < len(lines):
                if lines[i].startswith("=== ["):
                    line1 = lines[i]
                    line2 = lines[i + 1]
                    m1 = BANNER_LINE1_RE.match(line1)
                    m2 = BANNER_LINE2_SPLIT_RE.match(line2)
                    self.assertIsNotNone(m1)
                    self.assertIsNotNone(m2)
                    chunk_n_lines = int(m1.group(4))
                    part_n = int(m2.group(2))
                    # Content immediately follows line 2; chunk_n_lines lines.
                    content_lines = lines[i + 2 : i + 2 + chunk_n_lines]
                    reassembled_parts[part_n] = "\n".join(content_lines) + "\n"
                    i = i + 2 + chunk_n_lines
                else:
                    i += 1

        reassembled = "".join(reassembled_parts[k] for k in sorted(reassembled_parts))
        self.assertEqual(reassembled, original)

    def test_split_banners_all_carry_same_whole_file_sha(self):
        """Every chunk of a split file shares the same `sha=` (the whole-file
        SHA) — that's the reassembly anchor."""
        big = self._make_file("big.txt", 80)
        original_bytes = big.read_bytes()
        expected_sha = hashlib.sha256(original_bytes).hexdigest()
        paths = self._merge_paths([big], max_lines=20)
        all_text = "".join(p.read_text(encoding="utf-8") for p in paths)
        shas_in_split_banners = re.findall(
            r"=== sha=([a-f0-9]{64})  part=", all_text
        )
        self.assertGreater(len(shas_in_split_banners), 1)
        self.assertTrue(all(s == expected_sha for s in shas_in_split_banners))

    def test_split_part_total_is_consistent_across_chunks(self):
        """part=N/M — M is the same for every chunk of one file."""
        big = self._make_file("big.txt", 60)
        paths = self._merge_paths([big], max_lines=20)
        all_text = "".join(p.read_text(encoding="utf-8") for p in paths)
        totals = re.findall(r"  part=\d+/(\d+)  ", all_text)
        self.assertGreater(len(totals), 1)
        self.assertEqual(len(set(totals)), 1)

    def test_lines_range_invariant_holds_in_real_output(self):
        """The K == B - A + 1 invariant holds for every split chunk
        in real merged output."""
        big = self._make_file("big.txt", 100)
        paths = self._merge_paths([big], max_lines=15)
        all_text = "".join(p.read_text(encoding="utf-8") for p in paths)
        line_pairs = re.findall(
            r"(=== \[\d+/\d+\] .+ · (\d+) lines)\n"
            r"=== sha=[a-f0-9]{64}  part=\d+/\d+  range=(\d+)-(\d+)/\d+",
            all_text,
        )
        self.assertGreater(len(line_pairs), 1)
        for _, k_str, a_str, b_str in line_pairs:
            k, a, b = int(k_str), int(a_str), int(b_str)
            self.assertEqual(k, b - a + 1,
                             f"invariant broke: K={k}, range={a}-{b}")

    def test_every_merge_file_starts_with_merge_file_header(self):
        """Every output file's first line is `=== merge-file P/Q`."""
        big = self._make_file("big.txt", 80)
        paths = self._merge_paths([big], max_lines=20)
        self.assertGreater(len(paths), 1)
        for p in paths:
            first = p.read_text(encoding="utf-8").split("\n", 1)[0]
            self.assertIsNotNone(
                MERGE_FILE_HEADER_RE.match(first),
                f"first line not a merge-file header: {first!r}",
            )

    def test_merge_file_headers_count_from_one_to_total(self):
        """Header P numbers run 1..N matching the number of output files."""
        big = self._make_file("big.txt", 100)
        paths = self._merge_paths([big], max_lines=20)
        ps = []
        totals = set()
        for p in paths:
            first = p.read_text(encoding="utf-8").split("\n", 1)[0]
            m = MERGE_FILE_HEADER_RE.match(first)
            assert m is not None
            ps.append(int(m.group(1)))
            totals.add(int(m.group(2)))
        self.assertEqual(ps, list(range(1, len(paths) + 1)))
        self.assertEqual(totals, {len(paths)})

    def test_single_output_still_gets_a_merge_file_header(self):
        """Even when the merge fits in one file, the header is present
        (with `1/1`) — consumers handle one shape, not two."""
        small = self._make_file("small.txt", 5)
        paths = self._merge_paths([small], max_lines=100)
        self.assertEqual(len(paths), 1)
        first = paths[0].read_text(encoding="utf-8").split("\n", 1)[0]
        self.assertEqual(first, "=== merge-file 1/1")

    def test_header_overhead_is_subtracted_from_cap(self):
        """Total lines in any merge file (including header) stay at or below
        the cap — the bin-packer reserves the header line."""
        big = self._make_file("big.txt", 200)
        cap = 20
        paths = self._merge_paths([big], max_lines=cap)
        for p in paths:
            n_lines = p.read_text(encoding="utf-8").count("\n")
            self.assertLessEqual(
                n_lines, cap,
                f"{p.name} has {n_lines} lines, exceeds cap {cap}",
            )

    def test_single_output_keeps_base_name_under_cap(self):
        """If everything fits under the cap, the output is one file with
        the base name unchanged (no -part suffix)."""
        small = self._make_file("small.txt", 5)
        paths = self._merge_paths([small], max_lines=100)
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0], self.output)


class TestExpandPaths(unittest.TestCase):
    """Directory expansion (`expand_paths`)."""

    def setUp(self):
        self.tmpdir = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def _touch(self, rel: str, content: str = "x\n") -> Path:
        path = self.tmpdir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_plain_files_pass_through_unchanged(self):
        a = self._touch("a.txt")
        b = self._touch("b.txt")
        self.assertEqual(merge_files.expand_paths([a, b]), [a, b])

    def test_flat_directory_expanded_alphabetically(self):
        d = self.tmpdir / "flat"
        d.mkdir()
        c = self._touch("flat/c.txt")
        a = self._touch("flat/a.txt")
        b = self._touch("flat/b.txt")
        self.assertEqual(merge_files.expand_paths([d]), [a, b, c])

    def test_nested_directory_walked_recursively(self):
        root = self.tmpdir / "proj"
        root.mkdir()
        top = self._touch("proj/top.txt")
        deep = self._touch("proj/sub/deep.txt")
        deeper = self._touch("proj/sub/inner/deeper.txt")
        result = merge_files.expand_paths([root])
        self.assertEqual(set(result), {top, deep, deeper})
        self.assertEqual(result, sorted(result))

    def test_hidden_files_in_directory_are_included(self):
        d = self.tmpdir / "withhidden"
        d.mkdir()
        visible = self._touch("withhidden/visible.txt")
        hidden = self._touch("withhidden/.env.example")
        self.assertEqual(set(merge_files.expand_paths([d])), {visible, hidden})

    def test_ds_store_files_are_excluded(self):
        d = self.tmpdir / "macfolder"
        d.mkdir()
        kept = self._touch("macfolder/keep.txt")
        self._touch("macfolder/.DS_Store", "finder junk\n")
        self._touch("macfolder/nested/.DS_Store", "more junk\n")
        kept_nested = self._touch("macfolder/nested/keep2.txt")
        self.assertEqual(set(merge_files.expand_paths([d])), {kept, kept_nested})

    def test_hidden_subdirectories_are_walked(self):
        root = self.tmpdir / "proj"
        root.mkdir()
        readme = self._touch("proj/README.md")
        workflow = self._touch("proj/.github/workflows/ci.yml")
        self.assertEqual(set(merge_files.expand_paths([root])), {readme, workflow})

    def test_mixed_file_and_directory_args(self):
        loose = self._touch("loose.txt")
        d = self.tmpdir / "bundle"
        d.mkdir()
        inside = self._touch("bundle/inside.txt")
        self.assertEqual(merge_files.expand_paths([loose, d]), [loose, inside])

    def test_empty_directory_contributes_nothing(self):
        empty = self.tmpdir / "empty"
        empty.mkdir()
        other = self._touch("other.txt")
        self.assertEqual(merge_files.expand_paths([empty, other]), [other])

    def test_hidden_file_passed_directly_is_preserved(self):
        hidden = self._touch(".env")
        self.assertEqual(merge_files.expand_paths([hidden]), [hidden])

    def test_nonexistent_path_is_preserved_for_merge_to_warn(self):
        missing = self.tmpdir / "does-not-exist"
        self.assertEqual(merge_files.expand_paths([missing]), [missing])

    def test_symlink_to_file_in_dir_is_skipped(self):
        d = self.tmpdir / "dir"
        d.mkdir()
        real = self._touch("dir/real.txt")
        link = d / "link.txt"
        try:
            link.symlink_to(real)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not supported on this filesystem")
        self.assertEqual(merge_files.expand_paths([d]), [real])

    def test_folder_drop_end_to_end_through_merge(self):
        d = self.tmpdir / "drop"
        d.mkdir()
        (d / "one.txt").write_text("ONE\n")
        (d / "two.txt").write_text("TWO\n")
        files = merge_files.expand_paths([d])
        output = self.tmpdir / "out.txt"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            merge_files.merge(files=files, output_base=output, max_lines=10 ** 9)
        result = output.read_text(encoding="utf-8")
        self.assertIn("[1/2]", result)
        self.assertIn("[2/2]", result)
        self.assertIn("ONE", result)
        self.assertIn("TWO", result)


class TestMain(unittest.TestCase):
    """End-to-end tests for `main`."""

    def setUp(self):
        self.tmpdir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.addCleanup(setattr, sys, "argv", sys.argv)

    def _run_main(self, argv):
        sys.argv = ["merge-files.py"] + argv
        original_platform = merge_files.sys.platform
        merge_files.sys.platform = "linux"
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                return merge_files.main()
        finally:
            merge_files.sys.platform = original_platform

    def _write_src(self, content: str) -> Path:
        path = self.tmpdir / "src.txt"
        path.write_text(content, encoding="utf-8")
        return path

    def test_creates_missing_parent_dir_for_output(self):
        """`-o` with a non-existent parent directory must not crash."""
        src = self._write_src("hello\n")
        nested_out = self.tmpdir / "does" / "not" / "exist" / "out.txt"
        rc = self._run_main([str(src), "-o", str(nested_out)])
        self.assertEqual(rc, 0)
        self.assertTrue(nested_out.exists())
        self.assertIn("hello", nested_out.read_text(encoding="utf-8"))


class TestParseArgs(unittest.TestCase):
    """Argument parsing (`parse_args`)."""

    def _parse(self, argv):
        saved = sys.argv
        try:
            sys.argv = ["merge-files.py"] + argv
            return merge_files.parse_args()
        finally:
            sys.argv = saved

    def test_requires_at_least_one_file(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self._parse([])

    def test_files_collected_in_order(self):
        ns = self._parse(["a.py", "b.rb", "c.yml"])
        self.assertEqual([p.name for p in ns.files], ["a.py", "b.rb", "c.yml"])

    def test_output_flag(self):
        ns = self._parse(["a.py", "-o", "/srv/output/merged.txt"])
        self.assertEqual(ns.output, Path("/srv/output/merged.txt"))

    def test_output_defaults_to_none(self):
        ns = self._parse(["a.py"])
        self.assertIsNone(ns.output)

    def test_no_banners_flag(self):
        ns = self._parse(["a.py", "--no-banners"])
        self.assertTrue(ns.no_banners)

    def test_no_banners_defaults_to_false(self):
        ns = self._parse(["a.py"])
        self.assertFalse(ns.no_banners)

    def test_max_lines_defaults_to_3500(self):
        ns = self._parse(["a.py"])
        self.assertEqual(ns.max_lines, 3500)

    def test_max_lines_accepts_override(self):
        ns = self._parse(["a.py", "--max-lines", "1500"])
        self.assertEqual(ns.max_lines, 1500)

    def test_max_lines_floor_is_enforced(self):
        """Values below MIN_MAX_LINES are rejected by argparse with SystemExit."""
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self._parse(["a.py", "--max-lines", "5"])


# Deletions-manifest per-path regex (MERGE-FORMAT.md §11, format version 1.1).
DELETED_PATH_RE = re.compile(r"^=== deleted (.+)$")


def _make_record(path: str, content: str, index: int = 1, total: int = 1):
    """Build a FileRecord the way read_records would (forced trailing \\n, shas)."""
    text = content if (content == "" or content.endswith("\n")) else content + "\n"
    lines = text.splitlines(keepends=True)
    sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    sha_norm = hashlib.sha256((text.strip() + "\n").encode("utf-8")).hexdigest()
    return merge_files.FileRecord(
        path=Path(path), index=index, total=total,
        lines=lines, sha=sha, sha_normalized=sha_norm,
    )


class TestMakeDeletionsSection(unittest.TestCase):
    """Deletions manifest emitted atop part 1 of a --track run."""

    def test_carries_format_version_and_count(self):
        section = merge_files.make_deletions_section(["a/x.py", "b/y.py"])
        lines = section.splitlines()
        self.assertEqual(lines[0], "=== format-version=1.1")
        self.assertEqual(lines[1], "=== deleted-files 2")

    def test_one_line_per_deleted_path(self):
        section = merge_files.make_deletions_section(["a/x.py", "b/y.py", "c/z.py"])
        paths = [DELETED_PATH_RE.match(ln).group(1)
                 for ln in section.splitlines() if DELETED_PATH_RE.match(ln)]
        self.assertEqual(paths, ["a/x.py", "b/y.py", "c/z.py"])

    def test_lines_do_not_collide_with_file_banner_regex(self):
        """No manifest line matches the file-banner line-1 regex, so existing
        consumers skip the whole block."""
        section = merge_files.make_deletions_section(["a/x.py"])
        for line in section.splitlines():
            self.assertIsNone(BANNER_LINE1_RE.match(line))
            self.assertIsNone(MERGE_FILE_HEADER_RE.match(line))


class TestSnapshotAndTracking(unittest.TestCase):
    """`snapshot_from_records` and `apply_tracking` diff logic (no I/O)."""

    def test_snapshot_keys_on_display_path(self):
        rec = _make_record("/x/y/proj/app/view/foo.py", "code\n")
        snap = merge_files.snapshot_from_records([rec])
        self.assertIn("proj/app/view/foo.py", snap)
        self.assertEqual(snap["proj/app/view/foo.py"]["sha_norm"], rec.sha_normalized)

    def test_new_file_is_changed(self):
        rec = _make_record("/t/a.py", "a\n")
        result = merge_files.apply_tracking([rec], baseline_files={})
        self.assertEqual([r.path for r in result.changed], [Path("/t/a.py")])
        self.assertEqual(result.deleted, [])

    def test_unchanged_file_is_skipped(self):
        rec = _make_record("/t/a.py", "a\n")
        baseline = {"/t/a.py": {"sha_norm": rec.sha_normalized}}
        result = merge_files.apply_tracking([rec], baseline)
        self.assertEqual(result.changed, [])

    def test_whitespace_only_change_is_not_a_change(self):
        """sha-norm is the change key, so reindent/trailing-space churn is
        ignored."""
        old = _make_record("/t/a.py", "hello world\n")
        new = _make_record("/t/a.py", "\n\n  hello world  \n\n")
        baseline = {"/t/a.py": {"sha_norm": old.sha_normalized}}
        result = merge_files.apply_tracking([new], baseline)
        self.assertEqual(result.changed, [])

    def test_content_change_is_detected(self):
        new = _make_record("/t/a.py", "different\n")
        baseline = {"/t/a.py": {"sha_norm": "0" * 64}}
        result = merge_files.apply_tracking([new], baseline)
        self.assertEqual(len(result.changed), 1)

    def test_deleted_file_reported(self):
        rec = _make_record("/t/a.py", "a\n")
        baseline = {"/t/a.py": {"sha_norm": rec.sha_normalized},
                    "/t/gone.py": {"sha_norm": "1" * 64}}
        result = merge_files.apply_tracking([rec], baseline)
        self.assertEqual(result.changed, [])
        self.assertEqual(result.deleted, ["/t/gone.py"])

    def test_changed_records_are_renumbered_gap_free(self):
        """Only-changed subset gets a fresh 1..K [N/M] numbering."""
        a = _make_record("/t/a.py", "a\n", index=1, total=3)
        b = _make_record("/t/b.py", "b-new\n", index=2, total=3)
        c = _make_record("/t/c.py", "c\n", index=3, total=3)
        baseline = {
            "/t/a.py": {"sha_norm": a.sha_normalized},   # unchanged
            "/t/b.py": {"sha_norm": "9" * 64},           # changed
            "/t/c.py": {"sha_norm": c.sha_normalized},   # unchanged
        }
        result = merge_files.apply_tracking([a, b, c], baseline)
        self.assertEqual([(r.index, r.total) for r in result.changed], [(1, 1)])
        self.assertEqual(result.changed[0].path, Path("/t/b.py"))

    def test_snapshot_covers_full_batch_not_just_changed(self):
        a = _make_record("/t/a.py", "a\n")
        b = _make_record("/t/b.py", "b\n")
        baseline = {"/t/a.py": {"sha_norm": a.sha_normalized}}  # a unchanged, b new
        result = merge_files.apply_tracking([a, b], baseline)
        self.assertEqual(set(result.snapshot), {"/t/a.py", "/t/b.py"})


class TestConfigAndBaselineIO(unittest.TestCase):
    """Config loading + baseline persistence, isolated via XDG_* temp dirs."""

    def setUp(self):
        self.tmpdir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.tmpdir / "state"),
            "XDG_CONFIG_HOME": str(self.tmpdir / "config"),
        }))

    def _write_config(self, text: str):
        path = merge_files.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_config_paths_honor_xdg(self):
        self.assertEqual(
            merge_files.config_path(),
            self.tmpdir / "config" / "merge-files" / "config.json",
        )
        self.assertEqual(
            merge_files.baseline_path("chan"),
            self.tmpdir / "state" / "merge-files" / "baselines" / "chan.json",
        )

    def test_missing_config_returns_defaults_silently(self):
        err = io.StringIO()
        with redirect_stderr(err):
            config = merge_files.load_config()
        self.assertEqual(config["advance"], "optimistic")
        self.assertTrue(config["report_deletions"])
        self.assertEqual(config["skip_extensions"], [])
        self.assertEqual(err.getvalue(), "")

    def test_config_overlays_onto_defaults(self):
        self._write_config('{"report_deletions": false, "skip_extensions": [".log"]}')
        config = merge_files.load_config()
        self.assertFalse(config["report_deletions"])
        self.assertEqual(config["skip_extensions"], [".log"])
        # Untouched keys keep their defaults.
        self.assertEqual(config["advance"], "optimistic")

    def test_invalid_config_json_warns_and_uses_defaults(self):
        self._write_config("{not valid json")
        err = io.StringIO()
        with redirect_stderr(err):
            config = merge_files.load_config()
        self.assertTrue(config["report_deletions"])
        self.assertIn("Invalid config JSON", err.getvalue())

    def test_missing_baseline_is_empty(self):
        self.assertEqual(merge_files.load_baseline("nope"), {})

    def test_corrupt_baseline_warns_and_treats_all_new(self):
        path = merge_files.baseline_path("chan")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken", encoding="utf-8")
        err = io.StringIO()
        with redirect_stderr(err):
            result = merge_files.load_baseline("chan")
        self.assertEqual(result, {})
        self.assertIn("Corrupt baseline", err.getvalue())

    def test_save_and_load_baseline_round_trips(self):
        snapshot = {"a/x.py": {"sha_norm": "a" * 64, "sha": "b" * 64, "lines": 3}}
        merge_files.save_baseline("chan", snapshot)
        self.assertEqual(merge_files.load_baseline("chan"), snapshot)

    def test_save_baseline_preserves_created_timestamp(self):
        merge_files.save_baseline("chan", {"a": {"sha_norm": "a" * 64}})
        first = json.loads(merge_files.baseline_path("chan").read_text())
        merge_files.save_baseline("chan", {"b": {"sha_norm": "b" * 64}})
        second = json.loads(merge_files.baseline_path("chan").read_text())
        self.assertEqual(first["created"], second["created"])

    def test_save_baseline_is_atomic_no_tmp_left_behind(self):
        merge_files.save_baseline("chan", {"a": {"sha_norm": "a" * 64}})
        tmp = merge_files.baseline_path("chan").with_name("chan.json.tmp")
        self.assertFalse(tmp.exists())


class _TrackMergeBase(unittest.TestCase):
    """End-to-end --track scaffolding with XDG-isolated state/config."""

    def setUp(self):
        self.tmpdir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.tmpdir / "state"),
            "XDG_CONFIG_HOME": str(self.tmpdir / "config"),
        }))
        self.tree = self.tmpdir / "tree"
        self.tree.mkdir()
        self.output = self.tmpdir / "out.txt"

    def _write(self, rel: str, content: str) -> Path:
        path = self.tree / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def _rm(self, rel: str):
        (self.tree / rel).unlink()

    def _run(self, channel="demo", max_lines=100, config=None):
        files = merge_files.expand_paths([self.tree])
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return merge_files.merge(
                files=files, output_base=self.output,
                max_lines=max_lines, track=channel, config=config,
            )

    def _out_text(self, paths) -> str:
        return "".join(p.read_text(encoding="utf-8") for p in paths)


class TestTrackMergeEndToEnd(_TrackMergeBase):
    """The change-only merge behavior of --track, wired through merge()."""

    def test_first_run_merges_everything_and_writes_baseline(self):
        self._write("a.txt", "alpha\n")
        self._write("b.txt", "beta\n")
        paths = self._run()
        text = self._out_text(paths)
        self.assertIn("alpha", text)
        self.assertIn("beta", text)
        # Baseline now exists with both files.
        baseline = merge_files.load_baseline("demo")
        self.assertEqual(len(baseline), 2)

    def test_second_run_with_no_changes_writes_nothing(self):
        self._write("a.txt", "alpha\n")
        self._run()
        self.output.unlink()  # remove first output so we can detect a re-write
        result = self._run()
        self.assertEqual(result, [])            # success, nothing written
        self.assertFalse(self.output.exists())  # no output file produced

    def test_only_changed_file_is_merged(self):
        self._write("a.txt", "alpha\n")
        self._write("b.txt", "beta\n")
        self._run()
        self._write("b.txt", "beta CHANGED\n")
        paths = self._run()
        text = self._out_text(paths)
        self.assertIn("beta CHANGED", text)
        self.assertNotIn("alpha", text)  # a.txt unchanged → excluded
        self.assertIn("[1/1]", text)     # renumbered to a one-file batch

    def test_new_file_is_included_on_later_run(self):
        self._write("a.txt", "alpha\n")
        self._run()
        self._write("c.txt", "gamma\n")
        paths = self._run()
        self.assertIn("gamma", self._out_text(paths))

    def test_deletion_recorded_in_output_and_baseline(self):
        self._write("a.txt", "alpha\n")
        self._write("b.txt", "beta\n")
        self._run()
        self._rm("b.txt")
        self._write("a.txt", "alpha CHANGED\n")  # a change so there's content too
        paths = self._run()
        text = self._out_text(paths)
        self.assertIn("=== deleted-files 1", text)
        self.assertRegex(text, r"=== deleted .*b\.txt")
        # Deleted file is gone from the advanced baseline.
        self.assertNotIn(str(merge_files.display_path_for(self.tree / "b.txt")),
                         merge_files.load_baseline("demo"))

    def test_deletions_only_run_produces_manifest_file(self):
        """A run whose sole change is a deletion still writes one merge file
        carrying the manifest, so the consumer learns of the removal."""
        self._write("a.txt", "alpha\n")
        self._run()
        self._rm("a.txt")
        paths = self._run()
        self.assertEqual(len(paths), 1)
        text = self._out_text(paths)
        self.assertIn("=== deleted-files 1", text)
        self.assertIn("=== format-version=1.1", text)

    def test_report_deletions_false_suppresses_manifest(self):
        self._write("a.txt", "alpha\n")
        self._write("b.txt", "beta\n")
        self._run()
        self._rm("b.txt")
        self._write("a.txt", "alpha CHANGED\n")
        paths = self._run(config={"report_deletions": False})
        self.assertNotIn("deleted", self._out_text(paths))

    def test_no_deletions_output_is_plain_v1_format(self):
        """A change-only run with no deletions emits no 1.1 marker — it's
        byte-for-byte a normal merge."""
        self._write("a.txt", "alpha\n")
        self._run()
        self._write("a.txt", "alpha CHANGED\n")
        paths = self._run()
        self.assertNotIn("format-version", self._out_text(paths))

    def test_baseline_advances_so_change_not_remerged(self):
        self._write("a.txt", "alpha\n")
        self._run()
        self._write("a.txt", "alpha CHANGED\n")
        self._run()                       # merges the change, advances baseline
        result = self._run()              # nothing new now
        self.assertEqual(result, [])


class TestTrackParseArgs(unittest.TestCase):
    """--track argument parsing and validation."""

    def _parse(self, argv):
        saved = sys.argv
        try:
            sys.argv = ["merge-files.py"] + argv
            return merge_files.parse_args()
        finally:
            sys.argv = saved

    def test_track_name_accepted(self):
        ns = self._parse(["a.py", "--track", "checkers-prod"])
        self.assertEqual(ns.track, "checkers-prod")

    def test_track_defaults_to_none(self):
        ns = self._parse(["a.py"])
        self.assertIsNone(ns.track)

    def test_track_with_no_banners_is_rejected(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self._parse(["a.py", "--track", "x", "--no-banners"])

    def test_invalid_track_name_is_rejected(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self._parse(["a.py", "--track", "../escape"])


if __name__ == "__main__":
    unittest.main()
