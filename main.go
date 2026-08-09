package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"
	"unicode"

	"github.com/gdamore/tcell/v2"
	"github.com/mattn/go-runewidth"
)

const seekSeconds = 15 * time.Second

type entryKind int

const (
	parentEntry entryKind = iota
	directoryEntry
	fileEntry
)

type entry struct {
	path string
	kind entryKind
}

func (e entry) label() string {
	switch e.kind {
	case parentEntry:
		return "[..] Parent directory"
	case directoryEntry:
		return "[DIR] " + filepath.Base(e.path)
	default:
		return filepath.Base(e.path)
	}
}

type trackInfo struct {
	Artist   string
	Title    string
	Duration time.Duration
	Known    bool
}

type probeResult struct {
	path string
	info trackInfo
}

// player owns the ffplay process. ffplay stays an external dependency so the
// binary does not need to implement MP3 decoding or platform audio output.
type player struct {
	mu         sync.Mutex
	cmd        *exec.Cmd
	running    bool
	generation uint64
}

func newPlayer() (*player, error) {
	if _, err := exec.LookPath("ffplay"); err != nil {
		return nil, errors.New("ffplay is required. Install FFmpeg (macOS: brew install ffmpeg; Linux: install your distribution's ffmpeg package)")
	}
	return &player{}, nil
}

func (p *player) play(path string, offset time.Duration) error {
	p.stop()
	cmd := exec.Command("ffplay", "-nodisp", "-autoexit", "-loglevel", "error", "-ss", fmt.Sprintf("%.3f", offset.Seconds()), path)
	cmd.Stdin = nil
	cmd.Stdout = nil
	cmd.Stderr = nil
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := cmd.Start(); err != nil {
		return err
	}

	p.mu.Lock()
	p.cmd = cmd
	p.running = true
	p.generation++
	generation := p.generation
	p.mu.Unlock()

	go func() {
		_ = cmd.Wait()
		p.mu.Lock()
		defer p.mu.Unlock()
		if p.generation == generation && p.cmd == cmd {
			p.running = false
		}
	}()
	return nil
}

func (p *player) stop() {
	p.mu.Lock()
	cmd := p.cmd
	p.cmd = nil
	p.running = false
	p.generation++
	p.mu.Unlock()
	if cmd == nil || cmd.Process == nil {
		return
	}
	// ffplay is started in its own process group, so cleanup also reaches any
	// descendants it creates. The delayed SIGKILL prevents a stuck decoder from
	// surviving a track change or terminal exit.
	_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGTERM)
	go func(process *os.Process) {
		time.Sleep(500 * time.Millisecond)
		_ = process.Signal(syscall.SIGKILL)
	}(cmd.Process)
}

func (p *player) isRunning() bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.running
}

type browser struct {
	player        *player
	directory     string
	entries       []entry
	selected      int
	currentFile   string
	playing       bool
	position      time.Duration
	playStarted   time.Time
	status        string
	metadata      map[string]trackInfo
	pending       []string
	metadataTotal int
	probing       bool
	probeResults  chan probeResult
}

func newBrowser(directory string, player *player) *browser {
	b := &browser{player: player, metadata: make(map[string]trackInfo), probeResults: make(chan probeResult, 1)}
	b.directory = directory
	b.loadDirectory()
	return b
}

func (b *browser) loadDirectory() {
	children, err := os.ReadDir(b.directory)
	if err != nil {
		b.entries = nil
		b.status = "Cannot read directory: " + err.Error()
		return
	}
	var directories, mp3s []string
	for _, child := range children {
		name := child.Name()
		if strings.HasPrefix(name, ".") {
			continue
		}
		path := filepath.Join(b.directory, name)
		if child.IsDir() {
			directories = append(directories, path)
		} else if !strings.HasPrefix(name, "._") && strings.EqualFold(filepath.Ext(name), ".mp3") {
			mp3s = append(mp3s, path)
		}
	}
	sort.Slice(directories, func(i, j int) bool { return strings.ToLower(directories[i]) < strings.ToLower(directories[j]) })
	sort.Slice(mp3s, func(i, j int) bool { return strings.ToLower(mp3s[i]) < strings.ToLower(mp3s[j]) })
	b.entries = nil
	parent := filepath.Dir(b.directory)
	if parent != b.directory {
		b.entries = append(b.entries, entry{path: parent, kind: parentEntry})
	}
	for _, path := range directories {
		b.entries = append(b.entries, entry{path: path, kind: directoryEntry})
	}
	for _, path := range mp3s {
		b.entries = append(b.entries, entry{path: path, kind: fileEntry})
	}
	b.pending = b.pending[:0]
	for _, path := range mp3s {
		if _, known := b.metadata[path]; !known {
			b.pending = append(b.pending, path)
		}
	}
	b.metadataTotal = len(b.pending)
	if b.selected >= len(b.entries) {
		b.selected = max(0, len(b.entries)-1)
	}
	b.status = fmt.Sprintf("%d MP3 file(s) in this directory", len(mp3s))
}

