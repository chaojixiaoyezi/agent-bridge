#!/usr/bin/env python3
"""Combine the nine compact previews into one low-bandwidth review image."""

from __future__ import annotations

from pathlib import Path

from PIL import Image


VENDORS = (
    "deepseek",
    "gpt",
    "claude",
    "grok",
    "gemini",
    "kimi",
    "minimax",
    "glm",
    "qwen",
)


def main() -> None:
    root = Path(__file__).resolve().parent
    previews = root / "previews"
    tile_size = (320, 182)
    canvas = Image.new("RGB", (tile_size[0] * 3, tile_size[1] * 3), "white")
    for index, vendor in enumerate(VENDORS):
        source = Image.open(previews / f"{vendor}-8-expressions.webp").convert("RGB")
        source = source.resize(tile_size, Image.Resampling.LANCZOS)
        canvas.paste(source, ((index % 3) * tile_size[0], (index // 3) * tile_size[1]))
    canvas.save(
        previews / "all-vendors-low-bandwidth.webp",
        "WEBP",
        quality=58,
        method=6,
        optimize=True,
    )


if __name__ == "__main__":
    main()
