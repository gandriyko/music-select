#!/usr/bin/env python3
"""A keyboard-driven terminal browser and MP3 player.

Run from the directory you want to browse:
    python3 music_select.py
"""

from __future__ import annotations

import argparse
import curses
import json
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

EntryKind = Literal["parent", "directory", "file"]


@dataclass(frozen=True)
class Entry:
    path: Path
    kind: EntryKind

    @property
    def label(self) -> str:
        if self.kind == "parent":
            return "[..] Parent directory"
        if self.kind == "directory":
            return f"[DIR] {self.path.name}"
        return self.path.name


@dataclass(frozen=True)
class TrackInfo:
    artist: str = ""
    title: str = ""
    duration: float | None = None


class AudioPlayer:
    """Play MP3 files through ffplay."""

    def __init__(self) -> None:
        self.process: subprocess.Popen[bytes] | None = None
        self.track_info_cache: dict[Path, TrackInfo] = {}
        self.backend = "ffplay"
        if not shutil.which("ffplay"):
            raise RuntimeError(
                "ffplay is required. Install FFmpeg (macOS: brew install ffmpeg)."
            )

    def duration(self, path: Path) -> float | None:
        """Return an MP3 duration in seconds, caching the ffprobe result."""
        return self.track_info(path).duration

    def cached_track_info(self, path: Path) -> TrackInfo | None:
        """Return already-read track metadata without starting a probe."""
        return self.track_info_cache.get(path)

    def track_info(self, path: Path) -> TrackInfo:
        """Return duration and common MP3 tags, caching the ffprobe result."""
        if path in self.track_info_cache:
            return self.track_info_cache[path]
        if not shutil.which("ffprobe"):
            self.track_info_cache[path] = TrackInfo()
            return self.track_info_cache[path]
        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration:format_tags=artist,title",
                    "-of",
                    "json",
                    str(path),
                ],
                capture_output=True,
                check=True,
                text=True,
                timeout=10,
            )
            format_info = json.loads(result.stdout).get("format", {})
            duration = float(format_info.get("duration", -1))
            tags = {
                str(key).casefold(): str(value)
                for key, value in format_info.get("tags", {}).items()
            }
            self.track_info_cache[path] = TrackInfo(
                artist=tags.get("artist", ""),
                title=tags.get("title", ""),
                duration=duration if duration >= 0 else None,
            )
        except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
            self.track_info_cache[path] = TrackInfo()
        return self.track_info_cache[path]

    def play(self, path: Path, start: float) -> None:
        self.stop()
        self.process = subprocess.Popen(
            [
                "ffplay",
                "-nodisp",
                "-autoexit",
                "-loglevel",
                "error",
                "-ss",
                str(max(0.0, start)),
                str(path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def pause(self) -> None:
        if self.process is not None and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGSTOP)

    def resume(self) -> None:
        if self.process is not None and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGCONT)

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            # SIGKILL is needed if the user quits while the player is paused.
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()
        self.process = None

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def close(self) -> None:
        self.stop()


class MusicBrowser:
    SEEK_SECONDS = 15

    def __init__(self, start_dir: Path, player: AudioPlayer) -> None:
        self.player = player
        self.directory = start_dir.resolve()
        self.entries: list[Entry] = []
        self.selected = 0
        self.current_file: Path | None = None
        self.playing = False
        self.position_seconds = 0.0
        self.play_started_at = 0.0
        self.status = ""
        self.metadata_paths: list[Path] = []
        self.metadata_total = 0
        self.load_directory()

    def load_directory(self) -> None:
        self.metadata_paths = []
        self.metadata_total = 0
        try:
            children = list(self.directory.iterdir())
        except OSError as exc:
            self.entries = []
            self.status = f"Cannot read directory: {exc}"
            return

        directories = sorted(
            (
                path
                for path in children
                if path.is_dir() and not path.name.startswith(".")
            ),
            key=lambda path: path.name.casefold(),
        )
        mp3s = sorted(
            (
                path
                for path in children
                if path.is_file()
                and not path.name.startswith("._")  # macOS AppleDouble metadata sidecar
                and path.suffix.lower() == ".mp3"
            ),
            key=lambda path: path.name.casefold(),
        )
        parent = (
            [Entry(self.directory.parent, "parent")]
            if self.directory.parent != self.directory
            else []
        )
        self.entries = (
            parent
            + [Entry(path, "directory") for path in directories]
            + [Entry(path, "file") for path in mp3s]
        )
        self.metadata_paths = [
            path for path in mp3s if self.player.cached_track_info(path) is None
        ]
        self.metadata_total = len(self.metadata_paths)
        self.selected = min(self.selected, max(0, len(self.entries) - 1))
        self.status = f"{len(mp3s)} MP3 file(s) in this directory"

    @property
    def is_reading_metadata(self) -> bool:
        return bool(self.metadata_paths)

    def read_next_track_info(self) -> None:
        """Read metadata for one queued file so the UI can keep refreshing."""
        if self.metadata_paths:
            self.player.track_info(self.metadata_paths.pop(0))

    @property
    def metadata_progress(self) -> tuple[int, int]:
        return self.metadata_total - len(self.metadata_paths), self.metadata_total

    def current_position(self) -> float:
        if self.playing:
            return max(
                0.0, self.position_seconds + time.monotonic() - self.play_started_at
            )
        return self.position_seconds

    def play_file(self, path: Path, start: float = 0.0) -> None:
        try:
            self.player.play(path, start)
        except (OSError, RuntimeError) as exc:
            self.current_file = None
            self.playing = False
            self.position_seconds = 0.0
            self.status = f"Playback error: {exc}"
            return

        self.current_file = path
        self.position_seconds = max(0.0, start)
        self.play_started_at = time.monotonic()
        self.playing = True
        self.status = f"Playing: {path.name}"

    def stop(self) -> None:
        self.player.stop()
        self.current_file = None
        self.playing = False
        self.position_seconds = 0.0

    def toggle_pause(self) -> None:
        if self.current_file is None:
            self.status = "Select an MP3 file to start playback"
        elif self.playing:
            self.position_seconds = self.current_position()
            self.player.pause()
            self.playing = False
            self.status = "Paused"
        else:
            self.player.resume()
            self.play_started_at = time.monotonic()
            self.playing = True
            self.status = "Playing"

    def seek(self, amount: int) -> None:
        if self.current_file is None:
            self.status = "Select an MP3 file to seek"
            return
        target = max(0.0, self.current_position() + amount)
        duration = self.player.duration(self.current_file)
        if duration is not None and target >= duration:
            self.status = f"End of file ({duration:.0f}s)"
            return
        was_playing = self.playing
        self.play_file(self.current_file, target)
        if not was_playing and self.playing:
            self.player.pause()
            self.playing = False
            self.status = f"Paused at {target:.0f}s"

    def move(self, amount: int) -> None:
        if not self.entries:
            return
        self.selected = max(0, min(len(self.entries) - 1, self.selected + amount))
        entry = self.entries[self.selected]
        if entry.kind == "file" and entry.path != self.current_file:
            self.play_file(entry.path)

    def matching_files(self, query: str) -> list[Entry]:
        """Return MP3 entries matching filename, artist, or title, ignoring case."""
        normalized_query = query.casefold()
        matches: list[Entry] = []
        for entry in self.entries:
            if entry.kind != "file":
                continue
            info = self.player.cached_track_info(entry.path)
            searchable_text = " ".join(
                (
                    entry.path.name,
                    info.artist if info else "",
                    info.title if info else "",
                )
            ).casefold()
            if normalized_query in searchable_text:
                matches.append(entry)
        return matches

    def select_file(self, path: Path) -> None:
        """Make an existing MP3 entry selected and start it when necessary."""
        for index, entry in enumerate(self.entries):
            if entry.kind == "file" and entry.path == path:
                self.selected = index
                if entry.path != self.current_file:
                    self.play_file(entry.path)
                return

    def open_selected(self) -> None:
        if not self.entries:
            return
        entry = self.entries[self.selected]
        if entry.kind in {"parent", "directory"}:
            self.stop()
            self.directory = entry.path.resolve()
            self.selected = 0
            self.load_directory()
        elif entry.kind == "file" and entry.path != self.current_file:
            self.play_file(entry.path)

    def delete_selected(self) -> None:
        if not self.entries or self.entries[self.selected].kind != "file":
            self.status = "Only MP3 files can be deleted"
            return
        path = self.entries[self.selected].path
        try:
            if path == self.current_file:
                self.stop()
            path.unlink()
        except OSError as exc:
            self.status = f"Could not delete {path.name}: {exc}"
            return
        self.status = f"Deleted: {path.name}"
        self.load_directory()
        # Keep the same row index: it now points at the next MP3. If the last
        # MP3 was deleted, load_directory clamps it to the preceding MP3.
        if self.entries and self.entries[self.selected].kind == "file":
            self.play_file(self.entries[self.selected].path)

    def play_next_when_finished(self) -> None:
        """Advance after ffplay exits naturally, without looping at the end."""
        if not self.playing or self.player.is_running() or self.current_file is None:
            return

        current_index = next(
            (
                index
                for index, entry in enumerate(self.entries)
                if entry.kind == "file" and entry.path == self.current_file
            ),
            None,
        )
        if current_index is not None:
            for index in range(current_index + 1, len(self.entries)):
                if self.entries[index].kind == "file":
                    self.selected = index
                    self.play_file(self.entries[index].path)
                    return

        self.playing = False
        duration = self.player.duration(self.current_file)
        self.position_seconds = (
            duration if duration is not None else self.current_position()
        )
        self.status = "Finished: last MP3 in this directory"


def add_text(
    screen: curses.window, row: int, text: str, width: int, style: int = 0
) -> None:
    """Draw text without crashing if curses rejects the terminal's last cell."""
    try:
        screen.addnstr(row, 0, text, max(0, width - 1), style)
    except curses.error:
        # A resize between getmaxyx() and addnstr(), or a terminal's bottom-right
        # cell, may return ERR even when the visible text was drawn correctly.
        pass


def format_duration(seconds: float) -> str:
    """Format a playback position as minutes and seconds."""
    whole_seconds = max(0, int(seconds))
    minutes, seconds = divmod(whole_seconds, 60)
    return f"{minutes}:{seconds:02d}"


def progress_bar(position: float, duration: float | None, width: int) -> str:
    """Build a compact progress display for the active track."""
    if duration is None or duration <= 0:
        return ""

    clamped_position = min(max(0.0, position), duration)
    ratio = clamped_position / duration
    suffix = f" {ratio:.0%}"
    bar_width = max(1, width - len(suffix) - 2)
    filled = min(bar_width, int(ratio * bar_width))
    return f"[{'#' * filled}{'-' * (bar_width - filled)}]{suffix}"


def truncate_text(text: str, width: int) -> str:
    """Fit text inside a table cell, using an ellipsis when needed."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return "…"
    return f"{text[: width - 1]}…"


def table_column_widths(width: int) -> tuple[int, int, int, int]:
    """Divide the usable terminal width among the track-list columns."""
    available = max(1, width - 1)
    duration_width = min(8, max(4, available // 8))
    filename_width = max(8, available * 40 // 100)
    artist_width = max(7, available * 22 // 100)
    title_width = max(1, available - filename_width - artist_width - duration_width - 3)
    return filename_width, artist_width, title_width, duration_width


def table_row(
    entry: Entry, player: AudioPlayer, widths: tuple[int, int, int, int]
) -> str:
    """Format a browser entry as a filename, artist, title, and duration row."""
    filename_width, artist_width, title_width, duration_width = widths
    if entry.kind == "file":
        info = player.cached_track_info(entry.path)
        filename = entry.path.name
        artist = info.artist if info is not None else ""
        title = info.title if info is not None else ""
        duration = (
            format_duration(info.duration)
            if info is not None and info.duration is not None
            else "—"
        )
    else:
        filename = entry.label
        artist = ""
        title = ""
        duration = ""
    return (
        f"{truncate_text(filename, filename_width):<{filename_width}} "
        f"{truncate_text(artist, artist_width):<{artist_width}} "
        f"{truncate_text(title, title_width):<{title_width}} "
        f"{duration:>{duration_width}}"
    )


def draw(
    screen: curses.window,
    browser: MusicBrowser,
    search_query: str | None = None,
    search_selected: int = 0,
) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    add_text(screen, 0, f"Music Select — {browser.directory}", width, curses.A_BOLD)
    add_text(
        screen,
        1,
        "↑/↓ select & autoplay  Enter open folder/play  ←/→ seek 15s  "
        "Space pause  f find  d delete  q quit",
        width,
    )
    if browser.is_reading_metadata:
        completed, total = browser.metadata_progress
        spinner = "|/-\\"[completed % 4]
        add_text(
            screen,
            2,
            f"Reading metadata {spinner} {completed}/{total}",
            width,
            curses.A_BOLD,
        )
    else:
        try:
            screen.hline(2, 0, "-", max(0, width - 1))
        except curses.error:
            pass

    column_widths = table_column_widths(width)
    filename_width, artist_width, title_width, duration_width = column_widths
    header = (
        f"{'Filename':<{filename_width}} {'Artist':<{artist_width}} "
        f"{'Title':<{title_width}} {'Duration':>{duration_width}}"
    )
    add_text(screen, 3, header, width, curses.A_BOLD)
    try:
        screen.hline(4, 0, "-", max(0, width - 1))
    except curses.error:
        pass

    if search_query is None:
        displayed_entries = browser.entries
        displayed_selected = browser.selected
    else:
        displayed_entries = browser.matching_files(search_query)
        displayed_selected = min(search_selected, max(0, len(displayed_entries) - 1))

    visible_rows = max(1, height - 6)
    start = max(
        0,
        min(
            displayed_selected - visible_rows // 2,
            len(displayed_entries) - visible_rows,
        ),
    )
    for row, entry in enumerate(
        displayed_entries[start : start + visible_rows], start=5
    ):
        style = (
            curses.A_REVERSE
            if start + row - 5 == displayed_selected
            else curses.A_NORMAL
        )
        add_text(
            screen, row, table_row(entry, browser.player, column_widths), width, style
        )
    if search_query is not None and not displayed_entries:
        add_text(screen, 5, "No matching MP3 files", width)

    position = browser.current_position()
    now_playing = (
        browser.current_file.name if browser.current_file else "Nothing playing"
    )
    if search_query is not None:
        footer = f"Find file: {search_query}_  Up/Down choose  Enter play  Esc cancel"
    else:
        duration = (
            browser.player.duration(browser.current_file)
            if browser.current_file
            else None
        )
        total_time = (
            format_duration(duration) if duration is not None else "unknown duration"
        )
        time_display = f"{format_duration(position)} / {total_time}"
        if browser.status == f"Playing: {now_playing}":
            footer = f"{browser.status} | {time_display}"
        else:
            footer = f"{browser.status} | {now_playing} | {time_display}"
        if browser.current_file is not None:
            available_width = max(1, width - 1 - len(footer) - 1)
            bar = progress_bar(position, duration, available_width)
            if bar:
                footer += f" {bar}"
    add_text(screen, height - 1, footer, width, curses.A_REVERSE)
    screen.refresh()


def run(screen: curses.window, start_dir: Path) -> None:
    curses.curs_set(0)
    screen.keypad(True)
    screen.timeout(200)
    try:
        player = AudioPlayer()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    browser = MusicBrowser(start_dir, player)
    search_query: str | None = None
    search_selected = 0
    try:
        while True:
            browser.play_next_when_finished()
            draw(screen, browser, search_query, search_selected)
            screen.timeout(0 if browser.is_reading_metadata else 200)
            key = screen.getch()
            if key == -1:
                browser.read_next_track_info()
                continue
            if search_query is not None:
                if key == 27:
                    search_query = None
                    browser.status = "Search cancelled"
                elif key in (curses.KEY_ENTER, 10, 13):
                    matches = browser.matching_files(search_query)
                    if matches:
                        browser.select_file(matches[search_selected].path)
                    else:
                        browser.status = f"No MP3 found matching: {search_query}"
                    search_query = None
                elif key == curses.KEY_UP:
                    search_selected = max(0, search_selected - 1)
                elif key == curses.KEY_DOWN:
                    matches = browser.matching_files(search_query)
                    search_selected = min(max(0, len(matches) - 1), search_selected + 1)
                elif key in (curses.KEY_BACKSPACE, 8, 127):
                    search_query = search_query[:-1]
                    search_selected = 0
                elif 32 <= key <= 126:
                    search_query += chr(key)
                    search_selected = 0
                continue
            if key in (ord("q"), 27):
                return
            if key in (ord("f"), ord("F")):
                search_query = ""
                search_selected = 0
                continue
            if key == curses.KEY_UP:
                browser.move(-1)
            elif key == curses.KEY_DOWN:
                browser.move(1)
            elif key == curses.KEY_LEFT:
                browser.seek(-browser.SEEK_SECONDS)
            elif key == curses.KEY_RIGHT:
                browser.seek(browser.SEEK_SECONDS)
            elif key in (curses.KEY_ENTER, 10, 13):
                browser.open_selected()
            elif key == ord(" "):
                browser.toggle_pause()
            elif (
                key in (ord("d"), ord("D"))
                and browser.entries
                and browser.entries[browser.selected].kind == "file"
            ):
                browser.delete_selected()
    finally:
        player.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Browse and play MP3 files in a terminal."
    )
    parser.add_argument(
        "directory",
        nargs="?",
        type=Path,
        default=Path.cwd(),
        help="initial directory (default: current directory)",
    )
    args = parser.parse_args()
    if not args.directory.is_dir():
        raise SystemExit(f"Not a directory: {args.directory}")
    curses.wrapper(run, args.directory)


if __name__ == "__main__":
    main()
