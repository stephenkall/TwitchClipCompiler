"""Unit tests for compiler.py."""

import json
import os
import subprocess
from pathlib import Path
from unittest import mock

import pytest

import compiler


# ── format_date ──────────────────────────────────────────────────────────────

class TestFormatDate:
    def test_valid_date(self):
        assert compiler.format_date("20240315") == "15/03/2024"

    def test_new_year(self):
        assert compiler.format_date("20240101") == "01/01/2024"

    def test_end_of_year(self):
        assert compiler.format_date("20231231") == "31/12/2023"

    def test_empty_string(self):
        assert compiler.format_date("") == "Unknown date"

    def test_none_returns_unknown(self):
        assert compiler.format_date(None) == "Unknown date"

    def test_non_numeric_passthrough(self):
        assert compiler.format_date("2024-03-15") == "2024-03-15"

    def test_wrong_length_passthrough(self):
        assert compiler.format_date("202403") == "202403"


# ── escape_ffmpeg_path ────────────────────────────────────────────────────────

class TestEscapeFFmpegPath:
    def test_windows_drive_colon_escaped(self):
        result = compiler.escape_ffmpeg_path(r"C:\Windows\Fonts\arial.ttf")
        assert "\\:" in result

    def test_backslashes_converted_to_forward(self):
        result = compiler.escape_ffmpeg_path(r"path\to\file.ttf")
        assert "\\" not in result.replace("\\:", "")

    def test_linux_path_unchanged(self):
        path = "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"
        assert compiler.escape_ffmpeg_path(path) == path

    def test_windows_path_forward_slashes(self):
        result = compiler.escape_ffmpeg_path(r"C:\Windows\Fonts\arial.ttf")
        assert result == "C\\:/Windows/Fonts/arial.ttf"


# ── load_urls ─────────────────────────────────────────────────────────────────

class TestLoadUrls:
    def test_parses_https_urls(self, tmp_path):
        f = tmp_path / "list.txt"
        f.write_text(
            "https://www.twitch.tv/ch/clip/Clip1\n"
            "https://www.twitch.tv/ch/clip/Clip2\n"
        )
        assert compiler.load_urls(f) == [
            "https://www.twitch.tv/ch/clip/Clip1",
            "https://www.twitch.tv/ch/clip/Clip2",
        ]

    def test_skips_empty_lines(self, tmp_path):
        f = tmp_path / "list.txt"
        f.write_text("\nhttps://twitch.tv/ch/clip/A\n\nhttps://twitch.tv/ch/clip/B\n")
        assert len(compiler.load_urls(f)) == 2

    def test_skips_comment_lines(self, tmp_path):
        f = tmp_path / "list.txt"
        f.write_text("# comment\nhttps://twitch.tv/ch/clip/A\nsome text\n")
        assert len(compiler.load_urls(f)) == 1

    def test_empty_file_returns_empty_list(self, tmp_path):
        f = tmp_path / "list.txt"
        f.write_text("")
        assert compiler.load_urls(f) == []

    def test_parses_http_urls(self, tmp_path):
        f = tmp_path / "list.txt"
        f.write_text("http://twitch.tv/ch/clip/A\n")
        assert len(compiler.load_urls(f)) == 1


# ── sort_clips_chronologically ───────────────────────────────────────────────

class TestSortClipsChronologically:
    def _make(self, date: str) -> tuple[Path, dict]:
        return Path(f"clip_{date}.mp4"), {"upload_date": date, "title": f"Clip {date}"}

    def test_sorts_oldest_first(self):
        clips = [self._make("20240315"), self._make("20230101"), self._make("20240101")]
        result = compiler.sort_clips_chronologically(clips)
        dates = [r[1]["upload_date"] for r in result]
        assert dates == ["20230101", "20240101", "20240315"]

    def test_already_sorted_unchanged(self):
        clips = [self._make("20230101"), self._make("20230601"), self._make("20240101")]
        result = compiler.sort_clips_chronologically(clips)
        dates = [r[1]["upload_date"] for r in result]
        assert dates == ["20230101", "20230601", "20240101"]

    def test_missing_date_goes_to_end(self):
        clips = [self._make(""), self._make("20240315"), self._make("20230101")]
        result = compiler.sort_clips_chronologically(clips)
        dates = [r[1]["upload_date"] for r in result]
        assert dates == ["20230101", "20240315", ""]

    def test_none_date_goes_to_end(self):
        clips = [
            (Path("clip_no_date.mp4"), {"upload_date": None, "title": "no date"}),
            self._make("20240315"),
        ]
        result = compiler.sort_clips_chronologically(clips)
        assert result[0][1]["upload_date"] == "20240315"

    def test_empty_list_returns_empty(self):
        assert compiler.sort_clips_chronologically([]) == []

    def test_single_clip_unchanged(self):
        clips = [self._make("20240315")]
        result = compiler.sort_clips_chronologically(clips)
        assert result == clips

    def test_same_date_preserves_relative_order(self):
        clips = [self._make("20240315"), self._make("20240315")]
        result = compiler.sort_clips_chronologically(clips)
        assert [r[1]["upload_date"] for r in result] == ["20240315", "20240315"]


