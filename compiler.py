#!/usr/bin/env python3
"""
TwitchClipCompiler - Downloads Twitch clips and builds a compilation video.

Usage:
    python compiler.py [options]

Options:
    --input FILE        URL list file (default: videolist.txt)
    --output FILE       Output video file (default: compilation.mp4)
    --clips-dir DIR     Directory for clips (default: clips)
    --no-download       Skip download phase
    --no-process        Skip processing phase
    --no-cache          Reprocess clips even if output already exists
    --clean-cache       Delete clips/processed/ before starting
    --workers N         Parallel workers for processing (default: 75% of CPU cores)
    --quiet, -q         Suppress progress output
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _ensure_deps() -> None:
    req = Path(__file__).parent / "requirements.txt"
    if not req.exists():
        return
    try:
        import pkg_resources
        pkg_resources.require(req.read_text().splitlines())
    except Exception:
        print("Installing dependencies from requirements.txt...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-r", str(req), "-q"]
        )
        print("Done.")


_ensure_deps()

import argparse
import json
import logging
import os
import platform
import shutil
import signal
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import yt_dlp
from tqdm import tqdm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

TITLE_DISPLAY_DURATION = 2.0
TRANSITION_DURATION = 0.5
TARGET_WIDTH = 1280
TARGET_HEIGHT = 720
TARGET_FPS = 30
CRF = 23
AUDIO_BITRATE = "128k"

# ── Signal handling ───────────────────────────────────────────────────────────

_stop = False


def _on_signal() -> None:
    global _stop
    if _stop:
        print("\nForce-quitting.", file=sys.stderr)
        sys.exit(1)
    print(
        "\n[Ctrl+C] Finishing current item and saving progress. Press again to force-quit.",
        file=sys.stderr,
    )
    _stop = True


signal.signal(signal.SIGINT, lambda *_: _on_signal())
if platform.system() != "Windows":
    signal.signal(signal.SIGTERM, lambda *_: _on_signal())


# ── Utility functions ─────────────────────────────────────────────────────────


def worker_count(override: Optional[int] = None, fraction: float = 0.75) -> int:
    """Return the number of parallel workers to use."""
    cores = os.cpu_count() or 1
    return min(override, cores) if override else max(1, int(cores * fraction))


def get_font_path() -> Optional[str]:
    """Return path to a usable TTF font for ffmpeg drawtext, or None."""
    candidates: dict[str, list[str]] = {
        "Windows": [
            r"C:\Windows\Fonts\arial.ttf",
            r"C:\Windows\Fonts\segoeui.ttf",
            r"C:\Windows\Fonts\calibri.ttf",
        ],
        "Linux": [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        ],
        "Darwin": [
            "/System/Library/Fonts/Helvetica.ttc",
            "/Library/Fonts/Arial.ttf",
        ],
    }
    system = platform.system()
    for path in candidates.get(system, candidates["Linux"]):
        if os.path.exists(path):
            return path
    return None


def escape_ffmpeg_path(path: str) -> str:
    """Escape a filesystem path for use inside ffmpeg filter option values."""
    path = path.replace("\\", "/")
    path = path.replace(":", "\\:")
    return path


def format_date(date_str: Optional[str]) -> str:
    """Convert YYYYMMDD string to DD/MM/YYYY. Returns 'Unknown date' on bad input."""
    if date_str and len(date_str) == 8 and date_str.isdigit():
        return f"{date_str[6:8]}/{date_str[4:6]}/{date_str[:4]}"
    return date_str or "Unknown date"


def get_video_duration(path: Path) -> float:
    """Return video duration in seconds via ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


def sort_clips_chronologically(
    clip_data: list[tuple[Path, dict]],
) -> list[tuple[Path, dict]]:
    """
    Return clip_data sorted oldest-first by upload_date (YYYYMMDD).
    Clips with a missing or malformed date are placed at the end.
    """
    def sort_key(item: tuple[Path, dict]) -> str:
        date = item[1].get("upload_date", "") or ""
        if len(date) == 8 and date.isdigit():
            return date
        return "99999999"

    return sorted(clip_data, key=sort_key)


# ── Core pipeline ─────────────────────────────────────────────────────────────


def download_clip(
    url: str,
    clips_dir: Path,
    metadata_file: Path,
    no_cache: bool = False,
) -> tuple[Path, dict]:
    """
    Download a Twitch clip with yt-dlp and cache metadata to a JSON file.
    Returns (local_path, metadata_dict). Re-uses cache if clip already exists
    and no_cache is False.
    """
    if not no_cache and metadata_file.exists():
        with open(metadata_file, encoding="utf-8") as f:
            all_meta: dict = json.load(f)
        if url in all_meta:
            meta = all_meta[url]
            clip_path = Path(meta["path"])
            if clip_path.exists():
                return clip_path, meta

    clips_dir.mkdir(parents=True, exist_ok=True)

    ydl_opts = {
        "outtmpl": str(clips_dir / "%(id)s.%(ext)s"),
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "quiet": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = Path(ydl.prepare_filename(info))

        if not filename.exists():
            clip_id = info.get("id", "")
            for f in clips_dir.glob(f"{clip_id}.*"):
                filename = f
                break

        meta = {
            "title": info.get("title", "Twitch Clip"),
            "upload_date": info.get("upload_date", ""),
            "id": info.get("id", ""),
            "url": url,
            "path": str(filename),
        }

    all_meta = {}
    if metadata_file.exists():
        with open(metadata_file, encoding="utf-8") as f:
            all_meta = json.load(f)
    all_meta[url] = meta
    metadata_file.parent.mkdir(parents=True, exist_ok=True)
    with open(metadata_file, "w", encoding="utf-8") as f:
        json.dump(all_meta, f, indent=2, ensure_ascii=False)

    return filename, meta


def process_clip(
    input_path: Path,
    output_path: Path,
    title: str,
    date_str: Optional[str],
    font_path: Optional[str] = None,
    no_cache: bool = False,
) -> None:
    """
    Normalize a clip to TARGET_WIDTH x TARGET_HEIGHT @ TARGET_FPS and overlay
    the title + date for the first TITLE_DISPLAY_DURATION seconds.
    Skips if output already exists and no_cache is False.
    """
    if not no_cache and output_path.exists():
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)

    clip_id = output_path.stem
    tmp_dir = output_path.parent
    title_file = tmp_dir / f"{clip_id}_title.txt"
    date_file = tmp_dir / f"{clip_id}_date.txt"
    title_file.write_text(title, encoding="utf-8")
    date_file.write_text(format_date(date_str), encoding="utf-8")

    title_path = escape_ffmpeg_path(str(title_file))
    date_path = escape_ffmpeg_path(str(date_file))
    font_spec = (
        f"fontfile='{escape_ffmpeg_path(font_path)}'"
        if font_path
        else "font=Sans"
    )
    enable = f"'lt(t,{TITLE_DISPLAY_DURATION})'"

    vf = ",".join([
        f"scale={TARGET_WIDTH}:{TARGET_HEIGHT}:force_original_aspect_ratio=decrease",
        f"pad={TARGET_WIDTH}:{TARGET_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black",
        f"fps={TARGET_FPS}",
        (
            f"drawtext={font_spec}"
            f":textfile='{title_path}'"
            f":fontsize=36:fontcolor=white"
            f":x=(w-text_w)/2:y=40"
            f":box=1:boxcolor=black@0.65:boxborderw=12"
            f":shadowcolor=black@0.5:shadowx=2:shadowy=2"
            f":enable={enable}"
        ),
        (
            f"drawtext={font_spec}"
            f":textfile='{date_path}'"
            f":fontsize=22:fontcolor=white@0.9"
            f":x=(w-text_w)/2:y=90"
            f":box=1:boxcolor=black@0.55:boxborderw=8"
            f":enable={enable}"
        ),
    ])

    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-crf", str(CRF),
        "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", "44100", "-ac", "2",
        "-movflags", "+faststart",
        str(output_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def build_compilation(processed_clips: list[Path], output_file: Path) -> None:
    """
    Concatenate processed clips into one file with xfade (fade) transitions.
    Uses a filter script file to avoid command-line length limits on Windows.
    """
    n = len(processed_clips)
    if n == 0:
        raise ValueError("No clips to compile")

    if n == 1:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(processed_clips[0]), "-c", "copy", str(output_file)],
            check=True,
        )
        return

    log.info("Getting durations for %d clips...", n)
    durations = [get_video_duration(p) for p in processed_clips]

    inputs: list[str] = []
    for p in processed_clips:
        inputs += ["-i", str(p)]

    video_filters: list[str] = []
    audio_filters: list[str] = []
    cumulative_offset = 0.0
    prev_v = "[0:v]"
    prev_a = "[0:a]"

    for i in range(1, n):
        cumulative_offset += durations[i - 1] - TRANSITION_DURATION
        out_v = "[outv]" if i == n - 1 else f"[v{i}]"
        out_a = "[outa]" if i == n - 1 else f"[a{i}]"

        video_filters.append(
            f"{prev_v}[{i}:v]xfade=transition=fade"
            f":duration={TRANSITION_DURATION}"
            f":offset={cumulative_offset:.3f}{out_v}"
        )
        audio_filters.append(
            f"{prev_a}[{i}:a]acrossfade=d={TRANSITION_DURATION}{out_a}"
        )
        prev_v = out_v
        prev_a = out_a

    filter_complex = ";".join(video_filters + audio_filters)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as f:
        f.write(filter_complex)
        filter_script = f.name

    try:
        cmd = [
            "ffmpeg", "-y",
            *inputs,
            "-filter_complex_script", filter_script,
            "-map", "[outv]",
            "-map", "[outa]",
            "-c:v", "libx264", "-preset", "fast", "-crf", str(CRF),
            "-c:a", "aac", "-b:a", AUDIO_BITRATE,
            "-movflags", "+faststart",
            str(output_file),
        ]
        subprocess.run(cmd, check=True)
    finally:
        os.unlink(filter_script)


