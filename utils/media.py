"""Media inspection shared by publishers and the public media server."""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlsplit

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".mp4"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif"}
VIDEO_EXTENSIONS = {".mp4"}
DEFAULT_MAX_IMAGE_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_VIDEO_BYTES = 200 * 1024 * 1024


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