# ── worker_count ─────────────────────────────────────────────────────────────

class TestWorkerCount:
    def test_override_respected(self):
        assert compiler.worker_count(override=2) == 2

    def test_override_capped_at_cpu_count(self):
        with mock.patch("os.cpu_count", return_value=4):
            assert compiler.worker_count(override=100) == 4

    def test_default_is_75_percent_of_cores(self):
        with mock.patch("os.cpu_count", return_value=8):
            assert compiler.worker_count() == 6  # floor(8 * 0.75)

    def test_minimum_one_worker(self):
        with mock.patch("os.cpu_count", return_value=1):
            assert compiler.worker_count() >= 1

    def test_none_cpu_count_defaults_to_one(self):
        with mock.patch("os.cpu_count", return_value=None):
            assert compiler.worker_count() >= 1


# ── get_video_duration ────────────────────────────────────────────────────────

class TestGetVideoDuration:
    def test_returns_float(self):
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(stdout="42.5\n", returncode=0)
            assert compiler.get_video_duration(Path("test.mp4")) == 42.5

    def test_strips_whitespace(self):
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(stdout="  15.123  \n", returncode=0)
            assert compiler.get_video_duration(Path("test.mp4")) == pytest.approx(15.123)

    def test_calls_ffprobe(self):
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(stdout="10.0\n", returncode=0)
            compiler.get_video_duration(Path("video.mp4"))
            cmd = mock_run.call_args[0][0]
        assert cmd[0] == "ffprobe"
        assert "video.mp4" in cmd


# ── process_clip ──────────────────────────────────────────────────────────────

class TestProcessClip:
    def test_skips_if_output_exists(self, tmp_path):
        inp = tmp_path / "input.mp4"
        inp.touch()
        out = tmp_path / "output.mp4"
        out.touch()

        with mock.patch("subprocess.run") as mock_run:
            compiler.process_clip(inp, out, "Title", "20240315")
        mock_run.assert_not_called()

    def test_creates_title_and_date_sidecar_files(self, tmp_path):
        inp = tmp_path / "input.mp4"
        inp.touch()
        out = tmp_path / "processed" / "001_clip.mp4"

        with mock.patch("subprocess.run"):
            compiler.process_clip(inp, out, "My Clip Title", "20240315")

        title_files = list((tmp_path / "processed").glob("*_title.txt"))
        date_files = list((tmp_path / "processed").glob("*_date.txt"))
        assert len(title_files) == 1
        assert title_files[0].read_text(encoding="utf-8") == "My Clip Title"
        assert len(date_files) == 1
        assert date_files[0].read_text(encoding="utf-8") == "15/03/2024"

    def test_calls_ffmpeg(self, tmp_path):
        inp = tmp_path / "input.mp4"
        inp.touch()
        out = tmp_path / "output.mp4"

        with mock.patch("subprocess.run") as mock_run:
            compiler.process_clip(inp, out, "My Clip", "20240315")
            cmd = mock_run.call_args[0][0]
        assert cmd[0] == "ffmpeg"
        assert "-vf" in cmd
        assert str(out) in cmd

    def test_vf_contains_drawtext(self, tmp_path):
        inp = tmp_path / "input.mp4"
        inp.touch()
        out = tmp_path / "output.mp4"

        with mock.patch("subprocess.run") as mock_run:
            compiler.process_clip(inp, out, "My Clip", "20240315")
            cmd = mock_run.call_args[0][0]

        vf_idx = cmd.index("-vf") + 1
        assert "drawtext" in cmd[vf_idx]
        assert "scale=" in cmd[vf_idx]

    def test_no_cache_reprocesses_existing_output(self, tmp_path):
        inp = tmp_path / "input.mp4"
        inp.touch()
        out = tmp_path / "output.mp4"
        out.touch()  # Already exists

        with mock.patch("subprocess.run") as mock_run:
            compiler.process_clip(inp, out, "Title", "20240315", no_cache=True)
        mock_run.assert_called_once()  # Should call ffmpeg despite existing output

    def test_date_none_handled(self, tmp_path):
        inp = tmp_path / "input.mp4"
        inp.touch()
        out = tmp_path / "output.mp4"

        with mock.patch("subprocess.run"):
            compiler.process_clip(inp, out, "My Clip", None)

        date_files = list(tmp_path.glob("*_date.txt"))
        assert len(date_files) == 1
        assert date_files[0].read_text(encoding="utf-8") == "Unknown date"


