"""V2 against a real Garage instance; never replaced with an S3 emulator.

Set GARAGE_TEST_ENDPOINT, GARAGE_TEST_ACCESS_KEY, GARAGE_TEST_SECRET_KEY and
optionally GARAGE_TEST_REGION/ORIGINALS_BUCKET/DERIVED_BUCKET. The test writes
only fresh UUID paths and cleans those paths in a finally block.
"""

import os
from hashlib import sha256
from uuid import uuid4

import httpx
import pytest
from botocore.exceptions import ClientError

from partner_agent.infra.storage import Storage, create_s3_client, object_key

pytestmark = [pytest.mark.integration, pytest.mark.storage]


def test_garage_private_binary_crud_and_range() -> None:
    endpoint = os.getenv("GARAGE_TEST_ENDPOINT")
    if not endpoint:
        pytest.skip("GARAGE_TEST_ENDPOINT is required for live Garage V2")
    region = os.getenv("GARAGE_TEST_REGION", "garage")
    access_key = os.environ["GARAGE_TEST_ACCESS_KEY"]
    secret = os.environ["GARAGE_TEST_SECRET_KEY"]
    originals = os.getenv("GARAGE_TEST_ORIGINALS_BUCKET", "originals")
    derived = os.getenv("GARAGE_TEST_DERIVED_BUCKET", "derived")
    client = create_s3_client(endpoint, region, access_key, secret)
    storage = Storage(client, originals, derived)
    storage.ready()
    payload = bytes(range(256)) * 1024 + "Проверка бинарных данных\x00\r\n".encode()
    key = object_key(uuid4(), uuid4(), "original")
    invalid_client = create_s3_client(endpoint, region, access_key, "invalid-secret")
    try:
        for bucket in (originals, derived):
            assert (
                storage.put(bucket, key, payload, "application/octet-stream")
                == sha256(payload).hexdigest()
            )
            head = storage.head(bucket, key)
            assert head["ContentLength"] == len(payload)
            assert head["Metadata"]["sha256"] == sha256(payload).hexdigest()
            actual = storage.get(bucket, key)
            assert actual.status_code == 200
            assert actual.data == payload
            partial = storage.get(bucket, key, start=13, end=41)
            assert partial.status_code == 206
            assert partial.content_range == f"bytes 13-41/{len(payload)}"
            assert partial.data == payload[13:42]
            tail = storage.get(bucket, key, start=len(payload) - 17)
            assert tail.status_code == 206
            assert tail.data == payload[-17:]
            with pytest.raises(ClientError) as bad_credentials:
                invalid_client.get_object(Bucket=bucket, Key=key)
            assert bad_credentials.value.response["ResponseMetadata"]["HTTPStatusCode"] in (
                401,
                403,
            )
            with httpx.Client(follow_redirects=False, trust_env=False) as anonymous:
                denied = anonymous.get(f"{endpoint.rstrip('/')}/{bucket}/{key}")
                assert denied.status_code in (401, 403)
                listing = anonymous.get(f"{endpoint.rstrip('/')}/{bucket}?list-type=2")
                assert listing.status_code in (401, 403)
            storage.delete(bucket, key)
            with pytest.raises(ClientError) as missing:
                storage.head(bucket, key)
            assert missing.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404
    finally:
        for bucket in (originals, derived):
            storage.delete(bucket, key)
        client.close()
        invalid_client.close()
