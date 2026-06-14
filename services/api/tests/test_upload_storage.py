from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from google.api_core.exceptions import Forbidden, NotFound

from catalog_api import upload_storage
from catalog_api.upload_storage import (
    GoogleCloudStorageUploadObjectInspector,
    GoogleCloudStorageUploadUrlSigner,
    UploadObjectInspectionError,
    UploadObjectMetadata,
    UploadObjectNotFoundError,
    UploadUrlSigningError,
)


class FakeBlob:
    def __init__(
        self,
        *,
        content_type: str | None = None,
        size: int | None = None,
        reload_error: Exception | None = None,
    ) -> None:
        self.arguments: dict[str, Any] | None = None
        self.content_type = content_type
        self.size = size
        self.reload_error = reload_error
        self.reload_client: FakeClient | None = None

    def generate_signed_url(self, **kwargs: Any) -> str:
        self.arguments = kwargs
        return "https://storage.example.test/signed"

    def reload(self, *, client: FakeClient) -> None:
        self.reload_client = client
        if self.reload_error is not None:
            raise self.reload_error


class FakeBucket:
    def __init__(self, blob: FakeBlob) -> None:
        self.requested_object_key: str | None = None
        self._blob = blob

    def blob(self, object_key: str) -> FakeBlob:
        self.requested_object_key = object_key
        return self._blob


class FakeClient:
    def __init__(self, bucket: FakeBucket) -> None:
        self.requested_bucket_name: str | None = None
        self._bucket = bucket

    def bucket(self, bucket_name: str) -> FakeBucket:
        self.requested_bucket_name = bucket_name
        return self._bucket


class ExistingSigningCredentials:
    signer_email = "catalog-api@catalog-classifier.iam.gserviceaccount.com"


def test_google_signer_uses_impersonation_and_v4_put_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_credentials = object()
    target_credentials = object()
    blob = FakeBlob()
    bucket = FakeBucket(blob)
    client = FakeClient(bucket)
    impersonation_arguments: dict[str, Any] = {}
    client_arguments: dict[str, Any] = {}

    monkeypatch.setattr(
        upload_storage.google.auth,
        "default",
        lambda *, scopes: (source_credentials, "catalog-classifier"),
    )

    def create_impersonated_credentials(**kwargs: Any) -> object:
        impersonation_arguments.update(kwargs)
        return target_credentials

    def create_client(**kwargs: Any) -> FakeClient:
        client_arguments.update(kwargs)
        return client

    monkeypatch.setattr(
        upload_storage.impersonated_credentials,
        "Credentials",
        create_impersonated_credentials,
    )
    monkeypatch.setattr(upload_storage.storage, "Client", create_client)

    signer = GoogleCloudStorageUploadUrlSigner(
        bucket_name="gs://lnlabs-bucket",
        signing_service_account=(
            "catalog-api@catalog-classifier.iam.gserviceaccount.com"
        ),
    )
    url = signer.sign_upload_url(
        object_key="organizations/org/batches/batch/originals/image.jpg",
        content_type="image/jpeg",
    )

    assert url == "https://storage.example.test/signed"
    assert impersonation_arguments == {
        "source_credentials": source_credentials,
        "target_principal": (
            "catalog-api@catalog-classifier.iam.gserviceaccount.com"
        ),
        "target_scopes": [upload_storage.STORAGE_SCOPE],
        "lifetime": 900,
    }
    assert client_arguments == {
        "project": "catalog-classifier",
        "credentials": target_credentials,
    }
    assert client.requested_bucket_name == "lnlabs-bucket"
    assert bucket.requested_object_key == (
        "organizations/org/batches/batch/originals/image.jpg"
    )
    assert blob.arguments == {
        "version": "v4",
        "expiration": timedelta(minutes=15),
        "method": "PUT",
        "content_type": "image/jpeg",
        "credentials": target_credentials,
    }


def test_google_signer_reuses_matching_impersonated_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_credentials = ExistingSigningCredentials()
    blob = FakeBlob()
    bucket = FakeBucket(blob)
    client = FakeClient(bucket)
    client_arguments: dict[str, Any] = {}

    monkeypatch.setattr(upload_storage, "Signing", ExistingSigningCredentials)
    monkeypatch.setattr(
        upload_storage.google.auth,
        "default",
        lambda *, scopes: (source_credentials, "catalog-classifier"),
    )

    def reject_double_impersonation(**kwargs: Any) -> object:
        raise AssertionError("matching credentials must not be impersonated again")

    def create_client(**kwargs: Any) -> FakeClient:
        client_arguments.update(kwargs)
        return client

    monkeypatch.setattr(
        upload_storage.impersonated_credentials,
        "Credentials",
        reject_double_impersonation,
    )
    monkeypatch.setattr(upload_storage.storage, "Client", create_client)

    signer = GoogleCloudStorageUploadUrlSigner(
        bucket_name="lnlabs-bucket",
        signing_service_account=source_credentials.signer_email,
    )
    signer.sign_upload_url(
        object_key="object.jpg",
        content_type="image/jpeg",
    )

    assert client_arguments["credentials"] is source_credentials
    assert blob.arguments is not None
    assert blob.arguments["credentials"] is source_credentials


def test_google_signer_requires_a_bucket_name() -> None:
    signer = GoogleCloudStorageUploadUrlSigner(
        bucket_name="",
        signing_service_account="signer@example.test",
    )

    with pytest.raises(UploadUrlSigningError):
        signer.sign_upload_url(
            object_key="object.jpg",
            content_type="image/jpeg",
        )


def test_google_inspector_uses_read_only_impersonation_and_reads_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_credentials = object()
    target_credentials = object()
    blob = FakeBlob(content_type="image/jpeg", size=123)
    bucket = FakeBucket(blob)
    client = FakeClient(bucket)
    impersonation_arguments: dict[str, Any] = {}
    client_arguments: dict[str, Any] = {}

    monkeypatch.setattr(
        upload_storage.google.auth,
        "default",
        lambda *, scopes: (source_credentials, "catalog-classifier"),
    )

    def create_impersonated_credentials(**kwargs: Any) -> object:
        impersonation_arguments.update(kwargs)
        return target_credentials

    def create_client(**kwargs: Any) -> FakeClient:
        client_arguments.update(kwargs)
        return client

    monkeypatch.setattr(
        upload_storage.impersonated_credentials,
        "Credentials",
        create_impersonated_credentials,
    )
    monkeypatch.setattr(upload_storage.storage, "Client", create_client)

    inspector = GoogleCloudStorageUploadObjectInspector(
        bucket_name="gs://lnlabs-bucket",
        signing_service_account=(
            "catalog-api@catalog-classifier.iam.gserviceaccount.com"
        ),
    )

    metadata = inspector.inspect_object(object_key="object.jpg")

    assert metadata == UploadObjectMetadata(
        content_type="image/jpeg",
        size_bytes=123,
    )
    assert impersonation_arguments == {
        "source_credentials": source_credentials,
        "target_principal": (
            "catalog-api@catalog-classifier.iam.gserviceaccount.com"
        ),
        "target_scopes": [upload_storage.STORAGE_READ_ONLY_SCOPE],
        "lifetime": 900,
    }
    assert client_arguments == {
        "project": "catalog-classifier",
        "credentials": target_credentials,
    }
    assert client.requested_bucket_name == "lnlabs-bucket"
    assert bucket.requested_object_key == "object.jpg"
    assert blob.reload_client is client


@pytest.mark.parametrize(
    ("reload_error", "expected_error"),
    [
        (NotFound("missing"), UploadObjectNotFoundError),
        (Forbidden("denied"), UploadObjectInspectionError),
    ],
)
def test_google_inspector_maps_storage_errors(
    monkeypatch: pytest.MonkeyPatch,
    reload_error: Exception,
    expected_error: type[Exception],
) -> None:
    blob = FakeBlob(reload_error=reload_error)
    client = FakeClient(FakeBucket(blob))
    inspector = GoogleCloudStorageUploadObjectInspector(
        bucket_name="lnlabs-bucket",
        signing_service_account="signer@example.test",
    )
    monkeypatch.setattr(inspector, "_google_client", lambda: client)

    with pytest.raises(expected_error):
        inspector.inspect_object(object_key="object.jpg")
