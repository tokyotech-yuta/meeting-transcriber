"""フォルダ監視モジュール - 監視フォルダに置かれた音声/テキストから議事録を自動生成"""

import os
import shutil
import time
from pathlib import Path

from app.logger import setup_logger
from app.minutes_generator import MinutesGenerator
from app.transcriber import Transcriber

logger = setup_logger(__name__)

AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac", ".ogg"}
TEXT_EXTENSIONS = {".txt", ".md", ".text"}

DONE_DIR_NAME = "完了"
ERROR_DIR_NAME = "エラー"


class FolderWatcher:
    """監視フォルダのファイルを検出し、文字起こしと議事録を自動生成する

    音声ファイル → 文字起こし + 議事録、テキストファイル → 議事録のみ。
    処理結果と元ファイルは「完了」サブフォルダへ、失敗時は「エラー」サブフォルダへ移動する。
    """

    def __init__(
        self,
        watch_dir: Path,
        model_name: str = "medium",
        poll_interval: float = 5.0,
        enable_minutes: bool = True,
    ) -> None:
        """
        Args:
            watch_dir: 監視対象ディレクトリ
            model_name: Whisperモデル名（small/medium/large-v3/large-v3-ja）
            poll_interval: 監視間隔（秒）
            enable_minutes: 議事録生成を有効にするか
        """
        self.watch_dir = watch_dir
        self.done_dir = watch_dir / DONE_DIR_NAME
        self.error_dir = watch_dir / ERROR_DIR_NAME
        self.model_name = model_name
        self.poll_interval = poll_interval

        self.watch_dir.mkdir(parents=True, exist_ok=True)
        self.done_dir.mkdir(parents=True, exist_ok=True)
        self.error_dir.mkdir(parents=True, exist_ok=True)

        # コピー途中のファイルを処理しないよう、前回スキャン時のサイズを記録して比較する
        self._pending_sizes: dict[Path, int] = {}

        # Transcriberはモデルロードが重いため、初回の音声ファイル処理時に遅延初期化
        self._transcriber: Transcriber | None = None

        self.minutes_gen: MinutesGenerator | None = None
        if enable_minutes:
            try:
                self.minutes_gen = MinutesGenerator()
            except Exception as e:
                logger.warning(f"⚠️  議事録生成は無効です（Ollama未接続）: {e}")

    def _get_transcriber(self) -> Transcriber:
        if self._transcriber is None:
            self._transcriber = Transcriber(model_name=self.model_name)
        return self._transcriber

    def _target_files(self) -> list[Path]:
        """監視フォルダ直下の処理対象ファイルを列挙"""
        files = []
        for path in self.watch_dir.iterdir():
            if not path.is_file():
                continue
            # 隠しファイル・Office系一時ファイルは無視
            if path.name.startswith((".", "~$")):
                continue
            if path.suffix.lower() in AUDIO_EXTENSIONS | TEXT_EXTENSIONS:
                files.append(path)
        return sorted(files)

    def scan_once(self) -> int:
        """1回スキャンし、書き込みが完了したファイルを処理する

        Returns:
            処理したファイル数
        """
        processed = 0
        current: dict[Path, int] = {}

        for path in self._target_files():
            try:
                size = path.stat().st_size
            except OSError:
                continue  # スキャン中に移動・削除された
            current[path] = size

            # 前回スキャンとサイズが一致すればコピー完了とみなす
            if self._pending_sizes.get(path) == size:
                self._process_file(path)
                processed += 1

        self._pending_sizes = {p: s for p, s in current.items() if p.exists()}
        return processed

    def _process_file(self, path: Path) -> None:
        logger.info(f"\n📥 新しいファイルを検出: {path.name}")
        try:
            if path.suffix.lower() in AUDIO_EXTENSIONS:
                self._process_audio(path)
            else:
                self._process_text(path)
            logger.info(f"✅ 処理完了: {path.name}")
        except Exception as e:
            logger.error(f"❌ 処理失敗: {path.name}: {e}")
            self._move_to_error(path, str(e))

    def _process_audio(self, path: Path) -> None:
        """音声ファイルを文字起こしし、議事録を生成する"""
        transcriber = self._get_transcriber()
        full_text, segments = transcriber.transcribe(path)

        transcript_path = self.done_dir / f"{path.stem}_文字起こし.txt"
        transcriber.save_transcript(full_text, segments, output_path=transcript_path)

        try:
            self._generate_minutes(full_text, path.stem)
        except Exception as e:
            # 文字起こしは保存済みのため、議事録の失敗はエラーメモだけ残して完了扱いにする
            logger.error(f"❌ 議事録生成エラー（文字起こしは保存済み）: {e}")
            note = self.done_dir / f"{path.stem}_議事録エラー.txt"
            note.write_text(f"議事録の生成に失敗しました。\n\n{e}\n", encoding="utf-8")

        shutil.move(str(path), self.done_dir / path.name)

    def _process_text(self, path: Path) -> None:
        """文字起こしテキストから議事録を生成する"""
        transcript = path.read_text(encoding="utf-8")
        if not self._generate_minutes(transcript, path.stem):
            raise RuntimeError("議事録生成が利用できません（Ollama未接続）")
        shutil.move(str(path), self.done_dir / path.name)

    def _generate_minutes(self, transcript: str, stem: str) -> bool:
        """議事録を生成して完了フォルダに保存する

        Returns:
            生成した場合はTrue、議事録生成が無効の場合はFalse
        """
        if self.minutes_gen is None:
            logger.warning("⚠️  議事録生成をスキップ（Ollama未接続）")
            return False

        minutes_path = self.done_dir / f"{stem}_議事録.md"
        self.minutes_gen.generate_and_save(transcript, meeting_title=stem, output_path=minutes_path)
        return True

    def _move_to_error(self, path: Path, message: str) -> None:
        """失敗したファイルをエラーフォルダへ移動し、原因メモを残す"""
        try:
            note = self.error_dir / f"{path.stem}_エラー.txt"
            note.write_text(f"処理に失敗しました: {path.name}\n\n{message}\n", encoding="utf-8")
            if path.exists():
                shutil.move(str(path), self.error_dir / path.name)
        except OSError as e:
            logger.error(f"❌ エラーフォルダへの移動に失敗: {e}")

    def run(self) -> None:
        """監視ループを開始（Ctrl+Cで停止）"""
        logger.info("=" * 80)
        logger.info(f"👀 フォルダ監視を開始: {self.watch_dir}")
        logger.info("   音声/テキストファイルを置くと自動で文字起こし・議事録を生成します")
        logger.info(f"   結果は「{DONE_DIR_NAME}」フォルダに保存されます")
        logger.info("=" * 80)

        while True:
            try:
                self.scan_once()
            except Exception as e:
                logger.error(f"❌ 監視処理エラー: {e}")
            time.sleep(self.poll_interval)


def main() -> None:
    """監視サービスのエントリーポイント"""
    watch_dir = Path(os.environ.get("WATCH_DIR", "data/inbox"))
    model_name = os.environ.get("WHISPER_MODEL", "medium")
    poll_interval = float(os.environ.get("POLL_INTERVAL", "5"))

    watcher = FolderWatcher(watch_dir, model_name=model_name, poll_interval=poll_interval)
    watcher.run()


if __name__ == "__main__":
    main()
