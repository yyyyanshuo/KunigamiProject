"""Visible disclosure watermark for newly generated chat images."""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image


WATERMARK_ASSET = (
    Path(__file__).resolve().parents[1] / "static" / "sakura_ai_watermark.png"
)


def _resampling_lanczos():
    return getattr(Image, "Resampling", Image).LANCZOS


def _build_watermark_badge(image_width: int, image_height: int) -> Image.Image:
    """Scale the bundled transparent watermark without altering its design."""

    with Image.open(WATERMARK_ASSET) as source:
        watermark = source.convert("RGBA")

    max_width = max(1, image_width - 24)
    target_width = min(max_width, max(150, round(image_width * 0.23)))
    scale = target_width / watermark.width
    target_height = max(1, round(watermark.height * scale))
    watermark = watermark.resize(
        (target_width, target_height),
        _resampling_lanczos(),
    )
    return watermark


def apply_ai_watermark(image_path: str | os.PathLike[str]) -> bool:
    """Bake the Sakura AI disclosure into one newly generated image.

    The source file is replaced only after the completed image is successfully
    encoded, so callers never upload a partially written asset.
    """

    path = Path(image_path)
    temp_path = path.with_name(f".{path.stem}.watermark{path.suffix}")
    try:
        if not WATERMARK_ASSET.is_file():
            raise FileNotFoundError(f"水印素材不存在: {WATERMARK_ASSET}")

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