func (b *browser) startProbe() {
	if b.probing || len(b.pending) == 0 {
		return
	}
	path := b.pending[0]
	b.pending = b.pending[1:]
	b.probing = true
	go func() { b.probeResults <- probeResult{path: path, info: probeTrack(path)} }()
}

func (b *browser) collectProbe() {
	select {
	case result := <-b.probeResults:
		b.metadata[result.path] = result.info
		b.probing = false
	default:
	}
}

func probeTrack(path string) trackInfo {
	if _, err := exec.LookPath("ffprobe"); err != nil {
		return trackInfo{}
	}
	cmd := exec.Command("ffprobe", "-v", "error", "-show_entries", "format=duration:format_tags=artist,title", "-of", "json", path)
	output, err := cmd.Output()
	if err != nil {
		return trackInfo{}
	}
	var decoded struct {
		Format struct {
			Duration string            `json:"duration"`
			Tags     map[string]string `json:"tags"`
		} `json:"format"`
	}
	if json.Unmarshal(output, &decoded) != nil {
		return trackInfo{}
	}
	var seconds float64
	if _, err := fmt.Sscan(decoded.Format.Duration, &seconds); err != nil || seconds < 0 {
		seconds = 0
	}
	info := trackInfo{Duration: time.Duration(seconds * float64(time.Second)), Known: seconds > 0}
	for key, value := range decoded.Format.Tags {
		switch strings.ToLower(key) {
		case "artist":
			info.Artist = value
		case "title":
			info.Title = value
		}
	}
	return info
}

func (b *browser) currentPosition() time.Duration {
	if b.playing {
		return b.position + time.Since(b.playStarted)
	}
	return b.position
}

func (b *browser) playFile(path string, start time.Duration) {
	if err := b.player.play(path, start); err != nil {
		b.currentFile, b.playing, b.position = "", false, 0
		b.status = "Playback error: " + err.Error()
		return
	}
	b.currentFile, b.position, b.playStarted, b.playing = path, maxDuration(0, start), time.Now(), true
	b.status = "Playing: " + filepath.Base(path)
}

func (b *browser) stop() { b.player.stop(); b.currentFile, b.playing, b.position = "", false, 0 }

func (b *browser) togglePause() {
	if b.currentFile == "" {
		b.status = "Select an MP3 file to start playback"
		return
	}
	if b.playing {
		b.position = b.currentPosition()
		b.player.stop()
		b.playing = false
		b.status = "Paused"
		return
	}
	b.playFile(b.currentFile, b.position)
}

func (b *browser) seek(amount time.Duration) {
	if b.currentFile == "" {
		b.status = "Select an MP3 file to seek"
		return
	}
	target := maxDuration(0, b.currentPosition()+amount)
	if info, ok := b.metadata[b.currentFile]; ok && info.Known && target >= info.Duration {
		b.status = fmt.Sprintf("End of file (%.0fs)", info.Duration.Seconds())
		return
	}
	if b.playing {
		b.playFile(b.currentFile, target)
	} else {
		b.position, b.status = target, fmt.Sprintf("Paused at %.0fs", target.Seconds())
	}
}

func (b *browser) move(amount int) {
	if len(b.entries) == 0 {
		return
	}
	b.selected = min(len(b.entries)-1, max(0, b.selected+amount))
	e := b.entries[b.selected]
	if e.kind == fileEntry && e.path != b.currentFile {
		b.playFile(e.path, 0)
	}
}

func (b *browser) matchingFiles(query string) []entry {
	query = strings.ToLower(query)
	var matches []entry
	for _, e := range b.entries {
		if e.kind != fileEntry {
			continue
		}
		info := b.metadata[e.path]
		if strings.Contains(strings.ToLower(strings.Join([]string{filepath.Base(e.path), info.Artist, info.Title}, " ")), query) {
			matches = append(matches, e)
		}
	}
	return matches
}

