# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for opensearch_client.client.OpensearchClient."""

import json
from typing import Any

from opensearch_client.client import (
    DEFAULT_INDEX,
    BulkItem,
    OpensearchClient,
    _pack_batches,
)
from opensearch_client.result import Failure, OpensearchResult, Success


class FakeTransport:
    """Records the request() it receives and returns a preset result."""

    def __init__(self, result: OpensearchResult[Any]) -> None:
        self.result = result
        self.calls: list[tuple[str, str, Any, int]] = []

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        content_type: str = "application/json",
        timeout: int = 30,
    ) -> OpensearchResult[Any]:
        self.calls.append((method, path, body, timeout))
        return self.result


class _SeqTransport:
    """A transport returning queued results in order (the last repeats).

    Counts the calls.
    """

    def __init__(self, *results: OpensearchResult[Any]) -> None:
        self._results = results
        self.calls = 0

    def request(self, *_: Any, **__: Any) -> OpensearchResult[Any]:
        self.calls += 1
        return self._results[min(self.calls - 1, len(self._results) - 1)]


def test_request_delegates_to_the_transport() -> None:
    transport = FakeTransport(Success({"x": 1}))
    res = OpensearchClient(transport).request("PUT", "idx", {"b": 2}, timeout=99)
    assert res.data == {"x": 1}
    method, path, body, timeout = transport.calls[0]
    assert (method, path, timeout) == ("PUT", "idx", 99)
    assert json.loads(body) == {"b": 2}  # the client JSON-encodes before the transport


def test_default_index_is_used_and_can_be_overridden() -> None:
    assert (
        OpensearchClient(FakeTransport(Success({}))).default_index
        == DEFAULT_INDEX
        == "*"
    )
    transport = FakeTransport(Success({"hits": {"hits": []}}))
    client = OpensearchClient(transport, default_index="logs-*")
    client.search({"q": 1})
    client.search({"q": 1}, index="other-*")
    assert transport.calls[0][1] == "logs-*/_search"
    assert transport.calls[1][1] == "other-*/_search"


def test_search_extracts_each_hit_source() -> None:
    hits = {"hits": {"hits": [{"_source": {"a": 1}}, {"_source": {"a": 2}}]}}
    res = OpensearchClient(FakeTransport(Success(hits))).search({"q": 1})
    assert res.data == [{"a": 1}, {"a": 2}]


def test_version_helpers_process_cat_output() -> None:
    nodes = [{"version": "2.19.1"}, {"version": "2.19.1"}, {"version": "2.18.0"}]
    assert OpensearchClient(
        FakeTransport(Success(nodes))
    ).opensearch_version().data == [
        "2.18.0",
        "2.19.1",
    ]
    plugins = [
        {"component": "sql", "version": "1"},
        {"component": "sec", "version": "2"},
    ]
    assert OpensearchClient(FakeTransport(Success(plugins))).plugin_versions().data == {
        "sql": "1",
        "sec": "2",
    }


def test_read_helpers_propagate_a_failed_request() -> None:
    res = OpensearchClient(FakeTransport(Failure("500 boom"))).search({"q": 1})
    assert not res
    assert "500" in res.reason


def test_index_exists_maps_200_and_404_and_propagates_other_errors() -> None:
    assert OpensearchClient(FakeTransport(Success({}))).index_exists("i").data is True
    missing = OpensearchClient(FakeTransport(Failure("no", status=404)))
    assert missing.index_exists("i").data is False
    err = OpensearchClient(FakeTransport(Failure("auth", status=401))).index_exists("i")
    assert not err and err.status == 401  # a non-404 failure is not "does not exist"


def test_list_indices_extracts_and_sorts_names() -> None:
    rows = [{"index": "b-2"}, {"index": "a-1"}]
    got = OpensearchClient(FakeTransport(Success(rows))).list_indices("x-*")
    assert got.data == ["a-1", "b-2"]


def test_pack_batches_bounds_by_bytes() -> None:
    items = [BulkItem(b"aaaa", {}), BulkItem(b"bbbb", {}), BulkItem(b"cccc", {})]
    assert [len(batch) for batch in _pack_batches(items, max_bytes=8)] == [2, 1]
    oversized = [BulkItem(b"x" * 9, {})]  # too big to split; gets a batch of its own
    assert _pack_batches(oversized, max_bytes=8) == [oversized]


def test_bulk_halves_a_batch_on_413_and_indexes_the_halves() -> None:
    ok = Success({"items": [{"index": {"status": 201}}]})
    transport = _SeqTransport(Failure("413", status=413), ok, ok)
    res = OpensearchClient(transport).bulk([{"a": 1}, {"a": 2}])
    assert res and res.data["indexed"] == 2 and transport.calls == 3


def test_bulk_retries_failed_docs_then_fails_with_the_summary() -> None:
    transport = _SeqTransport(
        Failure("503 unavailable", status=503)
    )  # every attempt fails
    res = OpensearchClient(transport).bulk([{"a": 1}], max_retries=2)
    assert not res and res.data["failed"] == 1 and transport.calls == 3  # 1 + 2 retries
