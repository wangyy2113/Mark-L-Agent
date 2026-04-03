"""Download images and files from Feishu messages to local temp storage."""

import io
import logging
import time
from pathlib import Path

from lark_oapi.api.im.v1 import GetMessageResourceRequest
from PIL import Image

logger = logging.getLogger(__name__)

_MEDIA_DIR = Path("data/media")
_MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB
_MAX_IMAGE_BYTES = 500_000  # ~500KB raw → ~667KB base64 → fits in 1MB SDK JSON buffer with overhead
_CLEANUP_AGE = 2 * 3600  # 2 hours
_last_cleanup: float = 0.0


def _get_client():
    """Reuse the lark.Client singleton from core.card."""
    from core.card import _get_client as _card_get_client
    return _card_get_client()


def _ensure_dir() -> None:
    """Create media directory if it doesn't exist."""
    _MEDIA_DIR.mkdir(parents=True, exist_ok=True)


def _lazy_cleanup() -> None:
    """Remove files older than _CLEANUP_AGE. Runs at most once per 10 minutes."""
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < 600:
        return
    _last_cleanup = now

    if not _MEDIA_DIR.exists():
        return

    cutoff = now - _CLEANUP_AGE
    removed = 0
    for f in _MEDIA_DIR.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    if removed:
        logger.info("Cleaned up %d old media files", removed)


def _compress_image(path: Path, max_bytes: int = _MAX_IMAGE_BYTES) -> Path:
    """Compress an image file so it stays under max_bytes.

    Returns the (possibly renamed) path — extension may change from .png to .jpg
    if JPEG conversion was needed.
    """
    if path.stat().st_size <= max_bytes:
        return path

    try:
        img = Image.open(path)
    except Exception:
        logger.warning("Cannot open %s for compression, leaving as-is", path)
        return path

    # Strategy 1: re-save as optimized PNG
    if path.suffix.lower() == ".png":
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        if buf.tell() <= max_bytes:
            path.write_bytes(buf.getvalue())
            logger.info("Compressed %s with optimized PNG (%d bytes)", path.name, buf.tell())
            return path

    # Strategy 2: convert to JPEG at decreasing quality
    # Drop alpha channel for JPEG
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGB")

    jpg_path = path.with_suffix(".jpg")
    for quality in (85, 60, 40):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        if buf.tell() <= max_bytes:
            jpg_path.write_bytes(buf.getvalue())
            if jpg_path != path:
                path.unlink(missing_ok=True)
            logger.info("Compressed %s -> JPEG q%d (%d bytes)", path.name, quality, buf.tell())
            return jpg_path

    # Strategy 3: progressively scale down dimensions
    for scale in (0.75, 0.5, 0.35, 0.25):
        w, h = int(img.width * scale), int(img.height * scale)
        if w < 64 or h < 64:
            break
        resized = img.resize((w, h), Image.LANCZOS)
        buf = io.BytesIO()
        resized.save(buf, format="JPEG", quality=60, optimize=True)
        if buf.tell() <= max_bytes:
            jpg_path.write_bytes(buf.getvalue())
            if jpg_path != path:
                path.unlink(missing_ok=True)
            logger.info("Compressed %s -> JPEG %dx%d (%d bytes)", path.name, w, h, buf.tell())
            return jpg_path

    # Last resort: keep the smallest we got
    jpg_path.write_bytes(buf.getvalue())
    if jpg_path != path:
        path.unlink(missing_ok=True)
    logger.warning("Could not compress %s under %d bytes, best: %d bytes", path.name, max_bytes, buf.tell())
    return jpg_path


def _download_resource(message_id: str, file_key: str, resource_type: str) -> bytes | None:
    """Download a resource from a Feishu message via the message resource API.

    Args:
        message_id: The message containing the resource.
        file_key: The image_key or file_key of the resource.
        resource_type: "image" or "file".

    Returns raw bytes on success, None on failure.
    """
    try:
        req = GetMessageResourceRequest.builder() \
            .message_id(message_id) \
            .file_key(file_key) \
            .type(resource_type) \
            .build()
        resp = _get_client().im.v1.message_resource.get(req)
        if not resp.success():
            logger.warning("Failed to download %s %s: %s %s", resource_type, file_key, resp.code, resp.msg)
            return None
        return resp.file.read()
    except Exception:
        logger.exception("Error downloading %s %s", resource_type, file_key)
        return None


def download_image(message_id: str, image_key: str) -> str | None:
    """Download an image from a Feishu message and save to local storage.

    Returns the absolute path on success, None on failure.
    """
    _ensure_dir()
    _lazy_cleanup()

    dest = _MEDIA_DIR / f"{image_key}.png"
    # Skip if already downloaded (dedup by key — may be .png or .jpg after compression)
    if dest.exists():
        return str(dest.resolve())
    dest_jpg = dest.with_suffix(".jpg")
    if dest_jpg.exists():
        return str(dest_jpg.resolve())

    data = _download_resource(message_id, image_key, "image")
    if data is None:
        return None

    if len(data) > _MAX_FILE_SIZE:
        logger.warning("Image %s too large (%d bytes), skipping", image_key, len(data))
        return None

    dest.write_bytes(data)
    logger.info("Downloaded image %s (%d bytes) -> %s", image_key, len(data), dest)
    dest = _compress_image(dest)
    return str(dest.resolve())


def download_file(message_id: str, file_key: str, file_name: str = "") -> str | None:
    """Download a file from a Feishu message and save to local storage.

    Returns the absolute path on success, None on failure.
    """
    _ensure_dir()
    _lazy_cleanup()

    # Build filename: {file_key}_{original_name} for readability
    safe_name = file_name.replace("/", "_").replace("\\", "_") if file_name else "file"
    dest = _MEDIA_DIR / f"{file_key}_{safe_name}"
    # Skip if already downloaded
    if dest.exists():
        return str(dest.resolve())

    data = _download_resource(message_id, file_key, "file")
    if data is None:
        return None

    if len(data) > _MAX_FILE_SIZE:
        logger.warning("File %s too large (%d bytes), skipping", file_key, len(data))
        return None

    dest.write_bytes(data)
    logger.info("Downloaded file %s (%d bytes) -> %s", file_key, len(data), dest)
    return str(dest.resolve())
