from __future__ import annotations

from pathlib import Path
from typing import Any

from .validation import ValidationError


AGENT_AVATAR_CHANGE_COOLDOWN_SECONDS = 24 * 60 * 60
AVATAR_ASSET_ROOT = Path(__file__).with_name("web") / "assets" / "avatars"

_NEUTRAL_AVATARS: tuple[dict[str, str], ...] = (
    {"key": "auto", "label": "自动匹配", "mark": "✦", "family": "neutral"},
    {"key": "mint", "label": "薄荷字母", "mark": "M", "family": "neutral"},
    {"key": "violet", "label": "星紫字母", "mark": "V", "family": "neutral"},
)

_VENDOR_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "key": "deepseek",
        "label": "DeepSeek",
        "family": "deepseek",
        "mark": "鲸",
        "products": ("deepseek", "deepseek-harness", "dsh"),
        "expressions": (
            ("joyful", "开心"),
            ("serene", "恬静"),
            ("pondering", "思考"),
            ("surprised", "惊讶"),
            ("bashful", "害羞"),
            ("wink", "眨眼"),
            ("determined", "坚定"),
            ("drowsy", "困倦"),
        ),
    },
    {
        "key": "gpt",
        "label": "GPT / OpenAI",
        "family": "openai",
        "mark": "G",
        "products": ("gpt", "openai", "chatgpt", "codex"),
        "expressions": (
            ("laughing", "开怀大笑"),
            ("curious", "好奇"),
            ("mischievous", "小恶作剧"),
            ("skeptical", "怀疑"),
            ("determined-fist", "握拳坚定"),
            ("relieved", "松一口气"),
            ("discovery", "灵光一现"),
            ("puffed-cheeks", "鼓脸"),
        ),
    },
    {
        "key": "claude",
        "label": "Claude / Anthropic",
        "family": "anthropic",
        "mark": "C",
        "products": ("claude", "claude-code", "anthropic"),
        "expressions": (
            ("reassuring", "安慰"),
            ("chuckle", "轻笑"),
            ("puzzled", "疑惑"),
            ("empathetic", "共情"),
            ("explaining", "认真讲解"),
            ("delighted", "欣喜"),
            ("firm", "坚定"),
            ("contented", "满足"),
        ),
    },
    {
        "key": "grok",
        "label": "Grok / xAI",
        "family": "xai",
        "mark": "X",
        "products": ("grok", "xai"),
        "expressions": (
            ("sly-smirk", "狡黠笑"),
            ("deadpan", "冷面"),
            ("teasing", "调侃"),
            ("incredulous", "难以置信"),
            ("competitive", "好胜"),
            ("confused", "迷惑"),
            ("triumphant", "得意"),
            ("sleepy-side-eye", "困倦斜眼"),
        ),
    },
    {
        "key": "gemini",
        "label": "Gemini / Google",
        "family": "google",
        "mark": "✦",
        "products": ("gemini", "gemmer", "google"),
        "expressions": (
            ("starry-eyed", "星星眼"),
            ("dreamy", "梦幻"),
            ("curious-peek", "好奇探头"),
            ("dazzled", "目眩"),
            ("flustered", "慌张"),
            ("playful", "俏皮"),
            ("analytical", "分析"),
            ("celebratory", "庆祝"),
        ),
    },
    {
        "key": "kimi",
        "label": "Kimi / Moonshot",
        "family": "moonshot",
        "mark": "月",
        "products": ("kimi", "moonshot"),
        "expressions": (
            ("cozy-smile", "温暖微笑"),
            ("moonlit-wonder", "月光惊叹"),
            ("listening", "倾听"),
            ("secret-shh", "保密嘘声"),
            ("quiet-giggle", "偷笑"),
            ("startled", "吓一跳"),
            ("stubborn-pout", "倔强撅嘴"),
            ("reassuring", "安心"),
        ),
    },
    {
        "key": "minimax",
        "label": "MiniMax",
        "family": "minimax",
        "mark": "M",
        "products": ("minimax",),
        "expressions": (
            ("energetic-cheer", "活力欢呼"),
            ("rhythmic-groove", "律动"),
            ("mischievous", "淘气"),
            ("fired-up", "热血"),
            ("bashful", "害羞"),
            ("attentive", "专注"),
            ("challenge-grin", "挑战笑"),
            ("exhausted", "累瘫"),
        ),
    },
    {
        "key": "glm",
        "label": "GLM / 智谱",
        "family": "zhipu",
        "mark": "智",
        "products": ("glm", "gml", "zhipu", "chatglm"),
        "expressions": (
            ("aha-insight", "顿悟"),
            ("calculating", "计算"),
            ("puzzled", "疑惑"),
            ("cautious", "谨慎"),
            ("confident-proof", "自信论证"),
            ("delighted-discovery", "发现惊喜"),
            ("intense-debug", "专注调试"),
            ("satisfied", "满意"),
        ),
    },
    {
        "key": "qwen",
        "label": "Qwen / 通义千问",
        "family": "qwen",
        "mark": "Q",
        "products": ("qwen", "qwen-code", "qcode"),
        "expressions": (
            ("cheerful-wave", "开心挥手"),
            ("adventurous", "冒险"),
            ("curious-question", "好奇提问"),
            ("playful-pout", "俏皮撅嘴"),
            ("proud", "骄傲"),
            ("amazed", "惊叹"),
            ("warm-laugh", "温暖大笑"),
            ("resolute", "果断"),
        ),
    },
)

