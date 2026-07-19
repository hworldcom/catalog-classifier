from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol
from uuid import UUID

from catalog_api.image_processing import JPEG_CONTENT_TYPE

GEMINI_MULTIMODAL_PROVIDER = "google-gemini"
GEMINI_MULTIMODAL_MODEL_ENV = "CATALOG_MULTIMODAL_MODEL"
DEFAULT_GEMINI_MULTIMODAL_MODEL = "gemini-2.5-flash"
ALLOWED_MULTIMODAL_DECISIONS = {
    "same_product",
    "different_product",
    "uncertain",
}


class MultimodalComparisonProviderError(Exception):
    """Raised when a multimodal comparison cannot be generated or parsed."""


@dataclass(frozen=True)
class MultimodalPairInput:
    batch_id: UUID
    image_a_id: UUID
    image_b_id: UUID
    image_a_filename: str
    image_b_filename: str
    embedding_similarity: float
    phash_distance: int | None
    category_match: bool | None
    suggested_category_a: str | None
    suggested_category_b: str | None
    pipeline_version: str


class MultimodalComparisonProvider(Protocol):
    provider: str
    model: str

    def compare_pair(
        self,
        *,
        pair: MultimodalPairInput,
        image_a_bytes: bytes,
        image_b_bytes: bytes,
        mime_type: str,
        timeout_seconds: int,
    ) -> str | dict[str, object]:
        """Return one constrained same-product comparison."""


@dataclass(frozen=True)
class MultimodalComparisonResult:
    provider: str
    model: str
    decision: str
    confidence: float
    reason: str
    raw_response: dict[str, object]


class GoogleGeminiMultimodalComparisonProvider:
    provider = GEMINI_MULTIMODAL_PROVIDER

    def __init__(
        self,
        *,
        model: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.model = model or os.getenv(
            GEMINI_MULTIMODAL_MODEL_ENV,
            DEFAULT_GEMINI_MULTIMODAL_MODEL,
        )
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                from google import genai
            except ImportError as error:
                raise MultimodalComparisonProviderError(
                    "The google-genai package is not installed."
                ) from error
            self._client = genai.Client()
        return self._client

    def compare_pair(
        self,
        *,
        pair: MultimodalPairInput,
        image_a_bytes: bytes,
        image_b_bytes: bytes,
        mime_type: str,
        timeout_seconds: int,
    ) -> str:
        try:
            from google.genai import types
        except ImportError as error:
            raise MultimodalComparisonProviderError(
                "The google-genai package is not installed."
            ) from error

        context = {
            "batchId": str(pair.batch_id),
            "imageAId": str(pair.image_a_id),
            "imageBId": str(pair.image_b_id),
            "imageAFilename": pair.image_a_filename,
            "imageBFilename": pair.image_b_filename,
            "embeddingSimilarity": pair.embedding_similarity,
            "phashDistance": pair.phash_distance,
            "categoryMatch": pair.category_match,
            "suggestedCategoryA": pair.suggested_category_a,
            "suggestedCategoryB": pair.suggested_category_b,
            "pipelineVersion": pair.pipeline_version,
        }
        prompt = (
            "Determine whether these two catalog photos show the same sellable "
            "clothing product. Different angles, backgrounds, or lighting may "
            "still be the same product. Different construction, pattern, logo, "
            "or design means a different product. Return only JSON with "
            "decision, confidence, and reason. decision must be same_product, "
            "different_product, or uncertain. confidence must be from 0 to 1. "
            f"Deterministic context: {json.dumps(context, sort_keys=True)}"
        )
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=[
                    prompt,
                    "Image A:",
                    types.Part.from_bytes(data=image_a_bytes, mime_type=mime_type),
                    "Image B:",
                    types.Part.from_bytes(data=image_b_bytes, mime_type=mime_type),
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    http_options=types.HttpOptions(
                        timeout=timeout_seconds * 1000,
                    ),
                ),
            )
            text = response.text
        except Exception as error:
            raise MultimodalComparisonProviderError(
                "The multimodal comparison provider call failed."
            ) from error

        if not isinstance(text, str) or not text.strip():
            raise MultimodalComparisonProviderError(
                "The multimodal comparison provider returned an empty response."
            )
        return text


def generate_multimodal_comparison(
    provider: MultimodalComparisonProvider,
    *,
    pair: MultimodalPairInput,
    image_a_bytes: bytes,
    image_b_bytes: bytes,
    timeout_seconds: int,
    mime_type: str = JPEG_CONTENT_TYPE,
) -> MultimodalComparisonResult:
    try:
        raw_provider_response = provider.compare_pair(
            pair=pair,
            image_a_bytes=image_a_bytes,
            image_b_bytes=image_b_bytes,
            mime_type=mime_type,
            timeout_seconds=timeout_seconds,
        )
        raw_response = _parse_raw_response(raw_provider_response)
        decision = _parse_decision(raw_response)
        confidence = _parse_confidence(raw_response)
        reason = _parse_reason(raw_response)
    except MultimodalComparisonProviderError:
        raise
    except Exception as error:
        raise MultimodalComparisonProviderError(
            "The multimodal comparison provider call failed."
        ) from error

    return MultimodalComparisonResult(
        provider=provider.provider,
        model=provider.model,
        decision=decision,
        confidence=confidence,
        reason=reason,
        raw_response=raw_response,
    )


def _parse_raw_response(raw_response: str | dict[str, object]) -> dict[str, object]:
    if isinstance(raw_response, dict):
        return raw_response
    if not isinstance(raw_response, str):
        raise MultimodalComparisonProviderError(
            "The multimodal comparison provider returned an unsupported response."
        )

    try:
        parsed_response = json.loads(raw_response)
    except json.JSONDecodeError as error:
        raise MultimodalComparisonProviderError(
            "The multimodal comparison provider returned malformed JSON."
        ) from error
    if not isinstance(parsed_response, dict):
        raise MultimodalComparisonProviderError(
            "The multimodal comparison provider returned non-object JSON."
        )
    return parsed_response


def _parse_decision(raw_response: dict[str, object]) -> str:
    raw_decision = raw_response.get("decision")
    if not isinstance(raw_decision, str):
        raise MultimodalComparisonProviderError(
            "The multimodal comparison provider returned an invalid decision."
        )
    decision = raw_decision.strip()
    if decision not in ALLOWED_MULTIMODAL_DECISIONS:
        raise MultimodalComparisonProviderError(
            "The multimodal comparison provider returned an invalid decision."
        )
    return decision


def _parse_confidence(raw_response: dict[str, object]) -> float:
    raw_confidence = raw_response.get("confidence")
    if isinstance(raw_confidence, bool) or not isinstance(
        raw_confidence,
        int | float,
    ):
        raise MultimodalComparisonProviderError(
            "The multimodal comparison provider returned invalid confidence."
        )
    confidence = float(raw_confidence)
    if (
        math.isnan(confidence)
        or math.isinf(confidence)
        or confidence < 0
        or confidence > 1
    ):
        raise MultimodalComparisonProviderError(
            "The multimodal comparison provider returned invalid confidence."
        )
    return confidence


def _parse_reason(raw_response: dict[str, object]) -> str:
    raw_reason = raw_response.get("reason")
    if not isinstance(raw_reason, str) or not raw_reason.strip():
        raise MultimodalComparisonProviderError(
            "The multimodal comparison provider returned an invalid reason."
        )
    return raw_reason.strip()


@lru_cache
def get_multimodal_comparison_provider() -> MultimodalComparisonProvider:
    return GoogleGeminiMultimodalComparisonProvider()
