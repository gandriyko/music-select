#!/usr/bin/env python3
"""A terminal browser and MP3 player.

Run from the directory you want to browse:
    ./mp3s.py
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
import unicodedata
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

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            # Escalate if ffplay does not exit promptly during cleanup.
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
        self.continuous_playback = True
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
            # Suspending ffplay interrupts its audio buffers, which is
            # especially noticeable when the file is on removable storage.
            # Stop cleanly and reopen at this position when playback resumes.
            self.player.stop()
            self.playing = False
            self.status = "Paused"
        else:
            self.play_file(self.current_file, self.position_seconds)

    def toggle_continuous_playback(self) -> None:
        self.continuous_playback = not self.continuous_playback
        state = "ON" if self.continuous_playback else "OFF"
        self.status = f"Continuous playback: {state}"

    def seek(self, amount: int) -> None:
        if self.current_file is None:
            self.status = "Select an MP3 file to seek"
            return
        target = max(0.0, self.current_position() + amount)
        duration = self.player.duration(self.current_file)
        if duration is not None and target >= duration:
            self.status = f"End of file ({duration:.0f}s)"
            return
        if self.playing:
            self.play_file(self.current_file, target)
        else:
            self.position_seconds = target
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
        """Advance after ffplay exits naturally when continuous play is on."""
        if not self.playing or self.player.is_running() or self.current_file is None:
            return

        if self.continuous_playback:
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
            for index, entry in enumerate(self.entries):
                if entry.kind == "file":
                    self.selected = index
                    self.play_file(entry.path)
                    return

        self.playing = False
        duration = self.player.duration(self.current_file)
        self.position_seconds = (
            duration if duration is not None else self.current_position()
        )
        if self.continuous_playback:
            self.status = "Finished: no MP3 files in this directory"
        else:
            self.status = f"Finished: {self.current_file.name}"


def add_text(
    screen: curses.window, row: int, text: str, width: int, style: int = 0
) -> None:
    """Draw text without crashing if curses rejects the terminal's last cell."""
    try:
        # addnstr limits Python characters, not terminal cells.  A combining
        # accent therefore consumed its limit without taking screen space and
        # could clip the final character in a row (for example, "3:10").
        screen.addstr(row, 0, truncate_text(text, max(0, width - 1)), style)
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
    if display_width(text) <= width:
        return text
    if width == 1:
        return "…"
    truncated: list[str] = []
    used_width = 0
    for character in text:
        character_width = display_width(character)
        if used_width + character_width > width - 1:
            break
        truncated.append(character)
        used_width += character_width
    return f"{''.join(truncated)}…"


def display_width(text: str) -> int:
    """Return the number of terminal columns occupied by text."""
    return sum(
        0
        if unicodedata.combining(character)
        else 2
        if unicodedata.east_asian_width(character) in {"W", "F"}
        else 1
        for character in text
    )


def pad_text(text: str, width: int, *, align_right: bool = False) -> str:
    """Pad text to a terminal-column width rather than a character count."""
    padding = " " * max(0, width - display_width(text))
    return f"{padding}{text}" if align_right else f"{text}{padding}"


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
        f"{pad_text(truncate_text(filename, filename_width), filename_width)} "
        f"{pad_text(truncate_text(artist, artist_width), artist_width)} "
        f"{pad_text(truncate_text(title, title_width), title_width)} "
        f"{pad_text(duration, duration_width, align_right=True)}"
    )


def list_view_start(
    entry_count: int, selected: int, height: int, requested_start: int | None = None
) -> int:
    """Return a valid first entry index for the visible list rows."""
    visible_rows = max(1, height - 6)
    if requested_start is None:
        requested_start = selected - visible_rows // 2
    return max(0, min(requested_start, entry_count - visible_rows))