func (b *browser) selectFile(path string) {
	for index, e := range b.entries {
		if e.kind == fileEntry && e.path == path {
			b.selected = index
			if path != b.currentFile {
				b.playFile(path, 0)
			}
			return
		}
	}
}

func (b *browser) openSelected() {
	if len(b.entries) == 0 {
		return
	}
	e := b.entries[b.selected]
	if e.kind == parentEntry || e.kind == directoryEntry {
		b.stop()
		b.directory, _ = filepath.Abs(e.path)
		b.selected = 0
		b.loadDirectory()
	} else if e.path != b.currentFile {
		b.playFile(e.path, 0)
	}
}

func (b *browser) deleteSelected() {
	if len(b.entries) == 0 || b.entries[b.selected].kind != fileEntry {
		b.status = "Only MP3 files can be deleted"
		return
	}
	path := b.entries[b.selected].path
	if path == b.currentFile {
		b.stop()
	}
	if err := os.Remove(path); err != nil {
		b.status = "Could not delete " + filepath.Base(path) + ": " + err.Error()
		return
	}
	b.status = "Deleted: " + filepath.Base(path)
	b.loadDirectory()
	if len(b.entries) > 0 && b.entries[b.selected].kind == fileEntry {
		b.playFile(b.entries[b.selected].path, 0)
	}
}

func (b *browser) advanceIfFinished() {
	if !b.playing || b.player.isRunning() || b.currentFile == "" {
		return
	}
	current := b.currentFile
	for index, e := range b.entries {
		if e.kind == fileEntry && e.path == current {
			for _, next := range b.entries[index+1:] {
				if next.kind == fileEntry {
					b.selected = index + 1
					for b.entries[b.selected].kind != fileEntry {
						b.selected++
					}
					b.playFile(b.entries[b.selected].path, 0)
					return
				}
			}
			break
		}
	}
	b.playing = false
	if info, ok := b.metadata[current]; ok && info.Known {
		b.position = info.Duration
	} else {
		b.position = b.currentPosition()
	}
	b.status = "Finished: last MP3 in this directory"
}

type app struct {
	screen  tcell.Screen
	browser *browser
	theme   string
	search  *searchState
}
type searchState struct {
	query    string
	selected int
}

func (a *app) run() {
	defer a.browser.stop()
	events := make(chan tcell.Event, 8)
	go func() {
		for {
			events <- a.screen.PollEvent()
		}
	}()
	ticker := time.NewTicker(100 * time.Millisecond)
	defer ticker.Stop()
	for {
		a.browser.collectProbe()
		a.browser.startProbe()
		a.browser.advanceIfFinished()
		a.draw()
		select {
		case event := <-events:
			if a.handleEvent(event) {
				return
			}
		case <-ticker.C:
		}
	}
}

func (a *app) handleEvent(event tcell.Event) bool {
	switch e := event.(type) {
	case *tcell.EventResize:
		a.screen.Sync()
		return false
	case *tcell.EventKey:
		if a.search != nil {
			a.handleSearch(e)
			return false
		}
		switch e.Key() {
		case tcell.KeyEscape:
			return true
		case tcell.KeyUp:
			a.browser.move(-1)
		case tcell.KeyDown:
			a.browser.move(1)
		case tcell.KeyLeft:
			a.browser.seek(-seekSeconds)
		case tcell.KeyRight:
			a.browser.seek(seekSeconds)
		case tcell.KeyEnter:
			a.browser.openSelected()
		default:
			switch unicode.ToLower(e.Rune()) {
			case 'q':
				return true
			case 'f':
				a.search = &searchState{}
			case ' ':
				a.browser.togglePause()
			case 'd':
				a.browser.deleteSelected()
			}
		}
	}
	return false
}

func (a *app) handleSearch(event *tcell.EventKey) {
	s := a.search
	switch event.Key() {
	case tcell.KeyEscape:
		a.search = nil
		a.browser.status = "Search cancelled"
	case tcell.KeyEnter:
		matches := a.browser.matchingFiles(s.query)
		if len(matches) > 0 {
			a.browser.selectFile(matches[min(s.selected, len(matches)-1)].path)
		} else {
			a.browser.status = "No MP3 found matching: " + s.query
		}
		a.search = nil
	case tcell.KeyUp:
		s.selected = max(0, s.selected-1)
	case tcell.KeyDown:
		s.selected = min(max(0, len(a.browser.matchingFiles(s.query))-1), s.selected+1)
	case tcell.KeyBackspace, tcell.KeyBackspace2:
		if len(s.query) > 0 {
			s.query = string([]rune(s.query)[:len([]rune(s.query))-1])
			s.selected = 0
		}
	default:
		if event.Rune() >= ' ' {
			s.query += string(event.Rune())
			s.selected = 0
		}
	}
}

