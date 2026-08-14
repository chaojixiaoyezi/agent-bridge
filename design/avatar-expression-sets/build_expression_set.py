#!/usr/bin/env python3
"""Crop a 4 x 2 generated sheet into compact avatar and preview WebP files."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


VENDOR_EXPRESSIONS = {
    "deepseek": (
        "joyful",
        "serene",
        "pondering",
        "surprised",
        "bashful",
        "wink",
        "determined",
        "drowsy",
    ),
    "gpt": (
        "laughing",
        "curious",
        "mischievous",
        "skeptical",
        "determined-fist",
        "relieved",
        "discovery",
        "puffed-cheeks",
    ),
    "claude": (
        "reassuring",
        "chuckle",
        "puzzled",
        "empathetic",
        "explaining",
        "delighted",
        "firm",
        "contented",
    ),
    "grok": (
        "sly-smirk",
        "deadpan",
        "teasing",
        "incredulous",
        "competitive",
        "confused",
        "triumphant",
        "sleepy-side-eye",
    ),
    "gemini": (
        "starry-eyed",
        "dreamy",
        "curious-peek",
        "dazzled",
        "flustered",
        "playful",
        "analytical",
        "celebratory",
    ),
    "kimi": (
        "cozy-smile",
        "moonlit-wonder",
        "listening",
        "secret-shh",
        "quiet-giggle",
        "startled",
        "stubborn-pout",
        "reassuring",
    ),
    "minimax": (
        "energetic-cheer",
        "rhythmic-groove",
        "mischievous",
        "fired-up",
        "bashful",
        "attentive",
        "challenge-grin",
        "exhausted",
    ),
    "glm": (
        "aha-insight",
        "calculating",
        "puzzled",
        "cautious",
        "confident-proof",
        "delighted-discovery",
        "intense-debug",
        "satisfied",
    ),
    "qwen": (
        "cheerful-wave",
        "adventurous",
        "curious-question",
        "playful-pout",
        "proud",
        "amazed",
        "warm-laugh",
        "resolute",
    ),
}
FONT_PATH = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"


def crop_tile(sheet: Image.Image, index: int) -> Image.Image:
    column = index % 4
    row = index // 4
    cell_left = round(column * sheet.width / 4)
    cell_right = round((column + 1) * sheet.width / 4)
    cell_top = round(row * sheet.height / 2)
    cell_bottom = round((row + 1) * sheet.height / 2)
    cell_width = cell_right - cell_left
    cell_height = cell_bottom - cell_top

    # Generated panels are portrait-oriented. Bias the square crop upward so
    # the hair ornament and face stay visible while still retaining shoulders.
    side = min(cell_width, cell_height)
    extra_x = cell_width - side
    extra_y = cell_height - side
    left = cell_left + extra_x // 2
    top = cell_top + round(extra_y * 0.28)
    tile = sheet.crop((left, top, left + side, top + side))

    # Remove the thin grid line while retaining a square source crop.
    inset = max(2, round(side * 0.012))
    tile = tile.crop((inset, inset, side - inset, side - inset))
    return tile


def build(sheet_path: Path, vendor: str, output_root: Path) -> None:
    sheet = Image.open(sheet_path).convert("RGB")
    expressions = VENDOR_EXPRESSIONS[vendor]
    final_dir = output_root / "final" / vendor
    preview_dir = output_root / "previews"
    final_dir.mkdir(parents=True, exist_ok=True)
    preview_dir.mkdir(parents=True, exist_ok=True)

    preview_tile_size = 128
    preview_header = 36
    preview = Image.new(
        "RGB",
        (preview_tile_size * 4, preview_tile_size * 2 + preview_header),
        "#f8fafc",
    )
    draw = ImageDraw.Draw(preview)
    font = ImageFont.truetype(FONT_PATH, 21)
    small_font = ImageFont.truetype(FONT_PATH, 14)
    draw.text((10, 6), f"{vendor.upper()} · 8 EXPRESSIONS", font=font, fill="#172033")

    for index, expression in enumerate(expressions):
        tile = crop_tile(sheet, index)
        avatar = tile.resize((192, 192), Image.Resampling.LANCZOS)
        avatar_path = final_dir / f"{vendor}-{index + 1:02d}-{expression}.webp"
        avatar.save(avatar_path, "WEBP", quality=78, method=6, optimize=True)

        thumbnail = tile.resize(
            (preview_tile_size, preview_tile_size), Image.Resampling.LANCZOS
        )
        x = (index % 4) * preview_tile_size
        y = preview_header + (index // 4) * preview_tile_size
        preview.paste(thumbnail, (x, y))
        draw.rectangle((x + 4, y + 4, x + 27, y + 24), fill=(12, 20, 34, 210))
        draw.text((x + 9, y + 7), str(index + 1), font=small_font, fill="white")

    preview.save(
        preview_dir / f"{vendor}-8-expressions.webp",
        "WEBP",
        quality=65,
        method=6,
        optimize=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sheet", type=Path, required=True)
    parser.add_argument("--vendor", required=True)
    parser.add_argument(
        "--out-root", type=Path, default=Path(__file__).resolve().parent
    )
    args = parser.parse_args()
    build(args.sheet, args.vendor.strip().lower(), args.out_root)


if __name__ == "__main__":
    main()
