"""Web UIモジュール - ブラウザから音声/テキストをアップロードして議事録を生成"""

import os
from datetime import datetime
from pathlib import Path
from typing import Any

import gradio as gr

from app.logger import setup_logger
from app.minutes_generator import MinutesGenerator
from app.transcriber import Transcriber
from app.watcher import (
    AUDIO_EXTENSIONS,
    DONE_DIR_NAME,
    ERROR_DIR_NAME,
    TEXT_EXTENSIONS,
    read_status,
)

logger = setup_logger(__name__)

OUTPUT_DIR = Path("data/transcripts")
WATCH_DIR = Path(os.environ.get("WATCH_DIR", "data/inbox"))


def parse_auth(value: str) -> tuple[str, str] | None:
    """環境変数WEBUI_AUTH（user:password形式）を認証タプルに変換する"""
    if ":" not in value:
        return None
    user, password = value.split(":", 1)
    if not user or not password:
        return None
    return (user, password)


def _list_recent(directory: Path, pattern: str, limit: int = 10) -> list[str]:
    """ディレクトリ内の該当ファイルを更新日時の新しい順に列挙する"""
    if not directory.exists():
        return []
    files = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        f"{p.name}（{datetime.fromtimestamp(p.stat().st_mtime).strftime('%m/%d %H:%M')}）"
        for p in files[:limit]
    ]


def _progress_bar(fraction: float, width: int = 18) -> str:
    """0〜1の進捗率をテキストバーにする"""
    fraction = min(max(fraction, 0.0), 1.0)
    filled = round(fraction * width)
    return "▓" * filled + "░" * (width - filled)


def _minutes_since(iso_time: str) -> float | None:
    """ISO形式時刻からの経過分数を返す（解釈できなければNone）"""
    try:
        return (datetime.now() - datetime.fromisoformat(iso_time)).total_seconds() / 60
    except (ValueError, TypeError):
        return None


def _render_current(current: dict[str, Any]) -> str:
    """処理中ファイルの表示ブロックを組み立てる"""
    stage = current.get("stage", "処理中")
    started = current.get("stage_started_at") or current.get("started_at") or ""
    elapsed_min = _minutes_since(started)

    lines = [f"**{current.get('file')}**"]

    progress = current.get("progress") or {}
    done = progress.get("done_sec")
    total = progress.get("total_sec")
    if done and total:
        fraction = done / total
        lines.append(f"{stage} {_progress_bar(fraction)} {fraction:.0%}")
        parts = []
        if elapsed_min is not None:
            parts.append(f"経過 {elapsed_min:.0f}分")
            if fraction > 0.02:
                remaining = elapsed_min * (1 - fraction) / fraction
                parts.append(f"残り約{max(remaining, 1):.0f}分")
        parts.append(f"会議 {total / 60:.1f}分中 {done / 60:.1f}分処理済")
        lines.append(" ｜ ".join(parts))
    else:
        suffix = f"（経過 {elapsed_min:.0f}分）" if elapsed_min is not None else ""
        lines.append(f"{stage}{suffix}")

    return "### 🔄 処理中\n" + "  \n".join(lines)


def render_watch_status() -> str:
    """フォルダ監視の処理状況をMarkdownで返す（Web UIの状況表示用）"""
    if not WATCH_DIR.exists():
        return "監視フォルダがまだ作成されていません。"

    status = read_status(WATCH_DIR)
    current = status.get("current")
    current_file = current.get("file") if current else None

    waiting = sorted(
        p.name
        for p in WATCH_DIR.iterdir()
        if p.is_file()
        and not p.name.startswith((".", "~$"))
        and p.suffix.lower() in AUDIO_EXTENSIONS | TEXT_EXTENSIONS
        and p.name != current_file
    )

    lines: list[str] = []
    has_status = status.get("updated_at") is not None

    if current:
        lines.append(_render_current(current))
        if waiting:
            lines.append("### ⏳ 待機中\n" + "\n".join(f"- {name}" for name in waiting))
    elif waiting and not has_status:
        # watcherが状況ファイル未対応（旧版）または未起動の場合は処理中かどうか判別できない
        lines.append("### 🔄 処理中または待機中\n" + "\n".join(f"- {name}" for name in waiting))
    elif waiting:
        lines.append("### ⏳ 待機中\n" + "\n".join(f"- {name}" for name in waiting))
    else:
        lines.append("現在処理中のファイルはありません。")

    last = status.get("last")
    if last:
        icon = "✅" if last.get("result") == "完了" else "❌"
        lines.append(
            f"直近の結果: {icon} {last.get('file')} → {last.get('result')}"
            f"（{last.get('finished_at', '')}）"
        )

    done = _list_recent(WATCH_DIR / DONE_DIR_NAME, "*_議事録.md")
    if done:
        lines.append("### ✅ 最近の完了\n" + "\n".join(f"- {name}" for name in done))

    errors = _list_recent(WATCH_DIR / ERROR_DIR_NAME, "*_エラー.txt")
    if errors:
        lines.append("### ❌ エラー\n" + "\n".join(f"- {name}" for name in errors))

    lines.append(f"（表示更新: {datetime.now().strftime('%H:%M:%S')}）")
    return "\n\n".join(lines)


