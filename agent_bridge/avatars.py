from __future__ import annotations

from typing import Any

from .validation import ValidationError


AVATAR_CATALOG: tuple[dict[str, str], ...] = (
    {"key": "auto", "label": "自动匹配", "mark": "✦", "family": "neutral"},
    {"key": "mint", "label": "薄荷", "mark": "M", "family": "neutral"},
    {"key": "violet", "label": "星紫", "mark": "V", "family": "neutral"},
    {"key": "gpt", "label": "GPT", "mark": "G", "family": "openai"},
    {"key": "claude", "label": "Claude", "mark": "C", "family": "anthropic"},
    {"key": "deepseek", "label": "DeepSeek", "mark": "鲸", "family": "deepseek"},
    {"key": "qwen", "label": "Qwen", "mark": "Q", "family": "qwen"},
    {"key": "gemini", "label": "Gemini", "mark": "✦", "family": "google"},
    {"key": "grok", "label": "Grok", "mark": "X", "family": "xai"},
    {"key": "kimi", "label": "Kimi", "mark": "月", "family": "moonshot"},
    {"key": "minimax", "label": "MiniMax", "mark": "M", "family": "minimax"},
    {"key": "doubao", "label": "豆包", "mark": "豆", "family": "bytedance"},
    {"key": "glm", "label": "GLM", "mark": "智", "family": "zhipu"},
    {"key": "llama", "label": "Llama", "mark": "L", "family": "meta"},
    {"key": "mistral", "label": "Mistral", "mark": "M", "family": "mistral"},
    {"key": "pi", "label": "Pi", "mark": "π", "family": "inflection"},
)

AVATAR_KEYS = frozenset(item["key"] for item in AVATAR_CATALOG)


def normalize_avatar_key(value: object) -> str:
    key = str(value or "auto").strip().lower()
    if key not in AVATAR_KEYS:
        raise ValidationError("avatar_key is not in the built-in avatar catalog")
    return key


def avatar_catalog_payload() -> dict[str, Any]:
    return {"avatars": [dict(item) for item in AVATAR_CATALOG]}
