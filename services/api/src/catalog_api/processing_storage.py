from __future__ import annotations

import os
from functools import lru_cache
from threading import Lock
from typing import Protocol

import google.auth
from google.api_core.exceptions import GoogleAPIError, NotFound
from google.auth.exceptions import GoogleAuthError
from google.cloud import storage

from catalog_api.upload_storage import STORAGE_SCOPE, UPLOAD_BUCKET_ENV


class WorkerStorage(Protocol):
    def read_object_bytes(self, *, object_key: str) -> bytes: ...

    def write_object_bytes(
        self,
        *,
        object_key: str,
        content_type: str,
        data: bytes,
    ) -> None: ...


class WorkerObjectNotFoundError(Exception):
    """Raised when the worker cannot find an expected source object."""


class WorkerObjectReadError(Exception):
    """Raised when the worker cannot read an object due to infrastructure failure."""


class WorkerObjectWriteError(Exception):
    """Raised when the worker cannot write a derived object."""


class GoogleCloudStorageWorkerStorage:
    def __init__(self, *, bucket_name: str) -> None:
        self.bucket_name = bucket_name.removeprefix("gs://").strip("/")
        self._client: storage.Client | None = None
        self._initialization_lock = Lock()

    def read_object_bytes(self, *, object_key: str) -> bytes:
        if not self.bucket_name:
            raise WorkerObjectReadError(
                f"{UPLOAD_BUCKET_ENV} must name the worker storage bucket."
            )

        try:
            client = self._google_client()
            blob = client.bucket(self.bucket_name).blob(object_key)
            return blob.download_as_bytes(client=client)
        except NotFound as error:
            raise WorkerObjectNotFoundError from error
        except (GoogleAPIError, GoogleAuthError, ValueError) as error:
            raise WorkerObjectReadError from error

    def write_object_bytes(
        self,
        *,
        object_key: str,
        content_type: str,
        data: bytes,
    ) -> None:
        if not self.bucket_name:
            raise WorkerObjectWriteError(
                f"{UPLOAD_BUCKET_ENV} must name the worker storage bucket."
            )

        try:
            client = self._google_client()
            blob = client.bucket(self.bucket_name).blob(object_key)
            blob.upload_from_string(
                data,
                client=client,
                content_type=content_type,
            )
        except (GoogleAPIError, GoogleAuthError, ValueError) as error:
            raise WorkerObjectWriteError from error

    def _google_client(self) -> storage.Client:
        if self._client is not None:
            return self._client

        with self._initialization_lock:
            if self._client is None:
                credentials, project_id = google.auth.default(scopes=[STORAGE_SCOPE])
                self._client = storage.Client(
                    project=project_id,
                    credentials=credentials,
                )

        return self._client


@lru_cache
def get_worker_storage() -> WorkerStorage:
    return GoogleCloudStorageWorkerStorage(
        bucket_name=os.getenv(UPLOAD_BUCKET_ENV, ""),
    )
