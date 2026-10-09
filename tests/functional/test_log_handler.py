# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Functional tests for opensearch_client.log_handler against a live OpenSearch cluster.

These need the same ``OPENSEARCH_URL`` / ``OPENSEARCH_USER`` /
``OPENSEARCH_PASSWORD`` variables as ``test_opensearch_client.py`` and are skipped when any
is unset. ``tests/functional/run.sh`` starts an ephemeral Docker cluster, sets
them, runs these tests, and tears everything down.
"""

import logging
import os
import uuid

import pytest

from opensearch_client.config import client_from_env
from opensearch_client.log_handler import EcsFormatter, OpensearchHandler

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


def test_handler_indexes_ecs_documents_that_a_real_cluster_accepts() -> None:
    """Records reach a fresh index as ECS documents, with nothing silently dropped."""
    client = client_from_env()
    assert client is not None, "OPENSEARCH_* is set but client_from_env returned None"

    index = f"opensearch_client-logs-{uuid.uuid4().hex[:8]}"
    # Settle the client's transport first: as the very first request, a 404 is read
    # as "the cluster did not answer" and sends the client to the dashboard proxy.
    assert client.get("_cluster/health")
    # The handler, not the test, must create the index.
    assert client.index_exists(index).data is False
    handler = OpensearchHandler(client, index, level=logging.INFO)
    handler.setFormatter(
        EcsFormatter(service_name="test", extra_fields={"labels": {"env": "ci"}})
    )
    # A logger of its own that does not propagate, so only this handler sees the
    # records and nothing else in the process can feed into the index.
    logger = logging.getLogger(f"opensearch_client-test-{index}")
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)

    try:
        logger.debug("below the handler level")
        logger.info("collector started", extra={"collector": "test"})
        logger.warning("fetched %d records from %s", 3, "source")
        try:
            raise ZeroDivisionError("division by zero")
        except ZeroDivisionError:
            logger.exception("lookup failed")
        handler.flush()

        assert handler.dropped == 0
        assert client.refresh(index)
        search = client.search({"query": {"match_all": {}}, "size": 10}, index=index)
        assert search
        documents = {document["message"]: document for document in search.data}
        assert sorted(documents) == [
            "collector started",
            "fetched 3 records from source",
            "lookup failed",
        ]

        started = documents["collector started"]
        assert started["service"]["name"] == "test"
        assert started["labels"] == {"env": "ci", "collector": "test"}
        failed = documents["lookup failed"]
        assert failed["log"]["level"] == "error"
        assert failed["error"]["type"] == "ZeroDivisionError"
        assert "ZeroDivisionError: division by zero" in failed["error"]["stack_trace"]

        # The types the cluster chose for the fields that matter for searching.
        mapping = client.get_mapping(index)
        assert mapping
        properties = mapping.data[index]["mappings"]["properties"]
        assert properties["@timestamp"]["type"] == "date"
        assert properties["event"]["properties"]["severity"]["type"] == "long"
    finally:
        logger.removeHandler(handler)
        handler.close()
        client.request("DELETE", index)