# ── build_compilation ─────────────────────────────────────────────────────────

class TestBuildCompilation:
    def test_raises_on_empty_list(self, tmp_path):
        with pytest.raises(ValueError, match="No clips"):
            compiler.build_compilation([], tmp_path / "out.mp4")

    def test_single_clip_uses_copy(self, tmp_path):
        clip = tmp_path / "clip.mp4"
        clip.touch()
        out = tmp_path / "out.mp4"

        with mock.patch("subprocess.run") as mock_run:
            compiler.build_compilation([clip], out)
            cmd = mock_run.call_args[0][0]
        assert "-c" in cmd and "copy" in cmd

    def test_multiple_clips_uses_filter_complex_script(self, tmp_path):
        clips = [tmp_path / f"clip{i}.mp4" for i in range(3)]
        for c in clips:
            c.touch()
        out = tmp_path / "out.mp4"

        with mock.patch("compiler.get_video_duration", return_value=10.0), \
             mock.patch("subprocess.run") as mock_run:
            compiler.build_compilation(clips, out)
            cmd = mock_run.call_args[0][0]
        assert "-filter_complex_script" in cmd

    def test_xfade_offset_correct_for_two_clips(self, tmp_path):
        clips = [tmp_path / f"clip{i}.mp4" for i in range(2)]
        for c in clips:
            c.touch()
        out = tmp_path / "out.mp4"
        duration = 10.0
        expected_offset = duration - compiler.TRANSITION_DURATION

        filter_content = ""

        def capture(cmd, **kwargs):
            nonlocal filter_content
            if "-filter_complex_script" in cmd:
                idx = cmd.index("-filter_complex_script") + 1
                with open(cmd[idx], encoding="utf-8") as f:
                    filter_content = f.read()
            return mock.Mock(returncode=0)

        with mock.patch("compiler.get_video_duration", return_value=duration), \
             mock.patch("subprocess.run", side_effect=capture):
            compiler.build_compilation(clips, out)

        assert f"offset={expected_offset:.3f}" in filter_content

    def test_xfade_offset_accumulates_for_three_clips(self, tmp_path):
        clips = [tmp_path / f"clip{i}.mp4" for i in range(3)]
        for c in clips:
            c.touch()
        out = tmp_path / "out.mp4"
        duration = 8.0
        td = compiler.TRANSITION_DURATION

        filter_content = ""

        def capture(cmd, **kwargs):
            nonlocal filter_content
            if "-filter_complex_script" in cmd:
                idx = cmd.index("-filter_complex_script") + 1
                with open(cmd[idx], encoding="utf-8") as f:
                    filter_content = f.read()
            return mock.Mock(returncode=0)

        with mock.patch("compiler.get_video_duration", return_value=duration), \
             mock.patch("subprocess.run", side_effect=capture):
            compiler.build_compilation(clips, out)

        offset1 = duration - td
        offset2 = 2 * duration - 2 * td
        assert f"offset={offset1:.3f}" in filter_content
        assert f"offset={offset2:.3f}" in filter_content

    def test_filter_contains_xfade_and_acrossfade(self, tmp_path):
        clips = [tmp_path / f"clip{i}.mp4" for i in range(2)]
        for c in clips:
            c.touch()
        out = tmp_path / "out.mp4"
        filter_content = ""

        def capture(cmd, **kwargs):
            nonlocal filter_content
            if "-filter_complex_script" in cmd:
                idx = cmd.index("-filter_complex_script") + 1
                with open(cmd[idx], encoding="utf-8") as f:
                    filter_content = f.read()
            return mock.Mock(returncode=0)

        with mock.patch("compiler.get_video_duration", return_value=5.0), \
             mock.patch("subprocess.run", side_effect=capture):
            compiler.build_compilation(clips, out)

        assert "xfade" in filter_content
        assert "acrossfade" in filter_content

    def test_all_clips_appear_as_inputs(self, tmp_path):
        clips = [tmp_path / f"clip{i}.mp4" for i in range(4)]
        for c in clips:
            c.touch()
        out = tmp_path / "out.mp4"

        with mock.patch("compiler.get_video_duration", return_value=6.0), \
             mock.patch("subprocess.run") as mock_run:
            compiler.build_compilation(clips, out)
            cmd = mock_run.call_args[0][0]

        input_count = cmd.count("-i")
        assert input_count == 4
