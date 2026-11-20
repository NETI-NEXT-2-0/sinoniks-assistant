"""Private Garage transport; callers must authorize access before using this adapter.

This synchronous adapter belongs in a worker or a threadpool, never directly in an
async request handler. It deliberately exposes neither credentials nor public URLs.
"""

from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from uuid import UUID

import boto3
from botocore.config import Config

MAX_OBJECT_BYTES = 25 * 1024 * 1024


@dataclass(frozen=True)
class StoredObject:
    data: bytes
    content_type: str
    status_code: int
    content_range: str | None


def object_key(document_id: UUID, version_id: UUID, artifact: str) -> str:
    """Build an opaque, version-specific key, excluding source filenames."""
    if (
        not artifact
        or not artifact.isascii()
        or not all(char.isalnum() or char in "-_." for char in artifact)
        or artifact in {".", ".."}
    ):
        raise ValueError("Invalid artifact identifier")
    return f"{document_id}/{version_id}/{artifact}"


def create_s3_client(
    endpoint_url: str,
    region: str,
    access_key_id: str,
    secret_access_key: str,
    *,
    connect_timeout: int = 5,
    read_timeout: int = 20,
    max_attempts: int = 2,
) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=region,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            retries={"mode": "standard", "total_max_attempts": max_attempts},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


class Storage:
    """Bounded binary operations on the two pre-provisioned private buckets."""

    def __init__(self, client: Any, originals_bucket: str, derived_bucket: str) -> None:
        self.client = client
        self.originals_bucket = originals_bucket
        self.derived_bucket = derived_bucket
        self._buckets = frozenset((originals_bucket, derived_bucket))

    def _check_bucket(self, bucket: str) -> None:
        if bucket not in self._buckets:
            raise ValueError("Bucket is outside application storage")

    def ready(self) -> None:
        for bucket in self._buckets:
            self.client.head_bucket(Bucket=bucket)

    def put(self, bucket: str, key: str, data: bytes, content_type: str) -> str:
        self._check_bucket(bucket)
        if len(data) > MAX_OBJECT_BYTES:
            raise ValueError("Object exceeds 25 MiB limit")
        digest = sha256(data).hexdigest()
        self.client.put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            Metadata={"sha256": digest},
        )
        return digest

    def head(self, bucket: str, key: str) -> dict[str, Any]:
        self._check_bucket(bucket)
        return dict(self.client.head_object(Bucket=bucket, Key=key))

    def get(
        self, bucket: str, key: str, *, start: int | None = None, end: int | None = None
    ) -> StoredObject:
        self._check_bucket(bucket)
        params: dict[str, Any] = {"Bucket": bucket, "Key": key}
        if start is None and end is not None:
            raise ValueError("Range end requires a start")
        if start is not None:
            if start < 0 or (end is not None and end < start):
                raise ValueError("Invalid byte range")
            params["Range"] = f"bytes={start}-{end if end is not None else ''}"
        response = self.client.get_object(**params)
        body = response["Body"]
        try:
            if response["ContentLength"] > MAX_OBJECT_BYTES:
                raise ValueError("Object exceeds 25 MiB limit")
            data = body.read(MAX_OBJECT_BYTES + 1)
            if len(data) > MAX_OBJECT_BYTES:
                raise ValueError("Object exceeds 25 MiB limit")
        finally:
            body.close()
        return StoredObject(
            data=data,
            content_type=response.get("ContentType", "application/octet-stream"),
            status_code=response["ResponseMetadata"]["HTTPStatusCode"],
            content_range=response.get("ContentRange"),
        )

    def delete(self, bucket: str, key: str) -> None:
        self._check_bucket(bucket)
        self.client.delete_object(Bucket=bucket, Key=key)