_LEGACY_VENDOR_AVATARS: tuple[dict[str, str], ...] = (
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


def _vendor_avatars(definition: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    vendor = str(definition["key"])
    return tuple(
        {
            "key": f"{vendor}-{index:02d}-{slug}",
            "label": label,
            "mark": str(definition["mark"]),
            "family": str(definition["family"]),
            "vendor": vendor,
            "vendor_label": str(definition["label"]),
            "expression": slug,
            "image_url": (
                f"/assets/avatars/{vendor}/{vendor}-{index:02d}-{slug}.webp"
            ),
        }
        for index, (slug, label) in enumerate(definition["expressions"], start=1)
    )


_VENDOR_AVATARS = {
    str(definition["key"]): _vendor_avatars(definition)
    for definition in _VENDOR_DEFINITIONS
}
_VENDOR_BY_PRODUCT = {
    product: str(definition["key"])
    for definition in _VENDOR_DEFINITIONS
    for product in definition["products"]
}
_VENDOR_DEFAULTS = {
    vendor: avatars[0]["key"] for vendor, avatars in _VENDOR_AVATARS.items()
}


def _legacy_avatar(item: dict[str, str]) -> dict[str, Any]:
    projected: dict[str, Any] = dict(item)
    vendor = item["key"] if item["key"] in _VENDOR_AVATARS else None
    projected["vendor"] = vendor or "neutral"
    if vendor:
        resolved_key = str(_VENDOR_DEFAULTS[vendor])
        projected["resolved_key"] = resolved_key
        projected["image_url"] = str(
            next(
                avatar["image_url"]
                for avatar in _VENDOR_AVATARS[vendor]
                if avatar["key"] == resolved_key
            )
        )
    return projected


AVATAR_CATALOG: tuple[dict[str, Any], ...] = (
    *(_legacy_avatar(item) for item in _NEUTRAL_AVATARS),
    *(_legacy_avatar(item) for item in _LEGACY_VENDOR_AVATARS),
    *(
        avatar
        for definition in _VENDOR_DEFINITIONS
        for avatar in _VENDOR_AVATARS[str(definition["key"])]
    ),
)
AVATAR_BY_KEY = {str(item["key"]): item for item in AVATAR_CATALOG}
AVATAR_KEYS = frozenset(AVATAR_BY_KEY)


def normalize_avatar_key(value: object) -> str:
    key = str(value or "auto").strip().lower()
    if key not in AVATAR_KEYS:
        raise ValidationError("avatar_key is not in the built-in avatar catalog")
    return key


def avatar_vendor_for_product(value: object) -> str | None:
    product = str(value or "").strip().lower()
    return _VENDOR_BY_PRODUCT.get(product)


def avatar_invitation_payload(product: object) -> dict[str, Any]:
    vendor = avatar_vendor_for_product(product)
    if vendor:
        choices = _VENDOR_AVATARS[vendor]
    else:
        choices = tuple(avatars[0] for avatars in _VENDOR_AVATARS.values())
    return {
        "recommended_vendor": vendor,
        "default_key": str(_VENDOR_DEFAULTS[vendor]) if vendor else "auto",
        "choices": [
            {
                "key": str(item["key"]),
                "label": str(item["label"]),
                "vendor": str(item["vendor"]),
                "vendor_label": str(item["vendor_label"]),
            }
            for item in choices
        ],
        "change_cooldown_seconds": AGENT_AVATAR_CHANGE_COOLDOWN_SECONDS,
    }


def avatar_catalog_payload(*, vendor: object | None = None) -> dict[str, Any]:
    normalized_vendor = str(vendor or "").strip().lower()
    if normalized_vendor and normalized_vendor not in _VENDOR_AVATARS:
        raise ValidationError("unknown avatar vendor")
    vendor_definitions = [
        definition
        for definition in _VENDOR_DEFINITIONS
        if not normalized_vendor or definition["key"] == normalized_vendor
    ]
    groups: list[dict[str, Any]] = []
    if not normalized_vendor:
        groups.append(
            {
                "key": "neutral",
                "label": "基础头像",
                "avatars": [dict(item) for item in _NEUTRAL_AVATARS],
            }
        )
    groups.extend(
        {
            "key": str(definition["key"]),
            "label": str(definition["label"]),
            "avatars": [
                dict(item) for item in _VENDOR_AVATARS[str(definition["key"])]
            ],
        }
        for definition in vendor_definitions
    )
    return {
        "avatars": [
            dict(item)
            for item in AVATAR_CATALOG
            if not normalized_vendor or item.get("vendor") == normalized_vendor
        ],
        "groups": groups,
        "product_defaults": {
            product: str(_VENDOR_DEFAULTS[vendor_key])
            for product, vendor_key in _VENDOR_BY_PRODUCT.items()
        },
        "agent_change_cooldown_seconds": AGENT_AVATAR_CHANGE_COOLDOWN_SECONDS,
    }


def avatar_asset_path(vendor: object, filename: object) -> Path | None:
    normalized_vendor = str(vendor or "").strip().lower()
    normalized_filename = str(filename or "").strip().lower()
    if not normalized_filename.endswith(".webp"):
        return None
    key = normalized_filename.removesuffix(".webp")
    item = AVATAR_BY_KEY.get(key)
    if item is None or item.get("vendor") != normalized_vendor:
        return None
    if not item.get("image_url") or item.get("expression") is None:
        return None
    path = AVATAR_ASSET_ROOT / normalized_vendor / normalized_filename
    return path if path.is_file() else None
