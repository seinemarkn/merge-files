#!/usr/bin/env bash
# build.sh — Compile MergeFiles.applescript into a drag-and-drop .app bundle.
#
# Usage:
#   ./build.sh             # builds MergeFiles.app in this directory
#   ./build.sh --clean     # removes any existing MergeFiles.app first
#
# The resulting MergeFiles.app accepts files dropped onto its icon and runs
# merge-files.py with those files as arguments. The .app is intentionally
# .gitignored — rebuild it on each machine by running this script.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SCRIPT_DIR/MergeFiles.applescript"
DEST="$SCRIPT_DIR/MergeFiles.app"

if [[ ! -f "$SRC" ]]; then
	echo "Error: $SRC not found." >&2
	exit 1
fi

if [[ ! -f "$SCRIPT_DIR/merge-files.py" ]]; then
	echo "Error: merge-files.py not found next to this script." >&2
	exit 1
fi

if ! command -v osacompile >/dev/null 2>&1; then
	echo "Error: osacompile not found. This builder only runs on macOS." >&2
	exit 1
fi

if [[ -e "$DEST" ]]; then
	rm -rf "$DEST"
fi

# Build in a temp dir first. If the target directory is in iCloud Drive (e.g.
# under ~/Documents), iCloud's sync agent stamps com.apple.fileprovider.fpfs#P
# onto new files mid-write, which racing with osacompile's codesign step trips
# the "resource fork, Finder information, or similar detritus not allowed"
# error. Building outside the synced tree sidesteps that.
TMP_BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_BUILD_DIR"' EXIT
TMP_APP="$TMP_BUILD_DIR/MergeFiles.app"

osacompile -o "$TMP_APP" "$SRC"
xattr -cr "$TMP_APP" 2>/dev/null || true
mv "$TMP_APP" "$DEST"

echo "Built: $DEST"
echo
echo "Next steps:"
echo "  • Drag files onto MergeFiles.app to merge them."
echo "  • Optionally move/symlink the .app to your Desktop, Dock, or ~/Applications."
