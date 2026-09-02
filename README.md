# Music Select

A simple terminal MP3 browser and player. It starts in the current directory and shows child directories and `.mp3` files. The `[..] Parent directory` entry lets you go up one level.

AppleDouble metadata files created by macOS, such as `._song.mp3`, are ignored.

## Install and run

```bash
./mp3s.py
```

Music Select requires `ffplay`, supplied by FFmpeg. Install FFmpeg on macOS:

```bash
brew install ffmpeg
```

The file list is displayed as a table with filename, artist, title, and
duration columns. Artist and title are read from the MP3 tags by `ffprobe`;
untagged files leave those columns empty. A spinner and count are shown while
metadata for the current folder is being read.

To make the `mp3s` command use the latest project version after an
update, copy the script to your local bin directory:

```bash
cp mp3s.py ~/.local/bin/mp3s
chmod +x ~/.local/bin/mp3s
```

You can start in a specific directory instead:

```bash
./mp3s.py /path/to/music
```

## Controls

| Input | Action |
| --- | --- |
| Up / Down | Move selection; selecting an MP3 starts it from the beginning |
| Enter | Open the selected directory, go to the parent directory, or play the selected MP3 from the beginning |
| Left / Right | Seek backward / forward 15 seconds; forward seeking stops at the end of the file |
| Space | Pause or resume playback |
| Right click | Pause or resume playback without changing the selection |
| c | Turn continuous playback on or off |
| f | Filter MP3s by filename, artist, or title; type a query, use Up / Down to choose a match, then press Enter to play it |
| d then d | Delete the selected MP3, then select and play the next file (or previous file if it was last) |
| q or Esc | Quit |
| Left click | Open a directory, navigate to the parent directory, or play an MP3 |
| Mouse wheel | Scroll the file list without changing the selection or playback |

The footer shows the currently selected track's elapsed time, total duration,
and a live progress bar. If FFmpeg cannot determine a file's duration, the
elapsed time is shown and the total is reported as unknown.

Continuous playback is on by default, and its current state is shown in the top
panel. Press `c` to toggle it. When it is on and a track finishes, the next MP3
in the current directory is selected and played automatically. When it is off,
playback stops after the current track. In continuous mode, finishing the final
MP3 starts the first MP3 again, so playback continues in a loop. Only direct
`.mp3` files in the displayed directory are listed; use the directory entries
to browse subdirectories or the parent directory.
