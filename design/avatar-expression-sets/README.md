# Unified vendor avatar expression sets

This directory contains the compact, application-ready avatar series requested
for Agent Bridge. Each vendor has one consistent female chibi mascot rendered in
eight expressions. The rendering system is shared across vendors, but the
expression and gesture set is intentionally different for every vendor; these
are not identical expression sheets with palette swaps. The exact per-vendor
slugs are declared in `build_expression_set.py` and preserved in final filenames.

The generated contact sheet is kept under `raw/` for provenance. Individual
application assets are cropped to 192 x 192 WebP under `final/<vendor>/`, and a
small 4 x 2 review sheet is written under `previews/` to avoid sending large
images during review.

Vendor marks are supplied to the generator from `logo-refs/` as visual
references and must appear as an integrated hair clip, earring, brooch, or
collar ornament—not as a floating corner badge.

## Vendors

- DeepSeek
- GPT / OpenAI
- Claude / Anthropic
- Grok / xAI
- Gemini / Google
- Kimi / Moonshot AI
- MiniMax
- GLM / Zhipu AI
- Qwen / Alibaba Cloud

## Output budget

- final avatar: 192 x 192 WebP, quality 78
- review sheet: 512 x 292 WebP, quality 65
