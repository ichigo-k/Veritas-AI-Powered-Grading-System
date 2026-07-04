"""
S3 helper utilities for grading-time file retrieval and presigned URLs.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


@dataclass(frozen=True)
class S3ObjectData:
    bucket: str
    key: str
    body: bytes
    content_type: str
    filename: str | None = None


class S3ResolutionError(Exception):
    """Raised when a fileUrl cannot be resolved to an S3 object."""


class S3Helper:
    def __init__(
        self,
        bucket_name: str,
        region: str,
        upload_prefix: str = "",
        presigned_url_expires_in: int = 3600,
        endpoint_url: str | None = None,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
    ) -> None:
        self._default_bucket = bucket_name
        self._upload_prefix = upload_prefix.strip("/")
        self._presigned_url_expires_in = presigned_url_expires_in

        client_kwargs: dict = {"region_name": region}
        if aws_access_key_id and aws_secret_access_key:
            client_kwargs["aws_access_key_id"] = aws_access_key_id
            client_kwargs["aws_secret_access_key"] = aws_secret_access_key
        if endpoint_url:
            # Pointing at an S3-compatible server (MinIO, LocalStack, etc.).
            # These require path-style addressing — bucket in the path, not the
            # host — since "bucket.localhost" doesn't resolve. Force SigV4 so
            # presigned URLs validate correctly.
            client_kwargs["endpoint_url"] = endpoint_url
            client_kwargs["config"] = Config(
                s3={"addressing_style": "path"},
                signature_version="s3v4",
            )
        self._client = boto3.client("s3", **client_kwargs)

    def resolve_object(self, file_ref: str) -> S3ObjectData:
        bucket, key_candidates = self._resolve_bucket_and_key_candidates(file_ref)
        last_error: Exception | None = None

        for key in key_candidates:
            try:
                head = self._client.head_object(Bucket=bucket, Key=key)
                obj = self._client.get_object(Bucket=bucket, Key=key)
                body = obj["Body"].read()
                content_type = (head.get("ContentType") or obj.get("ContentType") or self._guess_content_type(key))
                return S3ObjectData(
                    bucket=bucket,
                    key=key,
                    body=body,
                    content_type=content_type,
                    filename=self._filename_from_key(key),
                )
            except ClientError as exc:
                last_error = exc
                code = exc.response.get("Error", {}).get("Code", "")
                if code in {"NoSuchKey", "NotFound", "404", "NoSuchBucket"}:
                    continue
                raise S3ResolutionError(f"Failed to fetch S3 object for ref {file_ref!r}: {exc}") from exc

        raise S3ResolutionError(f"S3 object not found for ref {file_ref!r}") from last_error

    def get_presigned_get_url(self, key: str, bucket: str | None = None) -> str:
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket or self._default_bucket, "Key": key},
            ExpiresIn=self._presigned_url_expires_in,
        )

    def get_presigned_put_url(self, key: str, content_type: str, bucket: str | None = None) -> str:
        return self._client.generate_presigned_url(
            "put_object",
            Params={"Bucket": bucket or self._default_bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=self._presigned_url_expires_in,
        )

    def _resolve_bucket_and_key_candidates(self, file_ref: str) -> tuple[str, list[str]]:
        ref = file_ref.strip()
        if not ref:
            raise S3ResolutionError("Empty file reference")

        parsed = urlparse(ref)
        if parsed.scheme == "s3":
            bucket = parsed.netloc
            key = parsed.path.lstrip("/")
            if not bucket or not key:
                raise S3ResolutionError(f"Invalid s3 URI: {file_ref!r}")
            return bucket, [unquote(key)]

        if parsed.scheme in {"http", "https"} and parsed.netloc:
            resolved = self._parse_http_s3_url(parsed)
            if resolved:
                bucket, key = resolved
                return bucket, [key]

        key_candidates = [ref]
        if self._upload_prefix and not ref.startswith(self._upload_prefix + "/"):
            key_candidates.append(f"{self._upload_prefix}/{ref.lstrip('/')}")

        return self._default_bucket, key_candidates

    def _parse_http_s3_url(self, parsed) -> tuple[str, str] | None:
        host = parsed.netloc.lower()
        path = parsed.path.lstrip("/")

        # virtual-hosted style: bucket.s3.amazonaws.com/key
        if ".s3." in host or host.startswith("s3.") or host.endswith(".amazonaws.com"):
            host_parts = host.split(".")
            if host.startswith("s3."):
                parts = path.split("/", 1)
                if len(parts) == 2 and parts[0]:
                    return parts[0], unquote(parts[1])
            if host_parts and host_parts[0] not in {"s3", "www"}:
                bucket = host_parts[0]
                if path:
                    return bucket, unquote(path)

        return None

    def _filename_from_key(self, key: str) -> str | None:
        name = key.rsplit("/", 1)[-1]
        return name or None

    def _guess_content_type(self, key: str) -> str:
        guessed, _ = mimetypes.guess_type(key)
        return guessed or "application/octet-stream"
