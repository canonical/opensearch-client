# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Functional tests for osclient.logging against a live OpenSearch cluster.

These need the same ``OPENSEARCH_URL`` / ``OPENSEARCH_USER`` /
``OPENSEARCH_PASSWORD`` variables as ``test_osclient.py`` and are skipped when any
is unset. ``tests/functional/run.sh`` starts an ephemeral Docker cluster, sets
them, runs these tests, and tears everything down.
"""

import logging
import os
import uuid

import pytest

from osclient import DirectTransport
from osclient.config import client_from_env
from osclient.logging import OpensearchHandler

if not (
    os.environ.get("OPENSEARCH_URL")
    and os.environ.get("OPENSEARCH_USER")
    and os.environ.get("OPENSEARCH_PASSWORD")
):
    pytest.skip(
        "OPENSEARCH_URL, OPENSEARCH_USER, and OPENSEARCH_PASSWORD must be set; "
        "no OpenSearch cluster available",
        allow_module_level=True,
    )


def test_handler_indexes_records_at_or_above_its_level() -> None:
    """Records reach the index with their message formatted; lower levels do not."""
    client = client_from_env()
    assert client is not None, "OPENSEARCH_* is set but client_from_env returned None"
    transport = DirectTransport(
        os.environ["OPENSEARCH_URL"],
        (os.environ["OPENSEARCH_USER"], os.environ["OPENSEARCH_PASSWORD"]),
        verify=True,
    )

    index = f"osclient-logs-{uuid.uuid4().hex[:8]}"
    handler = OpensearchHandler(index, transport, level=logging.INFO)
    # A logger of its own that does not propagate, so only this handler sees the
    # records and nothing else in the process can feed into the index.
    logger = logging.getLogger(f"osclient-test-{index}")
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)

    try:
        logger.debug("below the handler level")
        logger.info("collector started")
        logger.warning("fetched %d records from %s", 3, "superset")
        handler.flush()

        assert client.refresh(index)
        search = client.search({"query": {"match_all": {}}, "size": 10}, index=index)
        assert search
        assert sorted(doc["message"] for doc in search.data) == [
            "collector started",
            "fetched 3 records from superset",
        ]
    finally:
        logger.removeHandler(handler)
        handler.close()
        client.request("DELETE", index)
