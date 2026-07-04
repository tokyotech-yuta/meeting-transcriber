# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## プロジェクト概要

完全ローカル実行の日本語向け会議文字起こし＆議事録生成ツール。faster-whisper（音声文字起こし）+ Ollama（議事録生成 LLM・RAG 埋め込み）を組み合わせ、データを外部送信しない。

## コマンド

依存管理は uv。開発依存込みのセットアップは `uv sync --all-groups`。

```bash
# 品質ゲート一式（CI と同じ）
uv run ruff check app/ main.py
uv run ruff format --check app/ main.py
uv run mypy app/ main.py
uv run pytest --no-cov

# 単一テスト実行
uv run pytest tests/test_transcriber.py --no-cov
uv run pytest tests/test_rag.py::TestKnowledgeBase::test_search --no-cov

# pre-commit（ruff / mypy / gitleaks を一括実行）
uv run pre-commit run --all-files

# アプリ起動（対話式メニュー CLI）
uv run python main.py

# 文字起こし単体実行
uv run python -m app.transcriber path/to/audio.wav large-v3
```

注意: pyproject.toml の pytest `addopts` にカバレッジ計測（`--cov=app` + html レポート）が含まれる。普段の開発・単一テストでは `--no-cov` を付けると速い（CI も `--no-cov`）。

## アーキテクチャ

処理は「録音 → 文字起こし → 議事録生成」の 3 段パイプライン。各段は独立したクラスで、`main.py` の対話式メニューが組み合わせる。

- `main.py` — メニュー CLI。`Transcriber` はモデルロードが重いため遅延初期化（メニュー 1/2 の初回選択時に生成、設定変更で破棄）。`MinutesGenerator` の初期化失敗（Ollama 未起動）は警告のみで、録音・文字起こしは継続利用可能にする graceful degradation 設計。
- `app/recorder.py` — `AudioRecorder`。sounddevice でマイク録音（16kHz モノラル、Whisper 最適値）、`data/audio/` に WAV 保存。
- `app/transcriber.py` — `Transcriber`。faster-whisper でローカル文字起こし、`data/transcripts/` に保存。`JAPANESE_MODELS` dict がユーザー向けモデル名 → 実モデル名のマッピング（例: `large-v3-ja` → `kotoba-tech/kotoba-whisper-v1.0-faster`）。faster-whisper は CTranslate2 形式必須のため、HuggingFace Transformers 形式の kotoba-whisper 素の版は使えない — 必ず `-faster` サフィックス版を指定する。
- `app/minutes_generator.py` — `MinutesGenerator`。Ollama chat（デフォルト `qwen2.5:7b`、temperature 0.3）で議事録を Markdown 生成。RAG 検索結果があればシステムプロンプトに「参考情報」として注入する。
- `app/watcher.py` — `FolderWatcher`。監視フォルダ（デフォルト `data/inbox`、環境変数 `WATCH_DIR` で変更）に置かれた音声/テキストを自動処理する常駐サービス。ファイルサイズが2回のスキャンで一致してから処理する（コピー途中の誤処理防止）。結果は「完了」、失敗は「エラー」サブフォルダへ移動。docker-compose の `watcher` サービスとして起動（`docker compose up -d watcher`）。
- `app/ollama_utils.py` — `ollama.list()` のレスポンス形式差異（ライブラリ 0.4 以降のオブジェクト形式 / 旧辞書形式）を吸収するヘルパー。モデル存在チェックはここを経由する。
- `app/webui.py` — Gradio 製 Web UI（ポート 7860、`docker compose up -d webui`）。アップロード → 議事録生成に加え、フォルダ監視分の処理状況（watcher が監視フォルダに書く `.processing_status.json`）を進捗バー付きで表示する。gradio 依存は dependency-group `webui`（Dockerfile で `--group webui` 指定）。mypy は gradio を ignore_missing_imports 扱い。
- `app/rag.py` — `KnowledgeBase`。`data/knowledge/*.md` を見出し単位＋500 文字でチャンク分割し、Ollama の `mxbai-embed-large` で埋め込み、コサイン類似度で top-k 検索。埋め込みは `.rag_cache/embeddings.json` にファイル内容の MD5 ハッシュをキーとしてキャッシュ（ファイル変更時のみ再計算）。
- `app/logger.py` — 全モジュール共通の `setup_logger`。メッセージのみのシンプルフォーマット（絵文字でレベル表現）。`print` ではなく logger を使う。

外部サービス依存は Ollama（localhost:11434）のみで、それも任意。Ollama 未接続でも録音・文字起こしは動く必要がある — 新機能でもこの縮退動作を壊さないこと。

## テスト

テストは faster-whisper / Ollama / マイクを一切使わない設計。`tests/conftest.py` のフィクスチャで `WhisperModel`・Ollama レスポンス・`KnowledgeBase` をモックする。新規テストもネットワーク・音声ハードウェア非依存で書く（CI は Ubuntu ヘッドレス環境で libportaudio2 / libsndfile1 のみインストール）。

## コード規約

- mypy は `disallow_untyped_defs` 有効 — 全関数に型アノテーション必須（テスト含む）
- ruff: line-length 100、double quotes。lint ルールは pyproject.toml 参照
- ログ・docstring・ユーザー向けメッセージは日本語
