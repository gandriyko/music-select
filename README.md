# Music Select

A simple terminal MP3 browser and player. It starts in the current directory and shows child directories and `.mp3` files. The `[..] Parent directory` entry lets you go up one level.

AppleDouble metadata files created by macOS, such as `._song.mp3`, are ignored.

## Install and run

```bash
python3 music_select.py
```

Music Select requires `ffplay`, supplied by FFmpeg. Install FFmpeg on macOS:

```bash
brew install ffmpeg
```

The file list is displayed as a table with filename, artist, title, and
duration columns. Artist and title are read from the MP3 tags by `ffprobe`;
untagged files leave those columns empty. A spinner and count are shown while
metadata for the current folder is being read.

To make the `music_select` command use the latest project version after an
update, copy the script to your local bin directory:

```bash
cp music_select.py ~/.local/bin/music_select
chmod +x ~/.local/bin/music_select
```

You can start in a specific directory instead:

```bash
python3 music_select.py /path/to/music
```

## Controls

| Key | Action |
| --- | --- |
| Up / Down | Move selection; selecting an MP3 starts it at 15 seconds |
| Enter | Open the selected directory, go to the parent directory, or play the selected MP3 at 15 seconds |
| Left / Right | Seek backward / forward 15 seconds; forward seeking stops at the end of the file |
| Space | Pause or resume playback |
| f | Filter MP3s by filename, artist, or title; type a query, use Up / Down to choose a match, then press Enter to play it |
| d | Delete the selected MP3 immediately, then select and play the next file (or previous file if it was last) |
| q or Esc | Quit |

The footer shows the currently selected track's elapsed time, total duration,
and a live progress bar. If FFmpeg cannot determine a file's duration, the
elapsed time is shown and the total is reported as unknown.

When a track finishes, the next MP3 in the current directory is selected and
played automatically. Playback stops after the final MP3; it does not loop back
to the first file. Only direct `.mp3` files in the displayed directory are
listed; use the directory entries to browse subdirectories or the parent directory.
