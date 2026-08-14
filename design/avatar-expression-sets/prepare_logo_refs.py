#!/usr/bin/env python3
"""Create centered, legible PNG references from the existing exact SVG marks."""

from __future__ import annotations

import sys
import re
import subprocess
import tempfile
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "avatar-vendor-books" / "logos"
sys.path.insert(0, str(ROOT / "avatar-vendor-books"))

from build_sheet import load_logo  # noqa: E402


VENDORS = {
    "deepseek": "deepseek-color",
    "gpt": "openai",
    "claude": "claude-color",
    "grok": "xai",
    "gemini": "gemini-color",
    "kimi": "kimi-color",
    "minimax": "minimax-color",
    "glm": "chatglm-color",
    "qwen": "qwen-color",
}


def main() -> None:
    output_dir = Path(__file__).resolve().parent / "logo-refs"
    output_dir.mkdir(parents=True, exist_ok=True)
    for vendor, source_name in VENDORS.items():
        svg_path = (
            Path(__file__).resolve().parent / "source-logos" / f"{source_name}.svg"
            if vendor == "claude"
            else SOURCE_DIR / f"{source_name}.svg"
        )
        source = svg_path.read_text(encoding="utf-8")
        source = re.sub(r'height="1em"', 'height="512"', source, count=1)
        source = re.sub(r'width="1em"', 'width="512"', source, count=1)
        with tempfile.TemporaryDirectory(prefix="avatar-logo-") as temp_name:
            temp_dir = Path(temp_name)
            render_svg = temp_dir / f"{source_name}.svg"
            render_svg.write_text(source, encoding="utf-8")
            subprocess.run(
                ["qlmanage", "-t", "-s", "512", "-o", temp_name, str(render_svg)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            rendered_path = temp_dir / f"{source_name}.svg.png"
            logo = load_logo(svg_path, rendered_path)
        scale = min(360 / logo.width, 360 / logo.height)
        logo = logo.resize(
            (max(1, round(logo.width * scale)), max(1, round(logo.height * scale))),
            Image.Resampling.LANCZOS,
        )
        canvas = Image.new("RGBA", (512, 512), "white")
        canvas.alpha_composite(
            logo,
            ((canvas.width - logo.width) // 2, (canvas.height - logo.height) // 2),
        )
        canvas.convert("RGB").save(output_dir / f"{vendor}.png", optimize=True)


if __name__ == "__main__":
    main()
