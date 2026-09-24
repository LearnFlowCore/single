"""Media inspection shared by publishers and the public media server."""

from __future__ import annotations

import mimetypes
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator
from urllib.parse import unquote, urlsplit

from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

register_heif_opener()

SUPPORTED_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
    ".avif",
    ".mp4",
}
IMAGE_EXTENSIONS = SUPPORTED_EXTENSIONS - {".mp4"}
VIDEO_EXTENSIONS = {".mp4"}
DEFAULT_MAX_IMAGE_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_VIDEO_BYTES = 200 * 1024 * 1024
MAX_PUBLISH_IMAGE_EDGE = 1440


@dataclass(frozen=True, slots=True)
class MediaInfo:
    source: str | Path
    extension: str
    mime_type: str
    kind: str
    path: Path | None
    size: int | None


def is_remote_url(value: str | Path) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def validate_media(
    media: Iterable[str | Path],
    *,
    max_files: int = 10,
    allow_remote: bool = False,
    allowed_extensions: set[str] | None = None,
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    max_video_bytes: int = DEFAULT_MAX_VIDEO_BYTES,
) -> list[MediaInfo]:
    """Validate media names, local existence, count, and practical file sizes."""

    items = list(media)
    if len(items) > max_files:
        raise ValueError(f"At most {max_files} media files are allowed")

    allowed = allowed_extensions or SUPPORTED_EXTENSIONS
    result: list[MediaInfo] = []
    for item in items:
        remote = is_remote_url(item)
        if remote:
            if not allow_remote:
                raise ValueError("Remote media URLs are not supported by this platform")
            path_name = unquote(urlsplit(str(item)).path)
            extension = Path(path_name).suffix.lower()
            local_path = None
            size = None
        else:
            local_path = Path(item).expanduser().resolve()
            extension = local_path.suffix.lower()
            if not local_path.is_file():
                raise ValueError(f"Media file does not exist: {item}")
            size = local_path.stat().st_size

        if extension not in allowed:
            supported = ", ".join(sorted(ext.lstrip(".") for ext in allowed))
            raise ValueError(f"Unsupported media format '{extension or 'unknown'}'; use {supported}")

        kind = "video" if extension in VIDEO_EXTENSIONS else "image"
        limit = max_video_bytes if kind == "video" else max_image_bytes
        if size is not None and size > limit:
            raise ValueError(
                f"Media file is too large: {item} ({size / 1024 / 1024:.1f} MB; "
                f"maximum {limit / 1024 / 1024:.0f} MB)"
            )
        mime_type = mimetypes.types_map.get(extension) or (
            "video/mp4" if kind == "video" else "application/octet-stream"
        )
        result.append(MediaInfo(item, extension, mime_type, kind, local_path, size))
    return result


@contextmanager
def prepare_media_for_publish(
    media: Iterable[str | Path], *, for_instagram: bool = False
) -> Iterator[list[str]]:
    """Convert local images to API-safe files while preserving videos and remote URLs."""

    source_items = validate_media(media, allow_remote=True)
    with tempfile.TemporaryDirectory(prefix="other-world-media-") as directory:
        prepared: list[str] = []
        for index, item in enumerate(source_items):
            if item.path is None or item.kind == "video":
                prepared.append(str(item.source))
                continue
            if item.extension == ".gif" and not for_instagram:
                _verify_image(item.path)
                prepared.append(str(item.path))
                continue
            target = Path(directory) / f"image-{index + 1}.jpg"
            _convert_image(item.path, target, fit_instagram=for_instagram)
            prepared.append(str(target))
        yield prepared


def _verify_image(path: Path) -> None:
    try:
        with Image.open(path) as image:
            image.verify()
    except (OSError, UnidentifiedImageError) as exc:
        raise ValueError(f"Файл не является корректным изображением: {path.name}") from exc


def _convert_image(source: Path, target: Path, *, fit_instagram: bool) -> None:
    try:
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened)
            image.seek(0)
            image.load()
            image = _flatten_to_rgb(image)
            image.thumbnail(
                (MAX_PUBLISH_IMAGE_EDGE, MAX_PUBLISH_IMAGE_EDGE),
                Image.Resampling.LANCZOS,
            )
            if fit_instagram:
                image = _pad_instagram_ratio(image)
            image.save(target, "JPEG", quality=90, optimize=True, progressive=True)
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise ValueError(f"Не удалось подготовить изображение {source.name}: {exc}") from exc


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    if image.mode == "RGB":
        return image.copy()
    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        return Image.alpha_composite(background, rgba).convert("RGB")
    return image.convert("RGB")


def _pad_instagram_ratio(image: Image.Image) -> Image.Image:
    width, height = image.size
    if width <= 0 or height <= 0:
        raise ValueError("изображение имеет нулевой размер")
    ratio = width / height
    if 0.8 <= ratio <= 1.91:
        return image
    if ratio < 0.8:
        canvas_size = (round(height * 0.8), height)
    else:
        canvas_size = (width, round(width / 1.91))
    canvas = Image.new("RGB", canvas_size, "white")
    canvas.paste(image, ((canvas.width - width) // 2, (canvas.height - height) // 2))
    return canvas
