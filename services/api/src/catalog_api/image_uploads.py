from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

from fastapi import UploadFile
from PIL import Image, UnidentifiedImageError

MAX_FILES_PER_REQUEST = 20
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class ValidatedJpeg:
    original_filename: str
    size_bytes: int
    content: bytes | None
    error_code: str | None = None
    error_message: str | None = None

    @property
    def is_accepted(self) -> bool:
        return self.content is not None


async def validate_jpeg_upload(upload: UploadFile) -> ValidatedJpeg:
    original_filename = upload.filename or "unnamed"

    try:
        content = await upload.read(MAX_FILE_SIZE_BYTES + 1)
        size_bytes = upload.size if upload.size is not None else len(content)

        if size_bytes > MAX_FILE_SIZE_BYTES or len(content) > MAX_FILE_SIZE_BYTES:
            return ValidatedJpeg(
                original_filename=original_filename,
                size_bytes=size_bytes,
                content=None,
                error_code="file_too_large",
                error_message="JPEG files must be 10 mebibytes or smaller.",
            )

        try:
            with Image.open(BytesIO(content)) as image:
                image_format = image.format
                image.verify()

            with Image.open(BytesIO(content)) as image:
                image.load()
        except (
            Image.DecompressionBombError,
            UnidentifiedImageError,
            OSError,
            SyntaxError,
            ValueError,
        ):
            image_format = None

        if image_format != "JPEG":
            return ValidatedJpeg(
                original_filename=original_filename,
                size_bytes=size_bytes,
                content=None,
                error_code="invalid_jpeg",
                error_message="The file content is not a valid JPEG image.",
            )

        return ValidatedJpeg(
            original_filename=original_filename,
            size_bytes=size_bytes,
            content=content,
        )
    finally:
        await upload.close()