def load_urls(videolist_path: Path) -> list[str]:
    """Parse HTTP/HTTPS URLs from a text file (one per line, ignores comments)."""
    urls: list[str] = []
    with open(videolist_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith(("http://", "https://")):
                urls.append(line)
    return urls


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Compile Twitch clips into a video")
    parser.add_argument("--input", default="videolist.txt", help="File with clip URLs")
    parser.add_argument("--output", default="compilation.mp4", help="Output video file")
    parser.add_argument("--clips-dir", default="clips", help="Directory for clips")
    parser.add_argument("--no-download", action="store_true", help="Skip download phase")
    parser.add_argument("--no-process", action="store_true", help="Skip processing phase")
    parser.add_argument("--no-cache", action="store_true", help="Reprocess even if output exists")
    parser.add_argument("--clean-cache", action="store_true", help="Delete processed clips before starting")
    parser.add_argument("--workers", "-j", type=int, default=None, help="Parallel workers for processing")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress progress output")
    args = parser.parse_args()

    if args.quiet:
        logging.disable(logging.CRITICAL)
        os.environ["TQDM_DISABLE"] = "1"

    videolist = Path(args.input)
    output_file = Path(args.output)
    clips_dir = Path(args.clips_dir)
    raw_dir = clips_dir / "raw"
    processed_dir = clips_dir / "processed"
    metadata_file = clips_dir / "metadata.json"

    if args.clean_cache:
        shutil.rmtree(processed_dir, ignore_errors=True)
        log.info("Processed cache cleared.")

    urls = load_urls(videolist)
    n_workers = worker_count(args.workers)
    log.info("%d clips in playlist | workers: %d | font: %s",
             len(urls), n_workers, get_font_path() or "system default")

    font_path = get_font_path()

    # ── Phase 1: Download ─────────────────────────────────────────────────────
    clip_data: list[tuple[Path, dict]] = []
    if not args.no_download:
        log.info("Phase 1/3 — Downloading %d clips...", len(urls))
        for url in tqdm(urls, desc="Downloading", unit="clip", disable=args.quiet):
            if _stop:
                log.warning("Interrupted — re-run to continue.")
                sys.exit(0)
            try:
                path, meta = download_clip(url, raw_dir, metadata_file, no_cache=args.no_cache)
                clip_data.append((path, meta))
            except Exception as e:
                log.error("Failed to download %s: %s", url, e)
    else:
        if metadata_file.exists():
            with open(metadata_file, encoding="utf-8") as f:
                all_meta = json.load(f)
            for url in urls:
                if url in all_meta:
                    m = all_meta[url]
                    clip_data.append((Path(m["path"]), m))

    clip_data = sort_clips_chronologically(clip_data)
    if clip_data:
        first_date = clip_data[0][1].get("upload_date", "?")
        last_date = clip_data[-1][1].get("upload_date", "?")
        log.info("Clips sorted chronologically: %s → %s",
                 format_date(first_date), format_date(last_date))

    # ── Phase 2: Process (parallel) ──────────────────────────────────────────
    processed_clips: list[Optional[Path]] = [None] * len(clip_data)

    if not args.no_process:
        log.info("Phase 2/3 — Processing %d clips with %d workers...", len(clip_data), n_workers)

        def _process_one(indexed: tuple[int, tuple[Path, dict]]) -> tuple[int, Optional[Path]]:
            idx, (raw_path, meta) = indexed
            if _stop:
                return idx, None
            clip_id = meta.get("id", f"clip_{idx + 1:03d}")
            out_path = processed_dir / f"{idx + 1:03d}_{clip_id}.mp4"
            try:
                process_clip(
                    raw_path, out_path,
                    meta["title"], meta.get("upload_date"),
                    font_path, no_cache=args.no_cache,
                )
                return idx, out_path
            except Exception as e:
                log.error("Failed to process '%s': %s", meta.get("title", "?"), e)
                return idx, None

        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            futures = pool.map(_process_one, enumerate(clip_data))
            for idx, out_path in tqdm(
                futures, total=len(clip_data), desc="Processing", unit="clip", disable=args.quiet
            ):
                if _stop:
                    log.warning("Interrupted — re-run to continue.")
                    sys.exit(0)
                processed_clips[idx] = out_path
    else:
        for i, p in enumerate(sorted(processed_dir.glob("*.mp4"))):
            if i < len(processed_clips):
                processed_clips[i] = p

    final_clips = [p for p in processed_clips if p is not None]

    # ── Phase 3: Compile ──────────────────────────────────────────────────────
    if not final_clips:
        log.error("No clips available to compile.")
        sys.exit(1)

    log.info("Phase 3/3 — Building compilation from %d clips...", len(final_clips))
    build_compilation(final_clips, output_file)
    log.info("Done → %s", output_file)


if __name__ == "__main__":
    main()
