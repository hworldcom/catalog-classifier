from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from uuid import UUID

from PIL import Image, ImageOps, UnidentifiedImageError

JPEG_CONTENT_TYPE = "image/jpeg"
NORMALIZED_LONGEST_EDGE_MAX = 1024
THUMBNAIL_LONGEST_EDGE_MAX = 480
SUPPORTED_SOURCE_MODES = {"RGB", "L", "CMYK"}


class TerminalImageProcessingError(Exception):
    def __init__(self, *, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


@dataclass(frozen=True)
class DerivedImageKeys:
    normalized_object_key: str
    inference_object_key: str
    thumbnail_object_key: str


@dataclass(frozen=True)
class ProcessedImageDerivatives:
    sha256: str
    width: int
    height: int
    normalized_format: str
    normalized_size_bytes: int
    normalized_bytes: bytes
    inference_bytes: bytes
    thumbnail_bytes: bytes


def derived_image_keys(
    *,
    organization_id: UUID,
    batch_id: UUID,
    pipeline_version: str,
    image_id: UUID,
) -> DerivedImageKeys:
    prefix = (
        f"organizations/{organization_id}/batches/{batch_id}/"
        f"derived/{pipeline_version}/{image_id}"
    )
    return DerivedImageKeys(
        normalized_object_key=f"{prefix}/normalized.jpg",
        inference_object_key=f"{prefix}/inference.jpg",
        thumbnail_object_key=f"{prefix}/thumbnail.jpg",
    )


def process_original_image(original_bytes: bytes) -> ProcessedImageDerivatives:
    original_sha256 = sha256(original_bytes).hexdigest()
    try:
        with Image.open(BytesIO(original_bytes)) as source_image:
            source_image.load()
            if (
                source_image.format != "JPEG"
                or source_image.mode not in SUPPORTED_SOURCE_MODES
            ):
                raise TerminalImageProcessingError(
                    error_code="unsupported_image_mode",
                    message="The source image uses an unsupported image mode.",
                )

            normalized_image = ImageOps.exif_transpose(source_image).convert("RGB")
            normalized_bytes = _jpeg_bytes(normalized_image)
            inference_bytes = _resized_jpeg_bytes(
                normalized_image,
                max_longest_edge=NORMALIZED_LONGEST_EDGE_MAX,
            )
            thumbnail_bytes = _resized_jpeg_bytes(
                normalized_image,
                max_longest_edge=THUMBNAIL_LONGEST_EDGE_MAX,
            )
            width, height = normalized_image.size
    except TerminalImageProcessingError:
        raise
    except (OSError, UnidentifiedImageError) as error:
        raise TerminalImageProcessingError(
            error_code="image_decode_failed",
            message="The source image could not be decoded.",
        ) from error

    return ProcessedImageDerivatives(
        sha256=original_sha256,
        width=width,
        height=height,
        normalized_format=JPEG_CONTENT_TYPE,
        normalized_size_bytes=len(normalized_bytes),
        normalized_bytes=normalized_bytes,
        inference_bytes=inference_bytes,
        thumbnail_bytes=thumbnail_bytes,
    )


def _resized_jpeg_bytes(
    image: Image.Image,
    *,
    max_longest_edge: int,
) -> bytes:
    resized = image.copy()
    resized.thumbnail(
        (max_longest_edge, max_longest_edge),
        Image.Resampling.LANCZOS,
    )
    return _jpeg_bytes(resized)


def _jpeg_bytes(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="JPEG", quality=90)
    return output.getvalue()
