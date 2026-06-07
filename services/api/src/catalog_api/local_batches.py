from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Sequence
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

from PIL import Image, ImageOps
from pydantic import BaseModel, ConfigDict, Field

from catalog_api.image_uploads import ValidatedJpeg

LOCAL_STORAGE_ROOT_ENV = "CATALOG_LOCAL_STORAGE_ROOT"
MANIFEST_VERSION = 1
THUMBNAIL_MAX_SIZE = (480, 480)


class ManifestModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class ManifestImage(ManifestModel):
    image_id: UUID = Field(alias="imageId")
    original_filename: str = Field(alias="originalFilename")
    upload_order: int = Field(alias="uploadOrder")
    sha256: str
    group_id: UUID = Field(alias="groupId")
    is_retained: bool = Field(alias="isRetained")


class ManifestGroup(ManifestModel):
    group_id: UUID = Field(alias="groupId")
    retained_image_id: UUID = Field(alias="retainedImageId")
    image_ids: list[UUID] = Field(alias="imageIds")


class BatchManifest(ManifestModel):
    manifest_version: int = Field(alias="manifestVersion")
    batch_id: UUID = Field(alias="batchId")
    status: str
    created_at: datetime = Field(alias="createdAt")
    images: list[ManifestImage]
    groups: list[ManifestGroup]


class LocalBatchNotFoundError(Exception):
    pass


class LocalImageNotFoundError(Exception):
    pass


class LocalGroupNotFoundError(Exception):
    pass


class InvalidLocalBatchEditError(ValueError):
    pass


def default_local_storage_root() -> Path:
    return Path(__file__).resolve().parents[2] / ".local-data"


def configured_local_storage_root() -> Path:
    configured_root = os.getenv(LOCAL_STORAGE_ROOT_ENV)
    if configured_root:
        return Path(configured_root).expanduser().resolve()
    return default_local_storage_root()


class LocalBatchStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.batches_root = root / "batches"

    @classmethod
    def from_environment(cls) -> LocalBatchStore:
        return cls(configured_local_storage_root())

    def create_batch(self, uploads: Sequence[ValidatedJpeg]) -> BatchManifest:
        if not uploads or any(not upload.is_accepted for upload in uploads):
            raise ValueError("LocalBatchStore requires accepted JPEG uploads.")

        batch_id = uuid4()
        staging_directory = self.batches_root / f".{batch_id}.tmp"
        final_directory = self.batches_root / str(batch_id)
        images_directory = staging_directory / "images"
        thumbnails_directory = staging_directory / "thumbnails"

        images_directory.mkdir(parents=True)
        thumbnails_directory.mkdir()

        images: list[ManifestImage] = []
        groups_by_hash: dict[str, ManifestGroup] = {}

        try:
            for upload_order, upload in enumerate(uploads):
                content = upload.content
                if content is None:
                    raise ValueError("Accepted upload content is missing.")

                image_id = uuid4()
                sha256 = hashlib.sha256(content).hexdigest()
                group = groups_by_hash.get(sha256)

                if group is None:
                    group = ManifestGroup(
                        group_id=uuid4(),
                        retained_image_id=image_id,
                        image_ids=[],
                    )
                    groups_by_hash[sha256] = group

                group.image_ids.append(image_id)
                images.append(
                    ManifestImage(
                        image_id=image_id,
                        original_filename=upload.original_filename,
                        upload_order=upload_order,
                        sha256=sha256,
                        group_id=group.group_id,
                        is_retained=image_id == group.retained_image_id,
                    )
                )

                (images_directory / f"{image_id}.jpg").write_bytes(content)
                self._write_thumbnail(
                    content,
                    thumbnails_directory / f"{image_id}.jpg",
                )

            manifest = BatchManifest(
                manifest_version=MANIFEST_VERSION,
                batch_id=batch_id,
                status="ready",
                created_at=datetime.now(timezone.utc),
                images=images,
                groups=list(groups_by_hash.values()),
            )
            self._write_manifest_atomic(staging_directory, manifest)
            self.batches_root.mkdir(parents=True, exist_ok=True)
            os.replace(staging_directory, final_directory)
            return manifest
        except Exception:
            shutil.rmtree(staging_directory, ignore_errors=True)
            raise

    def load_batch(self, batch_id: UUID) -> BatchManifest:
        manifest_path = self._batch_directory(batch_id) / "manifest.json"
        if not manifest_path.is_file():
            raise LocalBatchNotFoundError(str(batch_id))

        return BatchManifest.model_validate_json(manifest_path.read_text())

    def image_path(
        self,
        batch_id: UUID,
        image_id: UUID,
        *,
        thumbnail: bool = False,
    ) -> Path:
        manifest = self.load_batch(batch_id)
        if not any(image.image_id == image_id for image in manifest.images):
            raise LocalImageNotFoundError(str(image_id))

        directory_name = "thumbnails" if thumbnail else "images"
        image_path = self._batch_directory(batch_id) / directory_name / f"{image_id}.jpg"
        if not image_path.is_file():
            raise LocalImageNotFoundError(str(image_id))
        return image_path

    def move_image(
        self,
        batch_id: UUID,
        image_id: UUID,
        target_group_id: UUID,
    ) -> BatchManifest:
        manifest = self.load_batch(batch_id)
        image = next(
            (item for item in manifest.images if item.image_id == image_id),
            None,
        )
        if image is None:
            raise LocalImageNotFoundError(str(image_id))

        target_group = next(
            (
                group
                for group in manifest.groups
                if group.group_id == target_group_id
            ),
            None,
        )
        if target_group is None:
            raise LocalGroupNotFoundError(str(target_group_id))

        if image.group_id == target_group_id:
            return manifest

        source_group = next(
            group
            for group in manifest.groups
            if group.group_id == image.group_id
        )
        source_group.image_ids.remove(image_id)
        target_group.image_ids.append(image_id)
        manifest.groups = [
            group for group in manifest.groups if group.image_ids
        ]

        self._normalize_group_membership(manifest)
        self._write_manifest_atomic(self._batch_directory(batch_id), manifest)
        return manifest

    def create_group(
        self,
        batch_id: UUID,
        image_ids: Sequence[UUID],
    ) -> tuple[UUID, BatchManifest]:
        if not image_ids:
            raise InvalidLocalBatchEditError(
                "Select at least one image to create a group."
            )

        selected_ids = set(image_ids)
        if len(selected_ids) != len(image_ids):
            raise InvalidLocalBatchEditError(
                "Image identifiers must be unique."
            )

        manifest = self.load_batch(batch_id)
        known_ids = {image.image_id for image in manifest.images}
        unknown_ids = selected_ids - known_ids
        if unknown_ids:
            raise LocalImageNotFoundError(str(next(iter(unknown_ids))))

        if any(set(group.image_ids) == selected_ids for group in manifest.groups):
            raise InvalidLocalBatchEditError(
                "The selected images already form an existing group."
            )

        for group in manifest.groups:
            group.image_ids = [
                image_id
                for image_id in group.image_ids
                if image_id not in selected_ids
            ]

        manifest.groups = [
            group for group in manifest.groups if group.image_ids
        ]
        new_group_id = uuid4()
        manifest.groups.append(
            ManifestGroup(
                group_id=new_group_id,
                retained_image_id=image_ids[0],
                image_ids=list(image_ids),
            )
        )

        self._normalize_group_membership(manifest)
        self._write_manifest_atomic(self._batch_directory(batch_id), manifest)
        return new_group_id, manifest

    def _batch_directory(self, batch_id: UUID) -> Path:
        return self.batches_root / str(batch_id)

    @staticmethod
    def _normalize_group_membership(manifest: BatchManifest) -> None:
        images_by_id = {image.image_id: image for image in manifest.images}
        assigned_ids: set[UUID] = set()

        for image in manifest.images:
            image.is_retained = False

        for group in manifest.groups:
            group.image_ids.sort(
                key=lambda image_id: images_by_id[image_id].upload_order
            )
            group.retained_image_id = group.image_ids[0]

            for image_id in group.image_ids:
                if image_id in assigned_ids:
                    raise ValueError("An image cannot belong to multiple groups.")
                assigned_ids.add(image_id)
                image = images_by_id[image_id]
                image.group_id = group.group_id
                image.is_retained = image_id == group.retained_image_id

        if assigned_ids != set(images_by_id):
            raise ValueError("Every image must belong to exactly one group.")

    @staticmethod
    def _write_thumbnail(content: bytes, destination: Path) -> None:
        with Image.open(BytesIO(content)) as source:
            normalized = ImageOps.exif_transpose(source)
            if normalized.mode != "RGB":
                normalized = normalized.convert("RGB")
            normalized.thumbnail(THUMBNAIL_MAX_SIZE, Image.Resampling.LANCZOS)
            normalized.save(destination, format="JPEG", quality=85, optimize=True)

    @staticmethod
    def _write_manifest_atomic(
        batch_directory: Path,
        manifest: BatchManifest,
    ) -> None:
        manifest_path = batch_directory / "manifest.json"
        temporary_path = batch_directory / f".manifest-{uuid4()}.tmp"
        payload = manifest.model_dump(mode="json", by_alias=True)

        with temporary_path.open("x", encoding="utf-8") as manifest_file:
            json.dump(payload, manifest_file, indent=2)
            manifest_file.write("\n")
            manifest_file.flush()
            os.fsync(manifest_file.fileno())

        os.replace(temporary_path, manifest_path)
