"""app.watcherのテスト"""

from pathlib import Path
from unittest.mock import Mock

import pytest

from app.watcher import DONE_DIR_NAME, ERROR_DIR_NAME, FolderWatcher


@pytest.fixture
def watch_dir(tmp_path: Path) -> Path:
    """一時的な監視ディレクトリ"""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    return inbox


@pytest.fixture
def watcher(watch_dir: Path) -> FolderWatcher:
    """議事録生成を無効化したFolderWatcher"""
    return FolderWatcher(watch_dir, enable_minutes=False)


def _scan_until_stable(watcher: FolderWatcher) -> int:
    """サイズ安定判定のため2回スキャンする"""
    watcher.scan_once()
    return watcher.scan_once()


def test_init_creates_subdirs(watch_dir: Path) -> None:
    """初期化時に完了・エラーフォルダが作成される"""
    FolderWatcher(watch_dir, enable_minutes=False)

    assert (watch_dir / DONE_DIR_NAME).is_dir()
    assert (watch_dir / ERROR_DIR_NAME).is_dir()


def test_first_scan_does_not_process(watcher: FolderWatcher, watch_dir: Path) -> None:
    """初回スキャンではコピー完了待ちのため処理しない"""
    (watch_dir / "meeting.txt").write_text("会議の内容", encoding="utf-8")

    assert watcher.scan_once() == 0


def test_text_file_generates_minutes(watcher: FolderWatcher, watch_dir: Path) -> None:
    """テキストファイルから議事録が生成され、完了フォルダへ移動する"""
    watcher.minutes_gen = Mock()
    (watch_dir / "meeting.txt").write_text("会議の内容", encoding="utf-8")

    assert _scan_until_stable(watcher) == 1

    watcher.minutes_gen.generate_and_save.assert_called_once()
    kwargs = watcher.minutes_gen.generate_and_save.call_args.kwargs
    assert kwargs["meeting_title"] == "meeting"
    assert kwargs["output_path"] == watch_dir / DONE_DIR_NAME / "meeting_議事録.md"
    assert (watch_dir / DONE_DIR_NAME / "meeting.txt").exists()
    assert not (watch_dir / "meeting.txt").exists()


def test_text_file_without_minutes_gen_goes_to_error(
    watcher: FolderWatcher, watch_dir: Path
) -> None:
    """議事録生成が無効の場合、テキストファイルはエラーフォルダへ移動する"""
    (watch_dir / "meeting.txt").write_text("会議の内容", encoding="utf-8")

    _scan_until_stable(watcher)

    assert (watch_dir / ERROR_DIR_NAME / "meeting.txt").exists()
    assert (watch_dir / ERROR_DIR_NAME / "meeting_エラー.txt").exists()


def test_audio_file_transcribed_and_minutes_generated(
    watcher: FolderWatcher, watch_dir: Path
) -> None:
    """音声ファイルは文字起こしと議事録の両方が生成される"""
    mock_transcriber = Mock()
    mock_transcriber.transcribe.return_value = ("文字起こし結果", [])

    def save_transcript(full_text: str, segments: list, output_path: Path) -> Path:
        output_path.write_text(full_text, encoding="utf-8")
        return output_path

    mock_transcriber.save_transcript.side_effect = save_transcript
    watcher._transcriber = mock_transcriber
    watcher.minutes_gen = Mock()

    (watch_dir / "meeting.wav").write_bytes(b"\x00" * 100)

    assert _scan_until_stable(watcher) == 1

    assert (watch_dir / DONE_DIR_NAME / "meeting_文字起こし.txt").exists()
    watcher.minutes_gen.generate_and_save.assert_called_once()
    assert (watch_dir / DONE_DIR_NAME / "meeting.wav").exists()


def test_audio_file_minutes_failure_keeps_transcript(
    watcher: FolderWatcher, watch_dir: Path
) -> None:
    """議事録生成に失敗しても文字起こしは完了フォルダに残る"""
    mock_transcriber = Mock()
    mock_transcriber.transcribe.return_value = ("文字起こし結果", [])
    mock_transcriber.save_transcript.side_effect = (
        lambda full_text, segments, output_path: output_path
    )
    watcher._transcriber = mock_transcriber
    watcher.minutes_gen = Mock()
    watcher.minutes_gen.generate_and_save.side_effect = Exception("LLM error")

    (watch_dir / "meeting.wav").write_bytes(b"\x00" * 100)

    _scan_until_stable(watcher)

    assert (watch_dir / DONE_DIR_NAME / "meeting.wav").exists()
    assert (watch_dir / DONE_DIR_NAME / "meeting_議事録エラー.txt").exists()
    assert not (watch_dir / ERROR_DIR_NAME / "meeting.wav").exists()


def test_transcribe_failure_moves_to_error(watcher: FolderWatcher, watch_dir: Path) -> None:
    """文字起こし失敗時はエラーフォルダへ移動する"""
    mock_transcriber = Mock()
    mock_transcriber.transcribe.side_effect = Exception("decode error")
    watcher._transcriber = mock_transcriber

    (watch_dir / "broken.wav").write_bytes(b"\x00" * 100)

    _scan_until_stable(watcher)

    assert (watch_dir / ERROR_DIR_NAME / "broken.wav").exists()
    assert (watch_dir / ERROR_DIR_NAME / "broken_エラー.txt").exists()


def test_growing_file_not_processed(watcher: FolderWatcher, watch_dir: Path) -> None:
    """サイズが変化し続けるファイル（コピー中）は処理しない"""
    target = watch_dir / "copying.wav"
    target.write_bytes(b"\x00" * 100)

    watcher.scan_once()
    target.write_bytes(b"\x00" * 200)  # コピー継続中を模擬

    assert watcher.scan_once() == 0
    assert target.exists()


def test_ignores_hidden_and_unsupported_files(watcher: FolderWatcher, watch_dir: Path) -> None:
    """隠しファイル・一時ファイル・対象外拡張子は無視する"""
    (watch_dir / ".hidden.wav").write_bytes(b"\x00")
    (watch_dir / "~$temp.txt").write_text("tmp", encoding="utf-8")
    (watch_dir / "document.pdf").write_bytes(b"\x00")

    assert _scan_until_stable(watcher) == 0
    assert (watch_dir / ".hidden.wav").exists()
    assert (watch_dir / "~$temp.txt").exists()
    assert (watch_dir / "document.pdf").exists()


def test_subdirectories_ignored(watcher: FolderWatcher, watch_dir: Path) -> None:
    """完了・エラーフォルダ内のファイルは再処理しない"""
    done_file = watch_dir / DONE_DIR_NAME / "old.txt"
    done_file.write_text("処理済み", encoding="utf-8")

    assert _scan_until_stable(watcher) == 0
    assert done_file.exists()