func (a *app) draw() {
	s := a.screen
	s.Clear()
	width, height := s.Size()
	usable := max(0, width-1)
	normal, bold, reverse := tcell.StyleDefault, tcell.StyleDefault.Bold(true), tcell.StyleDefault.Reverse(true)
	if a.theme == "white" {
		normal = normal.Foreground(tcell.ColorBlack).Background(tcell.ColorWhite)
		bold = normal.Bold(true)
		reverse = normal.Reverse(true)
	}
	if a.theme == "black" {
		normal = normal.Foreground(tcell.ColorWhite).Background(tcell.ColorBlack)
		bold = normal.Bold(true)
		reverse = normal.Reverse(true)
	}
	putText(s, 0, 0, "Music Select — "+a.browser.directory, usable, bold)
	putText(s, 0, 1, "↑/↓ select & autoplay  Enter open folder/play  ←/→ seek 15s  Space pause  f find  d delete  q quit", usable, normal)
	if a.browser.probing || len(a.browser.pending) > 0 {
		done := a.browser.metadataTotal - len(a.browser.pending)
		spinner := []string{"|", "/", "-", "\\"}[done%4]
		putText(s, 0, 2, fmt.Sprintf("Reading metadata %s %d/%d", spinner, done, a.browser.metadataTotal), usable, bold)
	} else {
		putText(s, 0, 2, strings.Repeat("-", usable), usable, normal)
	}
	filename, artist, title, duration := columnWidths(width)
	header := fmt.Sprintf("%-*s %-*s %-*s %*s", filename, "Filename", artist, "Artist", title, "Title", duration, "Duration")
	putText(s, 0, 3, header, usable, bold)
	putText(s, 0, 4, strings.Repeat("-", usable), usable, normal)
	entries, selected := a.browser.entries, a.browser.selected
	if a.search != nil {
		entries = a.browser.matchingFiles(a.search.query)
		selected = min(a.search.selected, max(0, len(entries)-1))
	}
	visible := max(1, height-6)
	start := min(max(0, selected-visible/2), max(0, len(entries)-visible))
	for row, e := range entries[start:min(len(entries), start+visible)] {
		style := normal
		if start+row == selected {
			style = reverse
		}
		putText(s, 0, 5+row, a.tableRow(e, filename, artist, title, duration), usable, style)
	}
	if a.search != nil && len(entries) == 0 {
		putText(s, 0, 5, "No matching MP3 files", usable, normal)
	}
	footer := a.footer(width)
	putText(s, 0, max(0, height-1), footer, usable, reverse)
	s.Show()
}

func (a *app) tableRow(e entry, filename, artist, title, duration int) string {
	name, performer, track, length := e.label(), "", "", ""
	if e.kind == fileEntry {
		if info, ok := a.browser.metadata[e.path]; ok {
			performer, track = info.Artist, info.Title
			if info.Known {
				length = formatDuration(info.Duration)
			} else {
				length = "—"
			}
		} else {
			length = "—"
		}
	}
	return pad(truncate(name, filename), filename, false) + " " + pad(truncate(performer, artist), artist, false) + " " + pad(truncate(track, title), title, false) + " " + pad(length, duration, true)
}

func (a *app) footer(width int) string {
	if a.search != nil {
		return "Find file: " + a.search.query + "_  Up/Down choose  Enter play  Esc cancel"
	}
	b := a.browser
	now := "Nothing playing"
	if b.currentFile != "" {
		now = filepath.Base(b.currentFile)
	}
	duration := time.Duration(0)
	known := false
	if info, ok := b.metadata[b.currentFile]; ok {
		duration, known = info.Duration, info.Known
	}
	total := "unknown duration"
	if known {
		total = formatDuration(duration)
	}
	footer := fmt.Sprintf("%s | %s | %s / %s", b.status, now, formatDuration(b.currentPosition()), total)
	if b.currentFile != "" && known {
		if bar := progressBar(b.currentPosition(), duration, max(1, width-1-runewidth.StringWidth(footer)-1)); bar != "" {
			footer += " " + bar
		}
	}
	return footer
}

