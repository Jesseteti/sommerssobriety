import os
import hashlib
import boto3
from botocore.client import Config


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def _get_s3_client():
    endpoint = os.environ.get("AWS_ENDPOINT_URL")
    access_key = os.environ.get("AWS_ACCESS_KEY_ID")
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
    region = os.environ.get("AWS_DEFAULT_REGION", "auto")

    if not endpoint or not access_key or not secret_key:
        raise RuntimeError(
            "Railway S3 credentials are not fully configured. "
            "Expected RAILWAY_S3_ENDPOINT, RAILWAY_S3_ACCESS_KEY_ID, "
            "RAILWAY_S3_SECRET_ACCESS_KEY, and RAILWAY_S3_BUCKET."
        )

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(signature_version="s3v4"),
    )


def _get_real_bucket_name() -> str:
    bucket = os.environ.get("S3_BUCKET_NAME")
    if not bucket:
        raise RuntimeError(
            "RAILWAY_S3_BUCKET is not set. "
            "Map it to the Railway bucket's BUCKET variable."
        )
    return bucket


def _build_object_key(bucket: str, object_path: str) -> str:
    """
    Convert your app's logical bucket + object_path into a single real S3 key.
    Example:
      bucket='receipts', object_path='resident_1/receipt_5.pdf'
      -> 'receipts/resident_1/receipt_5.pdf'
    """
    bucket = bucket.strip("/ ")
    object_path = object_path.lstrip("/ ")
    return f"{bucket}/{object_path}"


def upload_bytes(
    bucket: str,
    object_path: str,
    data: bytes,
    content_type: str = "application/octet-stream",
) -> dict:
    s3 = _get_s3_client()
    real_bucket = _get_real_bucket_name()
    object_key = _build_object_key(bucket, object_path)

    s3.put_object(
        Bucket=real_bucket,
        Key=object_key,
        Body=data,
        ContentType=content_type,
    )

    return {
        "sha256": sha256_bytes(data),
        "size_bytes": len(data),
        "bucket": bucket,               # logical bucket label used by your app
        "object_path": object_path,     # path used by your app
        "object_key": object_key,       # actual S3 key
        "content_type": content_type,
    }


def create_signed_url(bucket: str, object_path: str, expires_in_seconds: int = 300) -> str:
    s3 = _get_s3_client()
    real_bucket = _get_real_bucket_name()
    object_key = _build_object_key(bucket, object_path)

    return s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={
            "Bucket": real_bucket,
            "Key": object_key,
        },
        ExpiresIn=expires_in_seconds,
    )