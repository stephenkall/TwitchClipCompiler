# TwitchClipCompiler

Downloads a list of Twitch clips and compiles them into a single video, sorted chronologically, with smooth fade transitions and a title/date overlay at the start of each clip.

## Requirements

- Python 3.10+
- [ffmpeg](https://ffmpeg.org/download.html) (must be in `PATH`)

> All Python dependencies are installed automatically on first run.

## Installation

```bash
git clone https://github.com/stephenkall/TwitchClipCompiler.git
cd TwitchClipCompiler
```

No manual `pip install` needed — the script installs its own dependencies.

## Usage

1. Add one Twitch clip URL per line to `videolist.txt`
2. Run:

```bash
python compiler.py
```

The output file `compilation.mp4` will be created in the current directory.

### Options

| Flag | Description |
|------|-------------|
| `--input FILE` | URL list file (default: `videolist.txt`) |
| `--output FILE` | Output video path (default: `compilation.mp4`) |
| `--clips-dir DIR` | Directory for downloaded/processed clips (default: `clips/`) |
| `--no-download` | Skip download phase (use already-downloaded clips) |
| `--no-process` | Skip processing phase (use already-processed clips) |
| `--no-cache` | Reprocess clips even if output already exists |
| `--clean-cache` | Delete `clips/processed/` before starting |
| `--workers N`, `-j N` | Parallel workers for the processing phase (default: 75% of CPU cores) |
| `--quiet`, `-q` | Suppress progress bars and log output |

### Examples

```bash
# Full run
python compiler.py

# Compile from a different URL list into a custom output file
python compiler.py --input my_clips.txt --output highlights.mp4

# Re-download and reprocess everything from scratch
python compiler.py --clean-cache

# Skip download (clips already in clips/raw/) and use 4 workers
python compiler.py --no-download --workers 4
```

## How it works

```
videolist.txt
     │
     ▼
[Phase 1] Download          yt-dlp fetches each clip + metadata (cached)
     │
     ▼
[Sort]                      Clips ordered oldest → newest by upload date
     │
     ▼
[Phase 2] Process           ffmpeg normalizes to 1280×720 @ 30fps,
     │                      adds title + date overlay for first 2 seconds
     │                      (runs in parallel across CPU cores)
     ▼
[Phase 3] Compile           ffmpeg concatenates all clips with
                            0.5s crossfade transitions → compilation.mp4
```

### Output format

- Resolution: 1280×720
- Frame rate: 30 fps
- Video codec: H.264 (libx264, CRF 23)
- Audio codec: AAC 128 kbps, 44.1 kHz stereo
- Transitions: 0.5s fade between clips
- Title overlay: clip name + upload date, displayed for 2 seconds at the top of each clip

### Caching

Downloaded raw clips and processed clips are cached in `clips/`. Re-runs skip already-completed steps automatically. Use `--no-cache` or `--clean-cache` to force reprocessing.

## Development

```bash
pip install -r requirements.txt pytest
pytest tests/ -v
```

CI runs automatically on every push via GitHub Actions (`.github/workflows/test.yml`).
