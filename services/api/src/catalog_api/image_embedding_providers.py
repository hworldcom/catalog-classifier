from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

from catalog_api.embedding_vectors import EMBEDDING_DIMENSIONS
from catalog_api.image_processing import JPEG_CONTENT_TYPE

GEMINI_EMBEDDING_PROVIDER = "google-gemini"
GEMINI_EMBEDDING_MODEL = "gemini-embedding-2"


class ImageEmbeddingProviderError(Exception):
    """Raised when an image embedding cannot be generated."""


class ImageEmbeddingProvider(Protocol):
    provider: str
    model: str
    dimensions: int

    def embed_image(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
    ) -> list[float]:
        """Generate one embedding vector for image bytes."""


@dataclass(frozen=True)
class ImageEmbeddingResult:
    provider: str
    model: str
    dimensions: int
    embedding: list[float]


class GoogleGeminiImageEmbeddingProvider:
    provider = GEMINI_EMBEDDING_PROVIDER
    model = GEMINI_EMBEDDING_MODEL
    dimensions = EMBEDDING_DIMENSIONS

    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                from google import genai
            except ImportError as error:
                raise ImageEmbeddingProviderError(
                    "The google-genai package is not installed."
                ) from error
            self._client = genai.Client()
        return self._client

    def embed_image(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
    ) -> list[float]:
        try:
            from google.genai import types
        except ImportError as error:
            raise ImageEmbeddingProviderError(
                "The google-genai package is not installed."
            ) from error

        try:
            result = self.client.models.embed_content(
                model=self.model,
                contents=[
                    types.Part.from_bytes(
                        data=image_bytes,
                        mime_type=mime_type,
                    ),
                ],
                config=types.EmbedContentConfig(
                    output_dimensionality=self.dimensions,
                ),
            )
            [embedding] = result.embeddings
            values = embedding.values
        except Exception as error:
            raise ImageEmbeddingProviderError(
                "The image embedding provider call failed."
            ) from error

        return [float(value) for value in values]


def generate_image_embedding(
    provider: ImageEmbeddingProvider,
    *,
    image_bytes: bytes,
    mime_type: str = JPEG_CONTENT_TYPE,
) -> ImageEmbeddingResult:
    try:
        embedding = [
            float(value)
            for value in provider.embed_image(
                image_bytes=image_bytes,
                mime_type=mime_type,
            )
        ]
    except ImageEmbeddingProviderError:
        raise
    except Exception as error:
        raise ImageEmbeddingProviderError(
            "The image embedding provider call failed."
        ) from error

    if len(embedding) != EMBEDDING_DIMENSIONS:
        raise ImageEmbeddingProviderError(
            f"Expected {EMBEDDING_DIMENSIONS} embedding dimensions, "
            f"received {len(embedding)}."
        )

    return ImageEmbeddingResult(
        provider=provider.provider,
        model=provider.model,
        dimensions=EMBEDDING_DIMENSIONS,
        embedding=embedding,
    )


@lru_cache
def get_image_embedding_provider() -> ImageEmbeddingProvider:
    return GoogleGeminiImageEmbeddingProvider()
