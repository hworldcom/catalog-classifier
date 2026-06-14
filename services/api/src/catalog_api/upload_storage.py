from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache
from threading import Lock
from typing import Protocol

import google.auth
from google.api_core.exceptions import GoogleAPIError, NotFound
from google.auth import impersonated_credentials
from google.auth.credentials import Credentials, Signing
from google.auth.exceptions import GoogleAuthError
from google.cloud import storage

UPLOAD_BUCKET_ENV = "CATALOG_UPLOAD_BUCKET"
SIGNING_SERVICE_ACCOUNT_ENV = "CATALOG_SIGNING_SERVICE_ACCOUNT"
DEFAULT_SIGNING_SERVICE_ACCOUNT = (
    "catalog-api@catalog-classifier.iam.gserviceaccount.com"
)
SIGNED_UPLOAD_EXPIRATION = timedelta(minutes=15)
GOOGLE_CLOUD_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
STORAGE_SCOPE = "https://www.googleapis.com/auth/devstorage.read_write"
STORAGE_READ_ONLY_SCOPE = "https://www.googleapis.com/auth/devstorage.read_only"


class UploadUrlSigner(Protocol):
    def sign_upload_url(self, *, object_key: str, content_type: str) -> str: ...


class UploadObjectInspector(Protocol):
    def inspect_object(self, *, object_key: str) -> "UploadObjectMetadata": ...


@dataclass(frozen=True)
class UploadObjectMetadata:
    content_type: str | None
    size_bytes: int | None


class UploadUrlSigningError(Exception):
    """Raised when an upload URL cannot be signed."""


class UploadObjectNotFoundError(Exception):
    """Raised when a Cloud Storage object does not exist."""


class UploadObjectInspectionError(Exception):
    """Raised when Cloud Storage metadata cannot be inspected."""


class GoogleCloudStorageUploadUrlSigner:
    def __init__(
        self,
        *,
        bucket_name: str,
        signing_service_account: str,
        expiration: timedelta = SIGNED_UPLOAD_EXPIRATION,
    ) -> None:
        self.bucket_name = bucket_name.removeprefix("gs://").strip("/")
        self.signing_service_account = signing_service_account
        self.expiration = expiration
        self._credentials: Credentials | None = None
        self._client: storage.Client | None = None
        self._initialization_lock = Lock()

    def sign_upload_url(self, *, object_key: str, content_type: str) -> str:
        if not self.bucket_name:
            raise UploadUrlSigningError(
                f"{UPLOAD_BUCKET_ENV} must name the upload bucket."
            )
        if not self.signing_service_account:
            raise UploadUrlSigningError(
                f"{SIGNING_SERVICE_ACCOUNT_ENV} must name the signing service account."
            )

        try:
            client, credentials = self._google_client()
            blob = client.bucket(self.bucket_name).blob(object_key)
            return blob.generate_signed_url(
                version="v4",
                expiration=self.expiration,
                method="PUT",
                content_type=content_type,
                credentials=credentials,
            )
        except (GoogleAPIError, GoogleAuthError, ValueError) as error:
            raise UploadUrlSigningError from error

    def _google_client(
        self,
    ) -> tuple[storage.Client, Credentials]:
        if self._client is not None and self._credentials is not None:
            return self._client, self._credentials

        with self._initialization_lock:
            if self._client is None or self._credentials is None:
                source_credentials, project_id = google.auth.default(
                    scopes=[GOOGLE_CLOUD_SCOPE]
                )
                if (
                    isinstance(source_credentials, Signing)
                    and source_credentials.signer_email
                    == self.signing_service_account
                ):
                    credentials = source_credentials
                else:
                    credentials = impersonated_credentials.Credentials(
                        source_credentials=source_credentials,
                        target_principal=self.signing_service_account,
                        target_scopes=[STORAGE_SCOPE],
                        lifetime=int(self.expiration.total_seconds()),
                    )
                self._credentials = credentials
                self._client = storage.Client(
                    project=project_id,
                    credentials=credentials,
                )

        return self._client, self._credentials


class GoogleCloudStorageUploadObjectInspector:
    def __init__(
        self,
        *,
        bucket_name: str,
        signing_service_account: str,
    ) -> None:
        self.bucket_name = bucket_name.removeprefix("gs://").strip("/")
        self.signing_service_account = signing_service_account
        self._credentials: Credentials | None = None
        self._client: storage.Client | None = None
        self._initialization_lock = Lock()

    def inspect_object(self, *, object_key: str) -> UploadObjectMetadata:
        if not self.bucket_name:
            raise UploadObjectInspectionError(
                f"{UPLOAD_BUCKET_ENV} must name the upload bucket."
            )
        if not self.signing_service_account:
            raise UploadObjectInspectionError(
                f"{SIGNING_SERVICE_ACCOUNT_ENV} must name the signing service account."
            )

        try:
            client = self._google_client()
            blob = client.bucket(self.bucket_name).blob(object_key)
            blob.reload(client=client)
            return UploadObjectMetadata(
                content_type=blob.content_type,
                size_bytes=blob.size,
            )
        except NotFound as error:
            raise UploadObjectNotFoundError from error
        except (GoogleAPIError, GoogleAuthError, ValueError) as error:
            raise UploadObjectInspectionError from error

    def _google_client(self) -> storage.Client:
        if self._client is not None and self._credentials is not None:
            return self._client

        with self._initialization_lock:
            if self._client is None or self._credentials is None:
                source_credentials, project_id = google.auth.default(
                    scopes=[GOOGLE_CLOUD_SCOPE]
                )
                if (
                    isinstance(source_credentials, Signing)
                    and source_credentials.signer_email
                    == self.signing_service_account
                ):
                    credentials = source_credentials
                else:
                    credentials = impersonated_credentials.Credentials(
                        source_credentials=source_credentials,
                        target_principal=self.signing_service_account,
                        target_scopes=[STORAGE_READ_ONLY_SCOPE],
                        lifetime=int(SIGNED_UPLOAD_EXPIRATION.total_seconds()),
                    )
                self._credentials = credentials
                self._client = storage.Client(
                    project=project_id,
                    credentials=credentials,
                )

        return self._client


@lru_cache
def get_upload_url_signer() -> UploadUrlSigner:
    return GoogleCloudStorageUploadUrlSigner(
        bucket_name=os.getenv(UPLOAD_BUCKET_ENV, ""),
        signing_service_account=os.getenv(
            SIGNING_SERVICE_ACCOUNT_ENV,
            DEFAULT_SIGNING_SERVICE_ACCOUNT,
        ),
    )


@lru_cache
def get_upload_object_inspector() -> UploadObjectInspector:
    return GoogleCloudStorageUploadObjectInspector(
        bucket_name=os.getenv(UPLOAD_BUCKET_ENV, ""),
        signing_service_account=os.getenv(
            SIGNING_SERVICE_ACCOUNT_ENV,
            DEFAULT_SIGNING_SERVICE_ACCOUNT,
        ),
    )
