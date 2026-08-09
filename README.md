# Music Select

A simple terminal MP3 browser and player. It starts in the current directory and shows child directories and `.mp3` files. The `[..] Parent directory` entry lets you go up one level.

AppleDouble metadata files created by macOS, such as `._song.mp3`, are ignored.

## Install and run

Download a release binary for your platform, make it executable, and run it:

```bash
chmod +x music-select
./music-select
```

To build it from source, install Go and run:

```bash
go build -o music-select .
./music-select
```

Music Select requires `ffplay` and `ffprobe`, supplied by FFmpeg. Install
FFmpeg on macOS:

```bash
brew install ffmpeg
```

On common Linux distributions:

```bash
# Debian / Ubuntu
sudo apt install ffmpeg

# Fedora (with RPM Fusion configured)
sudo dnf install ffmpeg

# Arch Linux
sudo pacman -S ffmpeg
```

The file list is displayed as a table with filename, artist, title, and
duration columns. Artist and title are read from the MP3 tags by `ffprobe`;
untagged files leave those columns empty. A spinner and count are shown while
metadata for the current folder is being read.

To make `music-select` available in your shell after an update, copy the
binary to a directory on your `PATH`:

```bash
mkdir -p ~/.local/bin
cp music-select ~/.local/bin/music-select
chmod +x ~/.local/bin/music-select
```

You can start in a specific directory instead:

```bash
./music-select /path/to/music
```

## Controls

| Key | Action |
| --- | --- |
| Up / Down | Move selection; selecting an MP3 starts it from the beginning |
| Enter | Open the selected directory, go to the parent directory, or play the selected MP3 from the beginning |
| Left / Right | Seek backward / forward 15 seconds; forward seeking stops at the end of the file |
| Space | Pause or resume playback |
| f | Filter MP3s by filename, artist, or title; type a query, use Up / Down to choose a match, then press Enter to play it |
| d | Delete the selected MP3 immediately, then select and play the next file (or previous file if it was last) |
| q or Esc | Quit |

The footer shows the currently selected track's elapsed time, total duration,
and a live progress bar. If FFmpeg cannot determine a file's duration, the
elapsed time is shown and the total is reported as unknown.

Set the application background explicitly with `--theme white` (white
background with black text) or `--theme black` (black background with white
text). Without `--theme`, Music Select keeps the terminal's configured colours.

```bash
./music-select --theme white
./music-select /path/to/music --theme black
```

When a track finishes, the next MP3 in the current directory is selected and
played automatically. Playback stops after the final MP3; it does not loop back
to the first file. Only direct `.mp3` files in the displayed directory are
listed; use the directory entries to browse subdirectories or the parent directory.
