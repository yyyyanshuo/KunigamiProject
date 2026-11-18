"""Visible disclosure watermark for newly generated chat images."""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


WATERMARK_ASSET = (
    Path(__file__).resolve().parents[1] / "static" / "sakura_ai_watermark.png"
)


def _resampling_lanczos():
    return getattr(Image, "Resampling", Image).LANCZOS


def _build_watermark_badge(image_width: int, image_height: int) -> Image.Image:
    """Render public brand text for new images; never alter the original logo."""
    # An ASCII brand keeps rendering portable without a CJK font dependency.
    width = min(max(1, image_width - 24), max(150, round(image_width * 0.23)))
    badge = Image.new("RGBA", (600, 160), (255, 255, 255, 0))
    draw = ImageDraw.Draw(badge)
    draw.rounded_rectangle((0, 0, 599, 159), radius=24, fill=(255, 255, 255, 220))
    font = ImageFont.load_default(size=48)
    draw.text((28, 18), "Saki", font=font, fill=(213, 85, 142, 255))
    draw.text((28, 87), "AI generated", font=ImageFont.load_default(size=28), fill=(120, 62, 89, 255))
    return badge.resize((width, max(1, round(width * 160 / 600))), _resampling_lanczos())


def apply_ai_watermark(image_path: str | os.PathLike[str]) -> bool:
    """Bake the Saki AI disclosure into one newly generated image.

    The source file is replaced only after the completed image is successfully
    encoded, so callers never upload a partially written asset.
    """

    path = Path(image_path)
    temp_path = path.with_name(f".{path.stem}.watermark{path.suffix}")
    try:
        with Image.open(path) as source:
            source.load()
            has_alpha = source.mode in ("RGBA", "LA") or "transparency" in source.info
            canvas = source.convert("RGBA")
            width, height = canvas.size
            if width < 32 or height < 32:
                return False

            badge = _build_watermark_badge(width, height)
            margin = max(9, round(min(width, height) * 0.016))
            left = max(0, width - margin - badge.width)
            top = max(0, height - margin - badge.height)
            canvas.alpha_composite(badge, (left, top))

            save_kwargs = {}
            suffix = path.suffix.lower()
            if suffix in (".jpg", ".jpeg"):
                result = canvas.convert("RGB")
                save_kwargs.update(format="JPEG", quality=92, optimize=True)
            elif suffix == ".png":
                result = canvas if has_alpha else canvas.convert("RGB")
                save_kwargs.update(format="PNG", optimize=True)
            else:
                result = canvas
                save_kwargs["format"] = source.format or "PNG"
            result.save(temp_path, **save_kwargs)

        os.replace(temp_path, path)
        return True
    except Exception as exc:
        print(f"--- [AI Watermark] 水印写入失败: {path.name}: {exc} ---")
        return False
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass
