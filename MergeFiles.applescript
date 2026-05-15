-- MergeFiles.applescript
--
-- AppleScript droplet that wraps merge-files.py for drag-and-drop on macOS.
-- Compiled into MergeFiles.app by build.sh (sibling file).
--
-- The compiled .app uses `path to me` to find its own location, then invokes
-- merge-files.py from the same directory. That means the whole merge-files
-- folder is portable — copy it anywhere on any Mac, run build.sh once, and
-- the .app finds its script via a relative path.

on open theFiles
	-- `path to me` returns the .app's own POSIX path (may end with "/").
	set appPath to POSIX path of (path to me)
	if appPath ends with "/" then
		set appPath to text 1 thru -2 of appPath
	end if
	set parentDir to do shell script "dirname " & quoted form of appPath
	set toolPath to parentDir & "/merge-files.py"

	-- Build the argv string of dropped files. Each path is shell-quoted so
	-- spaces and special characters survive.
	set fileArgs to ""
	repeat with f in theFiles
		set fileArgs to fileArgs & " " & quoted form of (POSIX path of f)
	end repeat

	try
		do shell script "python3 " & quoted form of toolPath & fileArgs
	on error errMsg number errNum
		display dialog "merge-files failed:" & return & return & errMsg ¬
			buttons {"OK"} default button "OK" with icon stop
	end try
end open

-- Double-clicking the .app (no files dropped) lands here.
on run
	display dialog "Drop one or more files or folders onto MergeFiles.app to merge them. Folders are walked recursively, including hidden (dot-prefixed) entries; .DS_Store files are skipped." ¬
		buttons {"OK"} default button "OK"
end run