func putText(screen tcell.Screen, x, y int, text string, width int, style tcell.Style) {
	if y < 0 || width <= 0 {
		return
	}
	text = truncate(text, width)
	column := x
	runes := []rune(text)
	for index := 0; index < len(runes); {
		r := runes[index]
		w := runewidth.RuneWidth(r)
		if w == 0 {
			index++
			continue
		}
		if column+w > x+width {
			break
		}
		index++
		var combining []rune
		for index < len(runes) && runewidth.RuneWidth(runes[index]) == 0 {
			combining = append(combining, runes[index])
			index++
		}
		screen.SetContent(column, y, r, combining, style)
		column += w
	}
}
func truncate(text string, width int) string {
	if width <= 0 {
		return ""
	}
	if runewidth.StringWidth(text) <= width {
		return text
	}
	if width == 1 {
		return "…"
	}
	var out strings.Builder
	used := 0
	for _, r := range text {
		w := runewidth.RuneWidth(r)
		if used+w > width-1 {
			break
		}
		out.WriteRune(r)
		used += w
	}
	return out.String() + "…"
}
func pad(text string, width int, right bool) string {
	spaces := strings.Repeat(" ", max(0, width-runewidth.StringWidth(text)))
	if right {
		return spaces + text
	}
	return text + spaces
}
func columnWidths(width int) (int, int, int, int) {
	available := max(1, width-1)
	duration := min(8, max(4, available/8))
	filename := max(8, available*40/100)
	artist := max(7, available*22/100)
	title := max(1, available-filename-artist-duration-3)
	return filename, artist, title, duration
}
func formatDuration(value time.Duration) string {
	seconds := max(0, int(value.Seconds()))
	return fmt.Sprintf("%d:%02d", seconds/60, seconds%60)
}
func progressBar(position, duration time.Duration, width int) string {
	if duration <= 0 {
		return ""
	}
	ratio := float64(position) / float64(duration)
	if ratio < 0 {
		ratio = 0
	}
	if ratio > 1 {
		ratio = 1
	}
	suffix := fmt.Sprintf(" %.0f%%", ratio*100)
	bar := max(1, width-runewidth.StringWidth(suffix)-2)
	filled := min(bar, int(ratio*float64(bar)))
	return "[" + strings.Repeat("#", filled) + strings.Repeat("-", bar-filled) + "]" + suffix
}
func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
func max(a, b int) int {
	if a > b {
		return a
	}
	return b
}
func maxDuration(a, b time.Duration) time.Duration {
	if a > b {
		return a
	}
	return b
}

func main() {
	directory, theme, err := parseArgs(os.Args[1:])
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		fmt.Fprintln(os.Stderr, "Usage: music-select [--theme white|black] [directory]")
		os.Exit(2)
	}
	absolute, err := filepath.Abs(directory)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	if info, err := os.Stat(absolute); err != nil || !info.IsDir() {
		fmt.Fprintf(os.Stderr, "Not a directory: %s\n", directory)
		os.Exit(1)
	}
	player, err := newPlayer()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	screen, err := tcell.NewScreen()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	if err := screen.Init(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	defer screen.Fini()
	(&app{screen: screen, browser: newBrowser(absolute, player), theme: theme}).run()
}

func parseArgs(args []string) (string, string, error) {
	directory, theme := ".", ""
	hasDirectory := false
	for index := 0; index < len(args); index++ {
		argument := args[index]
		switch {
		case argument == "-h" || argument == "--help":
			return "", "", errors.New("help requested")
		case argument == "--theme":
			index++
			if index == len(args) {
				return "", "", errors.New("--theme requires white or black")
			}
			theme = args[index]
		case strings.HasPrefix(argument, "--theme="):
			theme = strings.TrimPrefix(argument, "--theme=")
		case strings.HasPrefix(argument, "-"):
			return "", "", fmt.Errorf("unknown option: %s", argument)
		case hasDirectory:
			return "", "", errors.New("only one directory may be supplied")
		default:
			directory = argument
			hasDirectory = true
		}
	}
	if theme != "" && theme != "white" && theme != "black" {
		return "", "", errors.New("--theme must be white or black")
	}
	return directory, theme, nil
}
