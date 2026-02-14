"""Media processing tools for Amplifier Hive sessions.

Provides image analysis (via Claude vision API) and PDF text extraction
(via pypdf for text-based PDFs, Tesseract OCR for scanned PDFs).

These tools are mounted on each session post-creation alongside the
Slack tools, giving every conversation permanent access to media processing.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Supported image formats for vision analysis
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif"}
MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
}

# Max image size to send to vision API (20MB)
MAX_IMAGE_SIZE = 20 * 1024 * 1024


class ImageAnalyzerTool:
    """Analyze images using Claude's vision capabilities.

    Sends images to the Anthropic Claude API as base64-encoded vision requests
    and returns detailed descriptions, categorizations, or brief summaries.
    """

    @property
    def name(self) -> str:
        return "analyze_image"

    @property
    def description(self) -> str:
        return (
            "Analyze an image file and describe its contents. "
            "Works with JPG, PNG, GIF, WebP, and BMP files. "
            "Can provide brief summaries, detailed descriptions, or "
            "categorization data (filename suggestions, categories, subjects). "
            "Use this when the user uploads an image or asks about image contents."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "image_path": {
                    "type": "string",
                    "description": "Path to the image file to analyze",
                },
                "detail_level": {
                    "type": "string",
                    "enum": ["brief", "detailed", "categorization"],
                    "description": (
                        "Level of detail: 'brief' for one-sentence summary, "
                        "'detailed' for comprehensive description, "
                        "'categorization' for filename/category/subjects JSON"
                    ),
                },
                "question": {
                    "type": "string",
                    "description": (
                        "Optional specific question to ask about the image "
                        "(overrides detail_level)"
                    ),
                },
            },
            "required": ["image_path"],
        }

    async def execute(self, input: dict[str, Any]) -> Any:
        """Analyze an image and return description."""
        from amplifier_core.models import ToolResult

        image_path = input.get("image_path", "")
        detail_level = input.get("detail_level", "detailed")
        question = input.get("question")

        path = Path(image_path).expanduser().resolve()
        if not path.exists():
            return ToolResult(
                success=False, output=f"Image file not found: {image_path}"
            )

        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            return ToolResult(
                success=False,
                output=f"Unsupported image format: {path.suffix}. "
                f"Supported: {', '.join(sorted(IMAGE_EXTENSIONS))}",
            )

        if path.stat().st_size > MAX_IMAGE_SIZE:
            return ToolResult(
                success=False,
                output=f"Image too large ({path.stat().st_size // 1024 // 1024}MB). "
                f"Max size: {MAX_IMAGE_SIZE // 1024 // 1024}MB",
            )

        # Read and encode
        try:
            image_data = base64.b64encode(path.read_bytes()).decode("utf-8")
        except Exception as e:
            return ToolResult(success=False, output=f"Failed to read image: {e}")

        media_type = MEDIA_TYPES.get(path.suffix.lower(), "image/jpeg")

        # Build prompt
        if question:
            prompt = question
        else:
            prompts = {
                "brief": "Briefly describe what you see in this image in one sentence.",
                "detailed": (
                    "Provide a detailed description of this image, including: "
                    "objects present, people (if any), text content, colors, "
                    "setting/scene, and any notable features."
                ),
                "categorization": (
                    "Analyze this image for file organization purposes. "
                    "Provide a JSON response with: "
                    "1) 'filename_suggestion': A brief descriptive name suitable "
                    "for a filename (lowercase, underscores, no spaces, max 50 chars), "
                    "2) 'category': A category for organizing (e.g., 'screenshots', "
                    "'photos', 'diagrams', 'documents', 'memes'), "
                    "3) 'subjects': Array of key subjects or topics (2-5 items). "
                    "Only output valid JSON, no other text."
                ),
            }
            prompt = prompts.get(detail_level, prompts["detailed"])

        # Call Anthropic vision API
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return ToolResult(
                success=False, output="ANTHROPIC_API_KEY environment variable not set"
            )

        try:
            result = await _call_vision_api(api_key, image_data, media_type, prompt)
            return ToolResult(success=True, output=result)
        except Exception as e:
            return ToolResult(success=False, output=f"Vision API error: {e}")


class PDFExtractorTool:
    """Extract text content from PDF files.

    Uses pypdf for text-based PDFs (fast, accurate) and falls back to
    Tesseract OCR for scanned PDFs (slower, best-effort).
    """

    @property
    def name(self) -> str:
        return "extract_pdf_text"

    @property
    def description(self) -> str:
        return (
            "Extract text content from a PDF file. "
            "Handles both text-based PDFs (fast) and scanned/image PDFs (via OCR). "
            "Can extract all pages or a specific page range. "
            "Use this when the user uploads a PDF or asks about PDF contents."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "pdf_path": {
                    "type": "string",
                    "description": "Path to the PDF file",
                },
                "start_page": {
                    "type": "integer",
                    "description": "First page to extract (1-based, default: 1)",
                },
                "end_page": {
                    "type": "integer",
                    "description": (
                        "Last page to extract (1-based, inclusive, default: all pages)"
                    ),
                },
                "ocr_fallback": {
                    "type": "boolean",
                    "description": (
                        "If true (default), use Tesseract OCR when text extraction "
                        "yields little content (for scanned PDFs)"
                    ),
                },
            },
            "required": ["pdf_path"],
        }

    async def execute(self, input: dict[str, Any]) -> Any:
        """Extract text from a PDF file."""
        from amplifier_core.models import ToolResult

        pdf_path = input.get("pdf_path", "")
        start_page = input.get("start_page", 1)
        end_page = input.get("end_page")
        ocr_fallback = input.get("ocr_fallback", True)

        path = Path(pdf_path).expanduser().resolve()
        if not path.exists():
            return ToolResult(success=False, output=f"PDF file not found: {pdf_path}")

        if path.suffix.lower() != ".pdf":
            return ToolResult(success=False, output=f"Not a PDF file: {path.suffix}")

        # Try text extraction with pypdf first
        try:
            text, total_pages = _extract_text_pypdf(path, start_page, end_page)
        except Exception as e:
            return ToolResult(success=False, output=f"Failed to read PDF: {e}")

        # Check if we got meaningful text
        stripped = text.strip()
        words = len(stripped.split()) if stripped else 0
        pages_extracted = (end_page or total_pages) - start_page + 1

        # Heuristic: if less than ~10 words per page, it's likely scanned
        is_likely_scanned = words < (pages_extracted * 10)

        if is_likely_scanned and ocr_fallback:
            logger.info(
                "PDF appears scanned (%d words for %d pages), trying OCR",
                words,
                pages_extracted,
            )
            try:
                ocr_text = _extract_text_ocr(path, start_page, end_page)
                if ocr_text.strip():
                    header = (
                        f"[Extracted via OCR from {path.name} "
                        f"({total_pages} pages total)]\n\n"
                    )
                    return ToolResult(success=True, output=header + ocr_text)
            except Exception as e:
                logger.warning("OCR fallback failed: %s", e)
                # Fall through to return whatever pypdf got

        if not stripped:
            method_note = ""
            if not ocr_fallback:
                method_note = " (OCR disabled -- enable ocr_fallback for scanned PDFs)"
            return ToolResult(
                success=False,
                output=f"No text could be extracted from {path.name}. "
                f"The PDF may be image-only or encrypted.{method_note}",
            )

        header = (
            f"[Extracted from {path.name} "
            f"(pages {start_page}-{end_page or total_pages} of {total_pages})]\n\n"
        )
        return ToolResult(success=True, output=header + text)


# -- Internal helpers --


async def _call_vision_api(
    api_key: str, image_data: str, media_type: str, prompt: str
) -> str:
    """Call the Anthropic Claude vision API."""
    import urllib.request
    import urllib.error

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    data = {
        "model": "claude-3-haiku-20240307",
        "max_tokens": 2048,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_data,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        headers=headers,
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
            return result["content"][0]["text"]
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        raise RuntimeError(f"API error {e.code}: {error_body}") from e


def _extract_text_pypdf(
    path: Path, start_page: int = 1, end_page: int | None = None
) -> tuple[str, int]:
    """Extract text from a PDF using pypdf. Returns (text, total_pages)."""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    total_pages = len(reader.pages)

    # Clamp page range
    start_idx = max(0, start_page - 1)
    end_idx = min(total_pages, end_page or total_pages)

    pages_text = []
    for i in range(start_idx, end_idx):
        page_text = reader.pages[i].extract_text() or ""
        if page_text.strip():
            pages_text.append(f"--- Page {i + 1} ---\n{page_text}")

    return "\n\n".join(pages_text), total_pages


def _extract_text_ocr(
    path: Path, start_page: int = 1, end_page: int | None = None
) -> str:
    """Extract text from a scanned PDF using Tesseract OCR.

    Converts PDF pages to images, then runs Tesseract on each.
    Requires: tesseract, Pillow, pytesseract.
    """
    from pypdf import PdfReader

    # Determine page count for range validation
    reader = PdfReader(str(path))
    total_pages = len(reader.pages)
    start_idx = max(0, start_page - 1)
    end_idx = min(total_pages, end_page or total_pages)

    # Try pdf-to-image conversion
    # Method: use pdftoppm if available (common on Linux), otherwise
    # try to extract embedded images from the PDF
    pages_text = []

    # Check if pdftoppm is available (poppler-utils)
    pdftoppm = _which("pdftoppm")
    if pdftoppm:
        pages_text = _ocr_with_pdftoppm(path, pdftoppm, start_idx + 1, end_idx)
    else:
        # Fallback: try to extract images directly from PDF pages
        pages_text = _ocr_from_pdf_images(reader, start_idx, end_idx)

    return "\n\n".join(pages_text)


def _which(cmd: str) -> str | None:
    """Find a command on PATH."""
    try:
        result = subprocess.run(
            ["which", cmd], capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None


def _ocr_with_pdftoppm(
    pdf_path: Path, pdftoppm: str, first_page: int, last_page: int
) -> list[str]:
    """Convert PDF pages to images with pdftoppm, then OCR each."""
    import tempfile
    from PIL import Image
    import pytesseract

    pages_text = []
    with tempfile.TemporaryDirectory() as tmpdir:
        # Convert pages to PNG
        subprocess.run(
            [
                pdftoppm,
                "-png",
                "-r",
                "300",
                "-f",
                str(first_page),
                "-l",
                str(last_page),
                str(pdf_path),
                f"{tmpdir}/page",
            ],
            capture_output=True,
            timeout=120,
        )

        # OCR each generated image
        for img_path in sorted(Path(tmpdir).glob("page-*.png")):
            try:
                image = Image.open(img_path)
                text = pytesseract.image_to_string(image)
                if text.strip():
                    # Extract page number from filename (page-01.png)
                    page_num = img_path.stem.split("-")[-1].lstrip("0") or "1"
                    pages_text.append(f"--- Page {page_num} ---\n{text}")
            except Exception as e:
                logger.warning("OCR failed for %s: %s", img_path.name, e)

    return pages_text


def _ocr_from_pdf_images(reader: Any, start_idx: int, end_idx: int) -> list[str]:
    """Extract embedded images from PDF and OCR them (fallback method)."""
    import io
    from PIL import Image
    import pytesseract

    pages_text = []
    for i in range(start_idx, end_idx):
        page = reader.pages[i]
        images = page.images if hasattr(page, "images") else []

        page_parts = []
        for img in images:
            try:
                image = Image.open(io.BytesIO(img.data))
                text = pytesseract.image_to_string(image)
                if text.strip():
                    page_parts.append(text)
            except Exception as e:
                logger.debug("Could not OCR image on page %d: %s", i + 1, e)

        if page_parts:
            pages_text.append(f"--- Page {i + 1} ---\n" + "\n".join(page_parts))

    return pages_text


def create_media_tools() -> list:
    """Create all media processing tools.

    Returns:
        List of tool instances to mount on a session.
    """
    return [
        ImageAnalyzerTool(),
        PDFExtractorTool(),
    ]
