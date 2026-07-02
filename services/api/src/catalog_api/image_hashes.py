from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

import imagehash
from PIL import Image, UnidentifiedImageError


class PerceptualHashError(Exception):
    """Raised when a normalized image cannot be perceptually hashed."""


@dataclass(frozen=True)
class PerceptualHashes:
    phash: str
    dhash: str


def compute_perceptual_hashes(normalized_image_bytes: bytes) -> PerceptualHashes:
    try:
        with Image.open(BytesIO(normalized_image_bytes)) as image:
            image.load()
            return PerceptualHashes(
                phash=_lower_hex_hash(imagehash.phash(image, hash_size=8)),
                dhash=_lower_hex_hash(imagehash.dhash(image, hash_size=8)),
            )
    except (OSError, UnidentifiedImageError) as error:
        raise PerceptualHashError(
            "The normalized image could not be perceptually hashed."
        ) from error


def _lower_hex_hash(hash_value: imagehash.ImageHash) -> str:
    value = str(hash_value).lower()
    if len(value) != 16 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise PerceptualHashError("Perceptual hash output was not 16-character hex.")
    return value
