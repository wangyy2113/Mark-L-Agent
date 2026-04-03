"""Download and compress images/whiteboards from Feishu cloud documents.

Uses lark-oapi Drive API (doc media) and Board API (whiteboard export).
Reuses the compression pipeline from core/media.py to keep images under
the SDK JSON buffer limit.

Public API:
- fetch_and_compress(resource_token, resource_type) → (base64_str, mime_type) | None
"""

import base64
import io
import logging
import uuid
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

_MEDIA_DIR = Path("data/media")
_MAX_IMAGE_BYTES = 500_000  # ~500KB raw → ~667KB base64


def _get_client():
    """Reuse the lark.Client singleton from core.card."""
    from core.card import _get_client as _card_get_client
    return _card_get_client()


def _ensure_dir() -> None:
    _MEDIA_DIR.mkdir(parents=True, exist_ok=True)


# ── Download functions ──

def download_media(file_token: str) -> bytes | None:
    """Download a doc-embedded image/file via Drive API.

    Works for file_tokens and image_tokens from fetch-doc results.
    Endpoint: GET /open-apis/drive/v1/medias/:file_token/download
    """
    try:
        from lark_oapi.api.drive.v1 import DownloadMediaRequest
        req = DownloadMediaRequest.builder().file_token(file_token).build()
        resp = _get_client().drive.v1.media.download(req)
        if not resp.success():
            logger.warning("Drive media download failed for %s: %s %s", file_token, resp.code, resp.msg)
            return None
        return resp.file.read()
    except Exception:
        logger.exception("Error downloading media %s", file_token)
        return None


def download_whiteboard(whiteboard_id: str) -> bytes | None:
    """Download a whiteboard rendered as an image via Board API.

    Endpoint: GET /open-apis/board/v1/whiteboards/:whiteboard_id/download_as_image
    """
    try:
        from lark_oapi.api.board.v1 import DownloadAsImageWhiteboardRequest
        req = DownloadAsImageWhiteboardRequest.builder().whiteboard_id(whiteboard_id).build()
        resp = _get_client().board.v1.whiteboard.download_as_image(req)
        if not resp.success():
            logger.warning("Whiteboard download failed for %s: %s %s", whiteboard_id, resp.code, resp.msg)
            return None
        return resp.file.read()
    except Exception:
        logger.exception("Error downloading whiteboard %s", whiteboard_id)
        return None


# ── Compression ──

def compress_image_bytes(data: bytes, max_bytes: int = _MAX_IMAGE_BYTES) -> tuple[bytes, str]:
    """Compress raw image bytes, return (compressed_bytes, mime_type).

    Uses the same three-stage strategy as core/media._compress_image:
    1. Optimized PNG (if already small enough)
    2. JPEG at decreasing quality (85 → 60 → 40)
    3. Progressive dimension scaling (0.75 → 0.5 → 0.35 → 0.25)
    """
    if len(data) <= max_bytes:
        # Try to detect format
        mime = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
        return data, mime

    try:
        img = Image.open(io.BytesIO(data))
    except Exception:
        logger.warning("Cannot open image for compression (%d bytes), returning as-is", len(data))
        return data, "image/png"

    # Stage 1: optimized PNG
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    if buf.tell() <= max_bytes:
        logger.info("Compressed image with optimized PNG (%d bytes)", buf.tell())
        return buf.getvalue(), "image/png"

    # Stage 2: JPEG at decreasing quality
    rgb = img.convert("RGB") if img.mode in ("RGBA", "LA", "P") else img
    for quality in (85, 60, 40):
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=quality, optimize=True)
        if buf.tell() <= max_bytes:
            logger.info("Compressed image -> JPEG q%d (%d bytes)", quality, buf.tell())
            return buf.getvalue(), "image/jpeg"

    # Stage 3: progressive dimension scaling
    best = buf.getvalue()
    for scale in (0.75, 0.5, 0.35, 0.25):
        w, h = int(rgb.width * scale), int(rgb.height * scale)
        if w < 64 or h < 64:
            break
        resized = rgb.resize((w, h), Image.LANCZOS)
        buf = io.BytesIO()
        resized.save(buf, format="JPEG", quality=60, optimize=True)
        best = buf.getvalue()
        if buf.tell() <= max_bytes:
            logger.info("Compressed image -> JPEG %dx%d (%d bytes)", w, h, buf.tell())
            return best, "image/jpeg"

    logger.warning("Could not compress image under %d bytes, best: %d bytes", max_bytes, len(best))
    return best, "image/jpeg"


# ── Public API ──

def fetch_and_compress(resource_token: str, resource_type: str = "media") -> tuple[str, str] | None:
    """Download a Feishu resource and return compressed base64.

    Args:
        resource_token: file_token, image_token, or whiteboard_token.
        resource_type: "media" (default) or "whiteboard".

    Returns:
        (base64_string, mime_type) on success, None on failure.
    """
    if resource_type == "whiteboard":
        data = download_whiteboard(resource_token)
    else:
        data = download_media(resource_token)

    if data is None:
        return None

    compressed, mime = compress_image_bytes(data)
    b64 = base64.b64encode(compressed).decode("ascii")
    logger.info(
        "fetch_and_compress: token=%s type=%s raw=%d compressed=%d mime=%s",
        resource_token[:20], resource_type, len(data), len(compressed), mime,
    )
    return b64, mime
