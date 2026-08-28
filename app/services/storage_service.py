from __future__ import annotations

from pathlib import Path
from threading import Lock

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import Settings, settings


class ObjectStorageError(RuntimeError):
    pass


class ObjectNotFoundError(FileNotFoundError):
    pass


class StorageService:
    def __init__(self, config: Settings = settings, client: BaseClient | None = None) -> None:
        self.config = config
        self._client = client
        self._client_lock = Lock()
        self._bucket_lock = Lock()
        self._bucket_ready = False

    @staticmethod
    def object_key(course_id: str, content_hash: str) -> str:
        return f"courses/{course_id}/documents/{content_hash}.pdf"

    def put_pdf(self, key: str, content: bytes) -> None:
        if self.config.object_storage_backend == "local":
            path = self._local_object_path(key)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            except OSError as exc:
                raise ObjectStorageError("Local object storage could not write the PDF.") from exc
            return

        self._ensure_bucket()
        kwargs: dict[str, object] = {
            "Bucket": self.config.s3_bucket,
            "Key": key,
            "Body": content,
            "ContentType": "application/pdf",
        }
        if self.config.s3_server_side_encryption:
            kwargs["ServerSideEncryption"] = self.config.s3_server_side_encryption
        try:
            self.client.put_object(**kwargs)
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStorageError("Object storage rejected the PDF upload.") from exc

    def get_bytes(self, key: str) -> bytes:
        legacy_path = self._legacy_path(key)
        if legacy_path is not None:
            if not legacy_path.is_file():
                raise ObjectNotFoundError("Stored PDF was not found.")
            try:
                return legacy_path.read_bytes()
            except OSError as exc:
                raise ObjectStorageError("The legacy PDF could not be read.") from exc

        if self.config.object_storage_backend == "local":
            path = self._local_object_path(key)
            if not path.is_file():
                raise ObjectNotFoundError("Stored PDF was not found.")
            try:
                return path.read_bytes()
            except OSError as exc:
                raise ObjectStorageError("Local object storage could not read the PDF.") from exc

        try:
            response = self.client.get_object(Bucket=self.config.s3_bucket, Key=key)
            body = response["Body"]
            try:
                return body.read()
            finally:
                body.close()
        except (BotoCoreError, ClientError) as exc:
            if isinstance(exc, ClientError) and self._is_not_found(exc):
                raise ObjectNotFoundError("Stored PDF was not found.") from exc
            raise ObjectStorageError("Object storage could not read the PDF.") from exc

    def exists(self, key: str) -> bool:
        legacy_path = self._legacy_path(key)
        if legacy_path is not None:
            return legacy_path.is_file()
        if self.config.object_storage_backend == "local":
            return self._local_object_path(key).is_file()
        try:
            self.client.head_object(Bucket=self.config.s3_bucket, Key=key)
            return True
        except (BotoCoreError, ClientError) as exc:
            if isinstance(exc, ClientError) and self._is_not_found(exc):
                return False
            raise ObjectStorageError("Object storage availability check failed.") from exc

    def delete(self, key: str) -> None:
        legacy_path = self._legacy_path(key)
        if legacy_path is not None:
            try:
                legacy_path.unlink(missing_ok=True)
            except OSError as exc:
                raise ObjectStorageError("The legacy PDF could not be deleted.") from exc
            return
        if self.config.object_storage_backend == "local":
            try:
                self._local_object_path(key).unlink(missing_ok=True)
            except OSError as exc:
                raise ObjectStorageError("Local object storage could not delete the PDF.") from exc
            return
        try:
            self.client.delete_object(Bucket=self.config.s3_bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStorageError("Object storage could not delete the PDF.") from exc

    def is_legacy_reference(self, reference: str) -> bool:
        return self._legacy_path(reference) is not None

    def check_ready(self) -> None:
        if self.config.object_storage_backend == "local":
            root = self.config.legacy_upload_dir.resolve()
            if not root.exists() or not root.is_dir():
                raise ObjectStorageError("Local object storage directory is unavailable.")
            return
        self._ensure_bucket()

    @property
    def client(self) -> BaseClient:
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    options: dict[str, object] = {
                        "service_name": "s3",
                        "region_name": self.config.s3_region,
                        "config": Config(
                            connect_timeout=self.config.provider_timeout_seconds,
                            read_timeout=self.config.provider_timeout_seconds,
                            retries={"max_attempts": 3, "mode": "standard"},
                            s3={
                                "addressing_style": (
                                    "path" if self.config.s3_force_path_style else "auto"
                                )
                            },
                        ),
                    }
                    if self.config.s3_endpoint_url:
                        options["endpoint_url"] = self.config.s3_endpoint_url
                    if self.config.s3_access_key_id:
                        options["aws_access_key_id"] = self.config.s3_access_key_id
                    if self.config.s3_secret_access_key:
                        options["aws_secret_access_key"] = self.config.s3_secret_access_key
                    self._client = boto3.client(**options)
        return self._client

    def _ensure_bucket(self) -> None:
        if self._bucket_ready:
            return
        with self._bucket_lock:
            if self._bucket_ready:
                return
            try:
                self.client.head_bucket(Bucket=self.config.s3_bucket)
            except (BotoCoreError, ClientError) as exc:
                if (
                    not isinstance(exc, ClientError)
                    or not self._is_bucket_not_found(exc)
                    or not self.config.s3_auto_create_bucket
                ):
                    raise ObjectStorageError(
                        "The configured object-storage bucket is unavailable."
                    ) from exc
                create_kwargs: dict[str, object] = {"Bucket": self.config.s3_bucket}
                if not self.config.s3_endpoint_url and self.config.s3_region != "us-east-1":
                    create_kwargs["CreateBucketConfiguration"] = {
                        "LocationConstraint": self.config.s3_region
                    }
                try:
                    self.client.create_bucket(**create_kwargs)
                except (BotoCoreError, ClientError):
                    # Another API process may have created the bucket between
                    # our HEAD and CREATE calls. Treat it as success only when
                    # a follow-up HEAD proves this process can access it.
                    try:
                        self.client.head_bucket(Bucket=self.config.s3_bucket)
                    except (BotoCoreError, ClientError) as verify_exc:
                        raise ObjectStorageError(
                            "The configured object-storage bucket could not be created."
                        ) from verify_exc
            self._bucket_ready = True

    def _legacy_path(self, reference: str) -> Path | None:
        candidate = Path(reference)
        legacy_root = self.config.legacy_upload_dir.resolve()
        try:
            resolved = candidate.resolve()
            resolved.relative_to(legacy_root)
        except (OSError, ValueError):
            return None
        return resolved

    def _local_object_path(self, key: str) -> Path:
        root = self.config.legacy_upload_dir.resolve()
        candidate = (root / "objects" / key).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ObjectStorageError("Invalid object-storage key.") from exc
        return candidate

    @staticmethod
    def _is_not_found(exc: ClientError) -> bool:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code == "NoSuchBucket":
            return False
        return code in {"404", "NoSuchKey", "NotFound"} or status == 404

    @staticmethod
    def _is_bucket_not_found(exc: ClientError) -> bool:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return code in {"404", "NoSuchBucket", "NotFound"} or status == 404


storage_service = StorageService()
