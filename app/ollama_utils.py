"""Ollamaクライアントの補助関数"""

from typing import Any

import ollama


def list_model_names() -> list[str]:
    """取得済みOllamaモデル名の一覧を返す

    ollamaライブラリ0.4以降はレスポンスが属性アクセスのオブジェクト形式
    （モデル名は `model` 属性）、それ以前は辞書形式（`name` キー）のため両対応する。
    """
    response: Any = ollama.list()
    models = response.get("models", []) if isinstance(response, dict) else response.models

    names: list[str] = []
    for m in models:
        name = m.get("model") or m.get("name") if isinstance(m, dict) else getattr(m, "model", None)
        if name:
            names.append(str(name))
    return names


def is_model_available(model: str, available_models: list[str]) -> bool:
    """モデルが取得済みか判定する（タグ省略時は :latest も同一視）"""
    return model in available_models or f"{model}:latest" in available_models
