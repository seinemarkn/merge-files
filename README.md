# merge-files

Concatenate code, config, and text files — in the order you give them — into
one or more **merge files**, each preceded by a compact 2-line banner that
identifies the source file and its SHA-256 fingerprint. When the combined
content exceeds a line cap, output is split across multiple files that
reassemble by plain concatenation.

It's a single-file Python script (standard library only, no dependencies) with
an optional macOS drag-and-drop app wrapper.

## Why

Sometimes you want a whole tree of files collapsed into a small number of
self-describing text files — to share a snapshot, archive a set of sources,
review or search them as one document, move them through a text-only channel,
or feed them to another tool. `merge-files` makes that deterministic and
reversible: every file is banner-tagged with its path, line count, and hash,
and the split format is specified precisely in
[MERGE-FORMAT.md](MERGE-FORMAT.md) so a consumer can verify and reassemble the
originals without reading the source.

## Requirements

- Python 3.11+ (no third-party packages)

## Install

```sh
git clone https://github.com/seinemarkn/merge-files.git
cd merge-files
chmod +x merge-files.py   # already executable in the repo
```

## Usage

```sh
# Merge specific files, in order
python3 merge-files.py file1.py file2.rb config.yml

# Merge a whole directory (expanded recursively, alphabetically)
python3 merge-files.py src/ -o all_sources.txt

# Cap lines per merge file (output splits automatically when exceeded)
python3 merge-files.py --max-lines 2000 src/

# Plain concatenation, no banners (single file, not reassemble-able)
python3 merge-files.py a.md b.md c.md --no-banners
```

Directories are walked recursively and sorted for deterministic output. Hidden
(dot-prefixed) files are **included** (they're often real config). Symlinks and
`.DS_Store` are skipped, and binary files (NUL-byte heuristic) are excluded
automatically. With no `-o`, output defaults to
`~/tmp/merges/file-merge-<timestamp>.txt`.

## Change tracking (`--track`)

Merge **only the files that changed** since the last run, with no git or
network required — the sending machine keeps a local per-channel baseline.

```sh
# First run: merges everything, records a baseline for channel "myproj"
python3 merge-files.py src/ --track myproj

# Later: merges only files new or changed since last time
python3 merge-files.py src/ --track myproj
```

- Change is judged by a whitespace-normalized hash, so reindents and
  trailing-space churn don't count.
- Files deleted since the last run are recorded in a small manifest at the top
  of the output (advisory — the tool never modifies the consumer's tree).
- Baselines live under `~/.local/state/merge-files/baselines/<channel>.json`
  (honors `$XDG_STATE_HOME`) and advance to the current snapshot each run.
- Tracking is CLI-only and cannot be triggered by the drag-and-drop app.

## Configuration

Optional user settings at `~/.config/merge-files/config.json` (honors
`$XDG_CONFIG_HOME`); a missing file uses defaults.

```json
{
  "report_deletions": true,
  "skip_extensions": [".log", ".tmp", ".min.js"]
}
```

- `skip_extensions` — drop files by extension during **directory expansion**
  (case-insensitive, leading dot optional, compound extensions like `.min.js`
  supported). Files named explicitly on the command line are always kept.
- `report_deletions` — set `false` to omit the deletions manifest in `--track`
  runs.

## macOS drag-and-drop app

`build.sh` compiles `MergeFiles.applescript` into `MergeFiles.app`, a droplet
that runs a plain, stateless merge on whatever files/folders you drop onto it.

```sh
./build.sh
```

The built `.app` is intentionally gitignored — rebuild it per machine.

## Output format

The full consumer specification — banner grammar, split/reassembly rules,
invariants, and the `--track` deletions manifest — is in
[MERGE-FORMAT.md](MERGE-FORMAT.md).

## Tests

```sh
python3 -m unittest test_merge_files.py
```

## License

[MIT](LICENSE) © Mark Nichols
