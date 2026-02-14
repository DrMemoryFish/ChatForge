# Asset Capture Guide (Screenshots + Demo GIF)

This guide creates README media without paid tools or heavy dependencies.

## Target Files

Screenshots:
- `docs/screens/01-connect.svg` -> replace with `01-connect.png` (or keep `.svg` naming style if preferred)
- `docs/screens/02-tree-selection.svg`
- `docs/screens/03-export-settings.svg`
- `docs/screens/04-batch-progress.svg`
- `docs/screens/05-preview-logs.svg`

GIF:
- `docs/gif/archivecord-demo.gif`

## Redaction Rules (Required)

Before capture, use a test token and non-sensitive data where possible.

Always hide or blur:
- Token input value.
- Personal usernames and display names.
- Server names and channel names if they identify private communities.
- File system paths containing personal info.

## Screenshot Capture (Free Tools)

Windows:
- Use Snipping Tool (`Win + Shift + S`) or Snipping Tool app.

macOS:
- Use `Cmd + Shift + 4` (selection) or `Cmd + Shift + 5` (capture controls).

Linux:
- Use GNOME Screenshot, Flameshot, or Spectacle (all free).

Suggested capture size:
- 1280x720 or 1440x810 (16:9), PNG format.

## Demo GIF Recording (Free Tools)

Windows:
- ScreenToGif (free).

macOS:
- QuickTime Player (record) + convert to GIF with ffmpeg/ImageMagick if needed.

Linux:
- Peek (easy GIF recorder) or OBS (record MP4, then convert to GIF).

Suggested recording settings:
- Duration: 15-25 seconds.
- Resolution: 1280x720.
- FPS: 10-15 (keeps GIF size manageable).
- Keep file under ~12 MB if possible.

## Demo Storyboard (Exact Sequence)

1. Launch ArchiveCord (token field visible, token redacted).
2. Paste token and click `Connect`.
3. In tree, check one DM and one server channel.
4. Set one simple filter (`After` date/time).
5. Click `Export & Process`.
6. Show batch label updating (`Exporting X of Y`) and progress bar.
7. Show preview pane updating.
8. Show completion status and output folder opened (or output path visible).

## Finalizing

1. Replace placeholder files in `docs/screens/` and `docs/gif/`.
2. Keep README paths unchanged, or update filenames if you rename files.
3. Verify rendering on GitHub PR page before merge.
