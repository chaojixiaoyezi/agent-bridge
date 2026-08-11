from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Iterable


OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
SHA256_RE = re.compile(r"^[a-fA-F0-9]{64}$")

MAX_ALIAS_CHARS = 160
MAX_CLIENT_IDENTITY_CHARS = 96
MAX_AGENT_USERNAME_CHARS = 48
MAX_BODY_CHARS = 65_536
MAX_REFS = 32
MAX_REF_PATH_CHARS = 4_096
MAX_CONVERSATION_ID_CHARS = 128
CONVERSATION_RESERVED_CHARS = {"/", "\\", "?", "#", "%"}


class ValidationError(ValueError):
    """One model/client supplied field violated the bridge data contract."""


def client_identity(value: str) -> str:
    """Validate a composed product-username identity such as codex-小可爱."""
    result = str(value or "").strip()
    if not result or len(result) > MAX_CLIENT_IDENTITY_CHARS:
        raise ValidationError(
            f"client_type must be 1-{MAX_CLIENT_IDENTITY_CHARS} characters"
        )
    if any(
        ord(character) < 32
        or character.isspace()
        or character in {"/", "\\"}
        for character in result
    ):
        raise ValidationError(
            "client_type cannot contain whitespace, control characters, or slashes"
        )
    if "-" not in result or result.startswith("-") or result.endswith("-"):
        raise ValidationError("client_type must use the product-username format")
    return result


def agent_username(value: str) -> str:
    result = str(value or "").strip()
    if not result or len(result) > MAX_AGENT_USERNAME_CHARS:
        raise ValidationError(
            f"username must be 1-{MAX_AGENT_USERNAME_CHARS} characters"
        )
    if any(
        ord(character) < 32
        or character.isspace()
        or character in {"/", "\\"}
        for character in result
    ):
        raise ValidationError(
            "username cannot contain whitespace, control characters, or slashes"
        )
    return result


def product_username(product: str, username: str) -> str:
    normalized_product = token(product, field="product_name")
    normalized_username = agent_username(username)
    return client_identity(f"{normalized_product}-{normalized_username}")


def opaque_id(value: str, *, field: str) -> str:
    result = str(value or "").strip()
    if not OPAQUE_ID_RE.fullmatch(result):
        raise ValidationError(
            f"{field} must be 1-128 ASCII URL-safe characters"
        )
    return result


def conversation_id(value: str) -> str:
    """Validate a human-facing room name without weakening internal IDs."""
    result = unicodedata.normalize("NFC", str(value or "").strip())
    if not result or len(result) > MAX_CONVERSATION_ID_CHARS:
        raise ValidationError(
            f"conversation_id must be 1-{MAX_CONVERSATION_ID_CHARS} characters"
        )
    if result in {".", ".."}:
        raise ValidationError("conversation_id cannot be . or ..")
    for character in result:
        category = unicodedata.category(character)
        if (
            character in CONVERSATION_RESERVED_CHARS
            or category.startswith("C")
            or category in {"Zl", "Zp"}
        ):
            raise ValidationError(
                "conversation_id cannot contain /, \\, ?, #, %, or control characters"
            )
    return result


def token(value: str, *, field: str) -> str:
    result = str(value or "").strip()
    if not TOKEN_RE.fullmatch(result):
        raise ValidationError(
            f"{field} must be 1-64 ASCII URL-safe characters"
        )
    return result


def alias(value: str, *, field: str = "session_alias") -> str:
    result = str(value or "").strip()
    if not result or len(result) > MAX_ALIAS_CHARS:
        raise ValidationError(f"{field} must be 1-{MAX_ALIAS_CHARS} characters")
    if any(ord(char) < 32 for char in result):
        raise ValidationError(f"{field} cannot contain control characters")
    return result


def body(value: str) -> str:
    result = str(value or "")
    if not result.strip():
        raise ValidationError("body cannot be empty")
    if len(result) > MAX_BODY_CHARS:
        raise ValidationError(f"body exceeds {MAX_BODY_CHARS} characters")
    return result


def string_tokens(values: Iterable[str] | None, *, field: str) -> list[str]:
    if values is None:
        return []
    result: list[str] = []
    for value in values:
        normalized = token(value, field=field)
        if normalized not in result:
            result.append(normalized)
    if len(result) > 32:
        raise ValidationError(f"{field} accepts at most 32 values")
    return result


def message_refs(refs: Iterable[dict[str, Any]] | None) -> list[dict[str, str]]:
    if refs is None:
        return []
    result: list[dict[str, str]] = []
    for raw in refs:
        if not isinstance(raw, dict):
            raise ValidationError("each ref must be an object")
        path = str(raw.get("path") or "").strip()
        if not path or len(path) > MAX_REF_PATH_CHARS:
            raise ValidationError("ref.path is empty or too long")
        if any(ord(char) < 32 for char in path):
            raise ValidationError("ref.path cannot contain control characters")
        digest = str(raw.get("sha256") or "").strip().lower()
        if digest and not SHA256_RE.fullmatch(digest):
            raise ValidationError("ref.sha256 must be a 64-character hex digest")
        label = str(raw.get("label") or "").strip()
        item = {"path": path}
        if digest:
            item["sha256"] = digest
        if label:
            item["label"] = alias(label, field="ref.label")
        result.append(item)
    if len(result) > MAX_REFS:
        raise ValidationError(f"refs accepts at most {MAX_REFS} entries")
    return result


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
