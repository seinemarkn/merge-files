"""Tests for merge-files.py.

Run with:
    python3 -m unittest commands/merge-files/test_merge_files.py
    # or from this directory:
    python3 -m unittest test_merge_files.py
"""
from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


def _load_script():
    """Load merge-files.py as a module (hyphenated filename → importlib)."""
    script_path = Path(__file__).parent / "merge-files.py"
    spec = importlib.util.spec_from_file_location("merge_files", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


merge_files = _load_script()


class TestMakeBanner(unittest.TestCase):
    """Banner formatting (`make_banner`)."""

    def test_contains_name_and_path(self):
        """Banner includes both the file name and full path."""
        path = Path("/srv/projects/foo/bar.py")
        banner = merge_files.make_banner(path, 1, 3, 10)
        self.assertIn("bar.py", banner)
        self.assertIn(str(path), banner)

    def test_shows_index_and_total(self):
        """Banner shows the file's position as 'File N of M'."""
        banner = merge_files.make_banner(Path("a.txt"), 2, 5, 0)
        self.assertIn("File 2 of 5", banner)

    def test_has_bar_separators(self):
        """Banner is bounded by two horizontal '=' bars."""
        banner = merge_files.make_banner(Path("a.txt"), 1, 1, 0)
        self.assertEqual(banner.count("=" * 72), 2)

    def test_ends_with_blank_line(self):
        """Banner ends with a blank line so content starts visually separated."""
        banner = merge_files.make_banner(Path("a.txt"), 1, 1, 0)
        self.assertTrue(banner.endswith("\n\n"))

    def test_shows_line_count(self):
        """Banner includes the file's line count, comma-formatted."""
        banner = merge_files.make_banner(Path("a.txt"), 1, 1, 1234)
        self.assertIn("Lines: 1,234", banner)


class _MergeTestBase(unittest.TestCase):
    """Shared scaffolding: an isolated temp dir + helpers for the merge tests."""

    def setUp(self):
        """Allocate a per-test temp directory via the unittest context stack."""
        self.tmpdir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.output = self.tmpdir / "out.txt"

    def _write(self, name: str, content: str) -> Path:
        """Write `content` to `name` inside the test's temp directory."""
        path = self.tmpdir / name
        path.write_text(content, encoding="utf-8")
        return path

    def _merge(self, files, no_banners=False):
        """Run merge() with stdout/stderr suppressed and return the output text."""
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            merge_files.merge(files=files, output=self.output, no_banners=no_banners)
        return self.output.read_text(encoding="utf-8")


class TestMergeHappyPath(_MergeTestBase):
    """Normal-case behavior of `merge`."""

    def test_concatenates_two_files_in_order(self):
        """Output preserves input ordering of file contents."""
        first = self._write("a.txt", "alpha\n")
        second = self._write("b.txt", "beta\n")
        result = self._merge([first, second])
        self.assertLess(result.index("alpha"), result.index("beta"))

    def test_banners_included_by_default(self):
        """Each file gets a header banner with its index/total and name."""
        first = self._write("a.txt", "alpha\n")
        result = self._merge([first])
        self.assertIn("File 1 of 1", result)
        self.assertIn("a.txt", result)

    def test_no_banners_flag_omits_banners(self):
        """`no_banners=True` produces plain concatenation with no headers."""
        first = self._write("a.txt", "alpha\n")
        second = self._write("b.txt", "beta\n")
        result = self._merge([first, second], no_banners=True)
        self.assertNotIn("File 1 of 2", result)
        self.assertNotIn("=" * 72, result)
        self.assertEqual(result, "alpha\nbeta\n")

    def test_adds_trailing_newline_when_missing(self):
        """A file without a trailing newline gets one appended so the next
        banner doesn't get glued to the prior file's last line."""
        first = self._write("a.txt", "no-newline")
        second = self._write("b.txt", "beta\n")
        result = self._merge([first, second])
        idx = result.index("no-newline")
        self.assertEqual(result[idx + len("no-newline")], "\n")

    def test_preserves_existing_trailing_newline(self):
        """A file that already ends in '\\n' is not double-terminated."""
        first = self._write("a.txt", "alpha\n")
        result = self._merge([first], no_banners=True)
        self.assertEqual(result, "alpha\n")

    def test_banner_includes_line_count(self):
        """Banner reports the correct number of lines for the file."""
        first = self._write("a.txt", "one\ntwo\nthree\n")
        result = self._merge([first])
        self.assertIn("Lines: 3", result)

    def test_banner_line_count_handles_missing_trailing_newline(self):
        """Final line without a trailing newline still counts as a line."""
        first = self._write("a.txt", "one\ntwo\nthree")
        result = self._merge([first])
        self.assertIn("Lines: 3", result)

    def test_banner_line_count_for_empty_file(self):
        """Empty file reports 0 lines."""
        empty = self._write("empty.txt", "")
        result = self._merge([empty])
        self.assertIn("Lines: 0", result)


class TestMergeEdgeCases(_MergeTestBase):
    """Failure-mode behavior of `merge`: skips, non-files, binary input."""

    def test_missing_file_is_skipped_others_still_merged(self):
        """A non-existent path is skipped; remaining files still merge."""
        missing = self.tmpdir / "does-not-exist.txt"
        present = self._write("b.txt", "beta\n")
        result = self._merge([missing, present])
        self.assertIn("beta", result)

    def test_directory_path_is_skipped(self):
        """A directory passed as a 'file' is skipped, not merged."""
        subdir = self.tmpdir / "subdir"
        subdir.mkdir()
        present = self._write("b.txt", "beta\n")
        result = self._merge([subdir, present])
        self.assertIn("beta", result)
        self.assertNotIn("File 1 of 2: subdir", result)

    def test_non_utf8_bytes_do_not_crash(self):
        """Invalid UTF-8 bytes are replaced (errors='replace'); merge succeeds."""
        binary = self.tmpdir / "binary.bin"
        binary.write_bytes(b"valid \xff\xfe invalid\n")
        result = self._merge([binary], no_banners=True)
        self.assertIn("valid", result)
        self.assertIn("invalid", result)

    def test_empty_file_is_handled(self):
        """An empty file becomes a single '\\n' (trailing-newline rule), then
        subsequent files merge normally."""
        empty = self._write("empty.txt", "")
        present = self._write("b.txt", "beta\n")
        result = self._merge([empty, present], no_banners=True)
        self.assertEqual(result, "\nbeta\n")

    def test_reports_skipped_count_in_stdout(self):
        """Final summary reports successfully-merged count vs. input count."""
        missing = self.tmpdir / "missing.txt"
        present = self._write("b.txt", "beta\n")
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            merge_files.merge(files=[missing, present], output=self.output)
        self.assertIn("Merged 1/2 file(s)", buf.getvalue())


class TestMain(unittest.TestCase):
    """End-to-end tests for `main` — focused on output-path resolution."""

    def setUp(self):
        """Allocate temp dir and save argv/platform so we can restore them."""
        self.tmpdir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.addCleanup(setattr, sys, "argv", sys.argv)

    def _run_main(self, argv):
        """Invoke main() with given argv, forcing non-darwin to skip `open`."""
        sys.argv = ["merge-files.py"] + argv
        original_platform = merge_files.sys.platform
        merge_files.sys.platform = "linux"
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                return merge_files.main()
        finally:
            merge_files.sys.platform = original_platform

    def test_creates_missing_parent_dir_for_output(self):
        """Regression: `-o` with a non-existent parent directory must not crash.

        Previously only the default-output path created its parent dir, so a
        user-supplied `-o some/missing/dir/out.txt` raised FileNotFoundError
        inside merge() when opening the output for writing.
        """
        src = self._write_src("hello\n")
        nested_out = self.tmpdir / "does" / "not" / "exist" / "out.txt"

        rc = self._run_main([str(src), "-o", str(nested_out)])

        self.assertEqual(rc, 0)
        self.assertTrue(nested_out.exists())
        self.assertIn("hello", nested_out.read_text(encoding="utf-8"))

    def _write_src(self, content: str) -> Path:
        """Create a single source file in the test's temp dir."""
        path = self.tmpdir / "src.txt"
        path.write_text(content, encoding="utf-8")
        return path


class TestParseArgs(unittest.TestCase):
    """Argument parsing (`parse_args`)."""

    def _parse(self, argv):
        """Invoke parse_args() with a temporary sys.argv override."""
        saved = sys.argv
        try:
            sys.argv = ["merge-files.py"] + argv
            return merge_files.parse_args()
        finally:
            sys.argv = saved

    def test_requires_at_least_one_file(self):
        """argparse should reject an empty argv with SystemExit."""
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self._parse([])

    def test_files_collected_in_order(self):
        """Positional file args are preserved in the order they were given."""
        ns = self._parse(["a.py", "b.rb", "c.yml"])
        self.assertEqual([p.name for p in ns.files], ["a.py", "b.rb", "c.yml"])

    def test_output_flag(self):
        """`-o` sets the output path as a Path."""
        ns = self._parse(["a.py", "-o", "/srv/output/merged.txt"])
        self.assertEqual(ns.output, Path("/srv/output/merged.txt"))

    def test_output_defaults_to_none(self):
        """Without `-o`, output is None (main() resolves the default later)."""
        ns = self._parse(["a.py"])
        self.assertIsNone(ns.output)

    def test_no_banners_flag(self):
        """`--no-banners` sets the flag to True."""
        ns = self._parse(["a.py", "--no-banners"])
        self.assertTrue(ns.no_banners)

    def test_no_banners_defaults_to_false(self):
        """Without `--no-banners`, the flag is False."""
        ns = self._parse(["a.py"])
        self.assertFalse(ns.no_banners)


if __name__ == "__main__":
    unittest.main()
