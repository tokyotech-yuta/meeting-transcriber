"""app.webuiのテスト"""

from pathlib import Path
from unittest.mock import Mock

import gradio as gr
import pytest

from app.watcher import DONE_DIR_NAME, ERROR_DIR_NAME, STATUS_FILE_NAME
from app.webui import WebUI, parse_auth, render_watch_status


@pytest.fixture
def webui(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> WebUI:
    """Ollama非依存のWebUI（出力先は一時ディレクトリ）"""
    monkeypatch.setattr("app.webui.OUTPUT_DIR", tmp_path / "out")
    ui = WebUI.__new__(WebUI)
    ui.model_name = "medium"
    ui._transcriber = None
    ui.minutes_gen = None
    return ui


def _noop_progress(fraction: float, desc: str = "") -> None:
    """テスト用の進捗コールバック"""


def test_parse_auth_valid() -> None:
    """user:password形式を認証タプルに変換する"""
    assert parse_auth("admin:secret") == ("admin", "secret")


def test_parse_auth_invalid() -> None:
    """不正な形式はNone（認証なし）になる"""
    assert parse_auth("") is None
    assert parse_auth("no-colon") is None
    assert parse_auth(":nopass") is None
    assert parse_auth("nouser:") is None


def test_process_without_file(webui: WebUI) -> None:
    """ファイル未指定はエラーになる"""
    with pytest.raises(gr.Error):
        webui.process(None, "", "", progress=_noop_progress)


def test_process_unsupported_extension(webui: WebUI, tmp_path: Path) -> None:
    """対応外の拡張子はエラーになる"""
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF")

    with pytest.raises(gr.Error):
        webui.process(str(pdf), "", "", progress=_noop_progress)


def test_process_text_file(webui: WebUI, tmp_path: Path) -> None:
    """テキストファイルから議事録が生成される"""
    transcript_file = tmp_path / "meeting.txt"
    transcript_file.write_text("会議の内容", encoding="utf-8")

    webui.minutes_gen = Mock()
    webui.minutes_gen.generate_and_save.return_value = ("# 議事録\n\nテスト", Path("dummy.md"))

    minutes, status, downloads = webui.process(
        str(transcript_file), "定例会議", "参加者: 田中", progress=_noop_progress
    )

    assert "議事録" in minutes
    assert "✅" in status
    assert len(downloads) == 1
    kwargs = webui.minutes_gen.generate_and_save.call_args.kwargs
    assert kwargs["meeting_title"] == "定例会議"
    assert kwargs["additional_context"] == "参加者: 田中"


def test_process_text_without_minutes_gen(webui: WebUI, tmp_path: Path) -> None:
    """Ollama未接続でテキストを処理するとエラーになる"""
    transcript_file = tmp_path / "meeting.txt"
    transcript_file.write_text("会議の内容", encoding="utf-8")

    with pytest.raises(gr.Error):
        webui.process(str(transcript_file), "", "", progress=_noop_progress)


def test_process_audio_file(webui: WebUI, tmp_path: Path) -> None:
    """音声ファイルは文字起こしと議事録の両方が生成される"""
    audio = tmp_path / "meeting.wav"
    audio.write_bytes(b"\x00" * 100)

    mock_transcriber = Mock()
    mock_transcriber.transcribe.return_value = ("文字起こし結果", [])

    def save_transcript(full_text: str, segments: list, output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(full_text, encoding="utf-8")
        return output_path

    mock_transcriber.save_transcript.side_effect = save_transcript
    webui._transcriber = mock_transcriber
    webui.minutes_gen = Mock()
    webui.minutes_gen.generate_and_save.return_value = ("# 議事録", Path("dummy.md"))

    minutes, status, downloads = webui.process(str(audio), "", "", progress=_noop_progress)

    assert minutes == "# 議事録"
    assert len(downloads) == 2


def test_render_watch_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """処理中・待機中・完了・エラーが状況表示に含まれる"""
    inbox = tmp_path / "inbox"
    (inbox / DONE_DIR_NAME).mkdir(parents=True)
    (inbox / ERROR_DIR_NAME).mkdir()
    monkeypatch.setattr("app.webui.WATCH_DIR", inbox)

    (inbox / "processing.mp4").write_bytes(b"\x00")
    (inbox / "waiting.wav").write_bytes(b"\x00")
    (inbox / DONE_DIR_NAME / "old_議事録.md").write_text("# 議事録", encoding="utf-8")
    (inbox / ERROR_DIR_NAME / "broken_エラー.txt").write_text("失敗", encoding="utf-8")
    (inbox / STATUS_FILE_NAME).write_text(
        '{"current": {"file": "processing.mp4", "stage": "文字起こし中",'
        ' "started_at": "2026-07-04T10:00:00"},'
        ' "last": {"file": "done.wav", "result": "完了", "finished_at": "2026-07-04T09:00:00"}}',
        encoding="utf-8",
    )

    markdown = render_watch_status()

    assert "processing.mp4" in markdown
    assert "文字起こし中" in markdown
    assert "waiting.wav" in markdown
    assert "old_議事録.md" in markdown
    assert "broken_エラー.txt" in markdown
    assert "done.wav" in markdown


def test_render_watch_status_with_progress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """進捗情報がある場合、バー・進捗率・残り時間が表示される"""
    import json
    from datetime import datetime, timedelta

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr("app.webui.WATCH_DIR", inbox)

    stage_started = (datetime.now() - timedelta(minutes=10)).isoformat(timespec="seconds")
    (inbox / STATUS_FILE_NAME).write_text(
        json.dumps(
            {
                "current": {
                    "file": "zoom.mp4",
                    "stage": "文字起こし中",
                    "started_at": stage_started,
                    "stage_started_at": stage_started,
                    "progress": {"done_sec": 1545.0, "total_sec": 3090.0},
                },
                "last": None,
                "updated_at": stage_started,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    markdown = render_watch_status()

    assert "zoom.mp4" in markdown
    assert "50%" in markdown
    assert "▓" in markdown
    assert "経過 10分" in markdown
    assert "残り約10分" in markdown
    assert "51.5分中 25.8分処理済" in markdown


def test_render_watch_status_without_status_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """状況ファイルが無い場合、ファイルは「処理中または待機中」に表示される"""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr("app.webui.WATCH_DIR", inbox)
    (inbox / "meeting.mp4").write_bytes(b"\x00")

    markdown = render_watch_status()

    assert "処理中または待機中" in markdown
    assert "meeting.mp4" in markdown
    assert "⏳ 待機中" not in markdown


def test_render_watch_status_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """処理対象が無い場合はその旨を表示する"""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr("app.webui.WATCH_DIR", inbox)

    assert "処理中のファイルはありません" in render_watch_status()


def test_process_audio_empty_transcript(webui: WebUI, tmp_path: Path) -> None:
    """無音音声は文字起こしのみ保存し、議事録は生成しない"""
    audio = tmp_path / "silent.wav"
    audio.write_bytes(b"\x00" * 100)

    mock_transcriber = Mock()
    mock_transcriber.transcribe.return_value = ("", [])
    mock_transcriber.save_transcript.side_effect = (
        lambda full_text, segments, output_path: output_path
    )
    webui._transcriber = mock_transcriber
    webui.minutes_gen = Mock()

    minutes, status, downloads = webui.process(str(audio), "", "", progress=_noop_progress)

    assert minutes == ""
    assert "⚠️" in status
    webui.minutes_gen.generate_and_save.assert_not_called()