def draw(
    screen: curses.window,
    browser: MusicBrowser,
    search_query: str | None = None,
    search_selected: int = 0,
    list_start: int | None = None,
) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    add_text(screen, 0, f"Music Select — {browser.directory}", width, curses.A_BOLD)
    add_text(
        screen,
        1,
        f"c continuous:{'ON' if browser.continuous_playback else 'OFF'}  "
        "↑/↓ select & autoplay  Enter open folder/play  ←/→ seek 15s  "
        "Space/right-click pause  f find  dd delete  q quit",
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
    start = list_view_start(
        len(displayed_entries), displayed_selected, height, list_start
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
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    screen.timeout(200)
    wheel_up = getattr(curses, "BUTTON4_PRESSED", 0)
    wheel_down = getattr(curses, "BUTTON5_PRESSED", 0)
    # On the macOS curses build, button five is not exposed. Its event bits
    # overlap these modifier constants instead.
    wheel_down_fallback = curses.BUTTON_CTRL | curses.BUTTON_SHIFT | curses.BUTTON_ALT
    try:
        player = AudioPlayer()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    browser = MusicBrowser(start_dir, player)
    search_query: str | None = None
    search_selected = 0
    list_start: int | None = None
    delete_pending_path: Path | None = None
    try:
        while True:
            browser.play_next_when_finished()
            draw(screen, browser, search_query, search_selected, list_start)
            screen.timeout(0 if browser.is_reading_metadata else 200)
            key = screen.getch()
            if key == -1:
                browser.read_next_track_info()
                continue
            if key not in (ord("d"), ord("D")):
                delete_pending_path = None
            if key == curses.KEY_MOUSE:
                try:
                    _, mouse_x, mouse_y, _, mouse_state = curses.getmouse()
                except curses.error:
                    # Apple's four-button ncurses recognizes an xterm
                    # wheel-down sequence as KEY_MOUSE but cannot represent
                    # button five in MEVENT, so getmouse() returns ERR.
                    # Preserve it as the otherwise-unused zero-state fallback.
                    mouse_x = mouse_y = -1
                    mouse_state = 0

                left_click = bool(
                    mouse_state
                    & (curses.BUTTON1_CLICKED | curses.BUTTON1_DOUBLE_CLICKED)
                )
                right_click = bool(
                    mouse_state
                    & (curses.BUTTON3_CLICKED | curses.BUTTON3_DOUBLE_CLICKED)
                )
                if right_click:
                    browser.toggle_pause()
                    continue
                # Give a click priority over wheel detection.  Some curses
                # builds expose no distinct BUTTON5_PRESSED mask, so their
                # fallback wheel state must never make a table click scroll.
                wheel_scrolled_up = not left_click and bool(
                    wheel_up and mouse_state & wheel_up
                )
                wheel_scrolled_down = not left_click and bool(
                    wheel_down and mouse_state & wheel_down
                )
                if not left_click and not wheel_down:
                    # This curses build reserves only four button groups. Its
                    # fifth-button wheel mask aliases modifier masks. Include
                    # every aliased event kind: different terminals report a
                    # wheel-down action as pressed, released, or clicked.
                    wheel_scrolled_down = (
                        bool(mouse_state & wheel_down_fallback) or mouse_state == 0
                    )
                if wheel_scrolled_up or wheel_scrolled_down:
                    if search_query is None:
                        displayed_entries = browser.entries
                        displayed_selected = browser.selected
                    else:
                        displayed_entries = browser.matching_files(search_query)
                        displayed_selected = search_selected
                    height, _ = screen.getmaxyx()
                    current_start = list_view_start(
                        len(displayed_entries), displayed_selected, height, list_start
                    )
                    direction = -1 if wheel_scrolled_up else 1
                    list_start = max(
                        0,
                        min(
                            current_start + direction * 3,
                            len(displayed_entries) - max(1, height - 6),
                        ),
                    )
                    continue
                if not left_click:
                    continue

                height, width = screen.getmaxyx()
                if mouse_x < 0 or mouse_x >= width or not 5 <= mouse_y < height - 1:
                    continue
                if search_query is None:
                    displayed_entries = browser.entries
                    displayed_selected = browser.selected
                else:
                    displayed_entries = browser.matching_files(search_query)
                    displayed_selected = min(
                        search_selected, max(0, len(displayed_entries) - 1)
                    )
                start = list_view_start(
                    len(displayed_entries), displayed_selected, height, list_start
                )
                clicked_index = start + mouse_y - 5
                if clicked_index >= len(displayed_entries):
                    continue
                clicked_entry = displayed_entries[clicked_index]
                if search_query is None:
                    browser.selected = clicked_index
                    browser.open_selected()
                    if clicked_entry.kind in {"parent", "directory"}:
                        list_start = 0
                else:
                    browser.select_file(clicked_entry.path)
                    search_query = None
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
                    list_start = None
                elif key == curses.KEY_DOWN:
                    matches = browser.matching_files(search_query)
                    search_selected = min(max(0, len(matches) - 1), search_selected + 1)
                    list_start = None
                elif key in (curses.KEY_BACKSPACE, 8, 127):
                    search_query = search_query[:-1]
                    search_selected = 0
                    list_start = None
                elif 32 <= key <= 126:
                    search_query += chr(key)
                    search_selected = 0
                    list_start = None
                continue
            if key in (ord("q"), 27):
                return
            if key in (ord("f"), ord("F")):
                search_query = ""
                search_selected = 0
                list_start = None
                continue
            if key == curses.KEY_UP:
                browser.move(-1)
                list_start = None
            elif key == curses.KEY_DOWN:
                browser.move(1)
                list_start = None
            elif key == curses.KEY_LEFT:
                browser.seek(-browser.SEEK_SECONDS)
            elif key == curses.KEY_RIGHT:
                browser.seek(browser.SEEK_SECONDS)
            elif key in (curses.KEY_ENTER, 10, 13):
                browser.open_selected()
            elif key == ord(" "):
                browser.toggle_pause()
            elif key in (ord("c"), ord("C")):
                browser.toggle_continuous_playback()
            elif (
                key in (ord("d"), ord("D"))
                and browser.entries
                and browser.entries[browser.selected].kind == "file"
            ):
                selected_path = browser.entries[browser.selected].path
                if delete_pending_path == selected_path:
                    browser.delete_selected()
                    delete_pending_path = None
                else:
                    delete_pending_path = selected_path
                    browser.status = f"Press d again to delete: {selected_path.name}"
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
