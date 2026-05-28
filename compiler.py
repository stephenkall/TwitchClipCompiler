#!/usr/bin/env python3
"""
TwitchClipCompiler - Downloads Twitch clips and builds a compilation video.

Usage:
    python compiler.py [--input videolist.txt] [--output compilation.mp4]
                       [--clips-dir clips] [--no-download] [--no-process]
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import yt_dlp

TITLE_DISPLAY_DURATION = 2.0    # seconds title/date shown at start of each clip
TRANSITION_DURATION = 0.5       # seconds for fade transition between clips
TARGET_WIDTH = 1280
TARGET_HEIGHT = 720
TARGET_FPS = 30
CRF = 23
AUDIO_BITRATE = "128k"


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
    # Colon after drive letter (Windows) must be escaped in ffmpeg filter syntax
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


def download_clip(url: str, clips_dir: Path, metadata_file: Path) -> tuple[Path, dict]:
    """
    Download a Twitch clip with yt-dlp and cache metadata to a JSON file.
    Returns (local_path, metadata_dict). Re-uses cache if clip already exists.
    """
    if metadata_file.exists():
        with open(metadata_file, encoding="utf-8") as f:
            all_meta: dict = json.load(f)
        if url in all_meta:
            meta = all_meta[url]
            clip_path = Path(meta["path"])
            if clip_path.exists():
                print(f"  [cached] {meta['title']}")
                return clip_path, meta

    clips_dir.mkdir(parents=True, exist_ok=True)

    ydl_opts = {
        "outtmpl": str(clips_dir / "%(id)s.%(ext)s"),
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "quiet": False,
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
) -> None:
    """
    Normalize a clip to TARGET_WIDTH x TARGET_HEIGHT @ TARGET_FPS and overlay
    the title + date for the first TITLE_DISPLAY_DURATION seconds.
    Skips if output already exists.
    """
    if output_path.exists():
        print(f"  [cached] {output_path.name}")
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Write text to sidecar files so we avoid ffmpeg filter escaping entirely.
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
    # Single-quoted expression: comma inside quotes is not a filter separator.
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
    subprocess.run(cmd, check=True)


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

    print(f"  Getting durations for {n} clips...")
    durations = [get_video_duration(p) for p in processed_clips]

    inputs: list[str] = []
    for p in processed_clips:
        inputs += ["-i", str(p)]

    # Build xfade video chain and acrossfade audio chain.
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

    # Write filter to a temp file to avoid Windows command-line length limits.
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Compile Twitch clips into a video")
    parser.add_argument("--input", default="videolist.txt", help="File with clip URLs")
    parser.add_argument("--output", default="compilation.mp4", help="Output video file")
    parser.add_argument("--clips-dir", default="clips", help="Directory for clips")
    parser.add_argument("--no-download", action="store_true", help="Skip download phase")
    parser.add_argument("--no-process", action="store_true", help="Skip processing phase")
    args = parser.parse_args()

    videolist = Path(args.input)
    output_file = Path(args.output)
    clips_dir = Path(args.clips_dir)
    raw_dir = clips_dir / "raw"
    processed_dir = clips_dir / "processed"
    metadata_file = clips_dir / "metadata.json"

    urls = load_urls(videolist)
    print(f"[*] {len(urls)} clips in playlist")

    font_path = get_font_path()
    print(f"[*] Font: {font_path or 'system default'}")

    # ── Phase 1: Download ─────────────────────────────────────────────────────
    clip_data: list[tuple[Path, dict]] = []
    if not args.no_download:
        print(f"\n[1/3] Downloading {len(urls)} clips...")
        for i, url in enumerate(urls, 1):
            print(f"  [{i:3d}/{len(urls)}] {url}")
            try:
                path, meta = download_clip(url, raw_dir, metadata_file)
                clip_data.append((path, meta))
            except Exception as e:
                print(f"  [!] Failed to download: {e}", file=sys.stderr)
    else:
        if metadata_file.exists():
            with open(metadata_file, encoding="utf-8") as f:
                all_meta = json.load(f)
            for url in urls:
                if url in all_meta:
                    m = all_meta[url]
                    clip_data.append((Path(m["path"]), m))

    # ── Phase 2: Process ──────────────────────────────────────────────────────
    processed_clips: list[Path] = []
    if not args.no_process:
        print(f"\n[2/3] Processing {len(clip_data)} clips...")
        for i, (raw_path, meta) in enumerate(clip_data, 1):
            clip_id = meta.get("id", f"clip_{i:03d}")
            out_path = processed_dir / f"{i:03d}_{clip_id}.mp4"
            print(f"  [{i:3d}/{len(clip_data)}] {meta['title'][:70]}")
            try:
                process_clip(
                    raw_path, out_path,
                    meta["title"], meta.get("upload_date"),
                    font_path,
                )
                processed_clips.append(out_path)
            except Exception as e:
                print(f"  [!] Failed to process: {e}", file=sys.stderr)
    else:
        processed_clips = sorted(processed_dir.glob("*.mp4"))

    # ── Phase 3: Compile ──────────────────────────────────────────────────────
    if not processed_clips:
        print("[!] No clips available to compile", file=sys.stderr)
        sys.exit(1)

    print(f"\n[3/3] Building compilation from {len(processed_clips)} clips...")
    build_compilation(processed_clips, output_file)
    print(f"\n[✓] Done → {output_file}")


if __name__ == "__main__":
    main()
