from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol, Sequence

from catalog_api.image_processing import JPEG_CONTENT_TYPE

GEMINI_CATEGORY_PROVIDER = "google-gemini"
GEMINI_CATEGORY_MODEL_ENV = "CATALOG_CATEGORY_MODEL"
DEFAULT_GEMINI_CATEGORY_MODEL = "gemini-2.5-flash"


class CategorySuggestionProviderError(Exception):
    """Raised when a category suggestion cannot be generated or parsed."""


class CategorySuggestionProvider(Protocol):
    provider: str
    model: str

    def suggest_category(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        category_slugs: Sequence[str],
    ) -> str | dict[str, object]:
        """Return one structured category suggestion for image bytes."""


@dataclass(frozen=True)
class CategorySuggestionResult:
    provider: str
    model: str
    category_slug: str | None
    confidence: float
    raw_response: dict[str, object]


class GoogleGeminiCategorySuggestionProvider:
    provider = GEMINI_CATEGORY_PROVIDER

    def __init__(
        self,
        *,
        model: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.model = model or os.getenv(
            GEMINI_CATEGORY_MODEL_ENV,
            DEFAULT_GEMINI_CATEGORY_MODEL,
        )
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                from google import genai
            except ImportError as error:
                raise CategorySuggestionProviderError(
                    "The google-genai package is not installed."
                ) from error
            self._client = genai.Client()
        return self._client

    def suggest_category(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        category_slugs: Sequence[str],
    ) -> str:
        try:
            from google.genai import types
        except ImportError as error:
            raise CategorySuggestionProviderError(
                "The google-genai package is not installed."
            ) from error

        prompt = (
            "Classify this catalog product image into one primary category. "
            "Return only JSON with fields categorySlug and confidence. "
            "Use one of these category slugs when possible: "
            f"{', '.join(category_slugs)}. "
            "If no category fits, return categorySlug as unknown."
        )
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=[
                    prompt,
                    types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                ),
            )
            text = response.text
        except Exception as error:
            raise CategorySuggestionProviderError(
                "The category suggestion provider call failed."
            ) from error

        if not isinstance(text, str) or not text.strip():
            raise CategorySuggestionProviderError(
                "The category suggestion provider returned an empty response."
            )
        return text


def generate_category_suggestion(
    provider: CategorySuggestionProvider,
    *,
    image_bytes: bytes,
    category_slugs: Sequence[str],
    mime_type: str = JPEG_CONTENT_TYPE,
) -> CategorySuggestionResult:
    try:
        raw_provider_response = provider.suggest_category(
            image_bytes=image_bytes,
            mime_type=mime_type,
            category_slugs=category_slugs,
        )
        raw_response = _parse_raw_response(raw_provider_response)
        confidence = _parse_confidence(raw_response)
    except CategorySuggestionProviderError:
        raise
    except Exception as error:
        raise CategorySuggestionProviderError(
            "The category suggestion provider call failed."
        ) from error

    category_slug = _parse_optional_slug(raw_response)
    return CategorySuggestionResult(
        provider=provider.provider,
        model=provider.model,
        category_slug=category_slug,
        confidence=confidence,
        raw_response=raw_response,
    )


def _parse_raw_response(raw_response: str | dict[str, object]) -> dict[str, object]:
    if isinstance(raw_response, dict):
        return raw_response
    if not isinstance(raw_response, str):
        raise CategorySuggestionProviderError(
            "The category suggestion provider returned an unsupported response."
        )

    try:
        parsed_response = json.loads(raw_response)
    except json.JSONDecodeError as error:
        raise CategorySuggestionProviderError(
            "The category suggestion provider returned malformed JSON."
        ) from error

    if not isinstance(parsed_response, dict):
        raise CategorySuggestionProviderError(
            "The category suggestion provider returned non-object JSON."
        )
    return parsed_response


def _parse_confidence(raw_response: dict[str, object]) -> float:
    raw_confidence = raw_response.get("confidence")
    if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, int | float):
        raise CategorySuggestionProviderError(
            "The category suggestion provider returned invalid confidence."
        )

    confidence = float(raw_confidence)
    if math.isnan(confidence) or math.isinf(confidence):
        raise CategorySuggestionProviderError(
            "The category suggestion provider returned invalid confidence."
        )
    if confidence < 0 or confidence > 1:
        raise CategorySuggestionProviderError(
            "The category suggestion provider returned invalid confidence."
        )
    return confidence


def _parse_optional_slug(raw_response: dict[str, object]) -> str | None:
    raw_slug = raw_response.get("categorySlug")
    if not isinstance(raw_slug, str):
        return None

    slug = raw_slug.strip()
    return slug or None


@lru_cache
def get_category_suggestion_provider() -> CategorySuggestionProvider:
    return GoogleGeminiCategorySuggestionProvider()