class WebUI:
    """アップロードされたファイルを処理するWeb UIバックエンド"""

    def __init__(self, model_name: str = "medium") -> None:
        """
        Args:
            model_name: Whisperモデル名（small/medium/large-v3/large-v3-ja）
        """
        self.model_name = model_name

        # Transcriberはモデルロードが重いため初回の音声処理時に遅延初期化
        self._transcriber: Transcriber | None = None

        self.minutes_gen: MinutesGenerator | None = None
        try:
            self.minutes_gen = MinutesGenerator()
        except Exception as e:
            logger.warning(f"⚠️  議事録生成は無効です（Ollama未接続）: {e}")

    def _get_transcriber(self) -> Transcriber:
        if self._transcriber is None:
            self._transcriber = Transcriber(model_name=self.model_name)
        return self._transcriber

    def process(
        self,
        file_path: str | None,
        meeting_title: str,
        additional_context: str,
        progress: Any = gr.Progress(track_tqdm=True),
    ) -> tuple[str, str, list[str]]:
        """アップロードされたファイルから議事録を生成する

        Args:
            file_path: アップロードファイルのパス（gradioが一時保存したもの）
            meeting_title: 会議タイトル（空文字可）
            additional_context: 参加者情報など（空文字可）
            progress: gradioの進捗表示

        Returns:
            (議事録Markdown, 状況メッセージ, ダウンロードファイルのパス一覧)
        """
        if not file_path:
            raise gr.Error("ファイルを選択してください。")

        path = Path(file_path)
        suffix = path.suffix.lower()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = f"{path.stem}_{timestamp}"
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        downloads: list[str] = []

        if suffix in AUDIO_EXTENSIONS:
            progress(0.1, desc="文字起こし中...")
            transcriber = self._get_transcriber()
            transcript, segments = transcriber.transcribe(path)
            transcript_path = OUTPUT_DIR / f"{stem}_文字起こし.txt"
            transcriber.save_transcript(transcript, segments, output_path=transcript_path)
            downloads.append(str(transcript_path))
            if not transcript.strip():
                return (
                    "",
                    "⚠️ 音声から文字を検出できませんでした（無音または非対応の音声）。",
                    downloads,
                )
        elif suffix in TEXT_EXTENSIONS:
            transcript = path.read_text(encoding="utf-8")
        else:
            raise gr.Error(
                f"対応していないファイル形式です: {suffix}\n"
                f"音声（{' '.join(sorted(AUDIO_EXTENSIONS))}）"
                f"またはテキスト（{' '.join(sorted(TEXT_EXTENSIONS))}）を指定してください。"
            )

        if self.minutes_gen is None:
            if downloads:
                return "", "⚠️ 議事録生成が利用できません（文字起こしのみ保存しました）。", downloads
            raise gr.Error("議事録生成が利用できません（Ollama未接続）。")

        progress(0.6, desc="議事録を生成中...")
        minutes_path = OUTPUT_DIR / f"{stem}_議事録.md"
        minutes, _ = self.minutes_gen.generate_and_save(
            transcript,
            meeting_title=meeting_title or None,
            additional_context=additional_context or None,
            output_path=minutes_path,
        )
        downloads.append(str(minutes_path))

        return minutes, "✅ 議事録を作成しました。下のボタンからダウンロードできます。", downloads


def build_app(ui: WebUI) -> Any:
    """gradioアプリを構築する"""
    with gr.Blocks(title="会議文字起こし＆議事録作成") as demo:
        gr.Markdown("# 🎙️ 会議文字起こし＆議事録作成")
        gr.Markdown(
            "会議の録音・録画（.wav / .mp3 / .m4a / .mp4 / .mov など。Zoom/Teams録画も可）"
            "または文字起こしテキスト（.txt / .md）をアップロードしてください。"
            "処理はすべて社内マシンの中で完結し、外部には送信されません。"
        )
        with gr.Row():
            with gr.Column():
                file_input = gr.File(label="会議の音声または文字起こしファイル", type="filepath")
                title_input = gr.Textbox(label="会議タイトル（省略可）")
                context_input = gr.Textbox(label="参加者情報など（省略可）")
                submit = gr.Button("議事録を作成", variant="primary")
            with gr.Column():
                status = gr.Textbox(label="状況", interactive=False)
                downloads = gr.Files(label="ダウンロード", interactive=False)
        minutes_preview = gr.Markdown(label="議事録プレビュー")

        with gr.Accordion("📊 フォルダ投入分の処理状況", open=True):
            watch_status = gr.Markdown(render_watch_status())
            refresh = gr.Button("状況を更新")

        submit.click(
            ui.process,
            inputs=[file_input, title_input, context_input],
            outputs=[minutes_preview, status, downloads],
        )
        refresh.click(render_watch_status, outputs=[watch_status])
        demo.load(render_watch_status, outputs=[watch_status])

        # gr.Timerがある版では10秒ごとに自動更新する
        timer_cls = getattr(gr, "Timer", None)
        if timer_cls is not None:
            timer = timer_cls(10)
            timer.tick(render_watch_status, outputs=[watch_status])
    return demo


def main() -> None:
    """Web UIサーバーのエントリーポイント"""
    ui = WebUI(model_name=os.environ.get("WHISPER_MODEL", "medium"))
    app = build_app(ui)
    # CPU実行のため同時処理は1件に制限し、複数ユーザーは順番待ちにする
    app.queue(default_concurrency_limit=1).launch(
        server_name="0.0.0.0",
        server_port=int(os.environ.get("WEBUI_PORT", "7860")),
        auth=parse_auth(os.environ.get("WEBUI_AUTH", "")),
    )


if __name__ == "__main__":
    main()
