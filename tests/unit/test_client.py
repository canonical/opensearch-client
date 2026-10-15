# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for opensearch_client.client.OpensearchClient."""

import contextlib
import json
from collections.abc import Generator
from typing import Any

from opensearch_client import client as client_module
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

    Counts the calls and keeps the body of every request.
    """

    def __init__(self, *results: OpensearchResult[Any]) -> None:
        self._results = results
        self.calls = 0
        self.bodies: list[bytes | None] = []

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: str = "application/json",
        timeout: int = 30,
    ) -> OpensearchResult[Any]:
        del method, path, content_type, timeout  # part of the transport's signature
        self.calls += 1
        self.bodies.append(body)
        return self._results[min(self.calls - 1, len(self._results) - 1)]


@contextlib.contextmanager
def _recorded_delays() -> Generator[list[float]]:
    """Make ``bulk`` record the delays it asks for instead of waiting."""
    delays: list[float] = []
    original = client_module.sleep
    client_module.sleep = delays.append
    try:
        yield delays
    finally:
        client_module.sleep = original


def _item_error(status: int, error_type: str | None = None) -> OpensearchResult[Any]:
    """A 200 bulk response in which the only document failed."""
    outcome: dict[str, Any] = {"status": status}
    if error_type is not None:
        outcome["error"] = {"type": error_type}
    return Success({"errors": True, "items": [{"index": outcome}]})


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


def test_ensure_index_creates_a_missing_index_and_reports_failures() -> None:
    created = Success({"acknowledged": True})
    missing = Failure("not found", status=404)
    # (what the cluster answers, whether the call succeeds, data, requests made)
    cases: list[tuple[str, list[OpensearchResult[Any]], bool, bool | None, int]] = [
        ("already exists", [Success({})], True, False, 1),
        ("missing, then created", [missing, created], True, True, 2),
        ("existence check fails", [Failure("denied", status=401)], False, None, 1),
        ("creation fails", [missing, Failure("blocked", status=403)], False, None, 2),
    ]

    for label, answers, succeeds, expected_data, requests in cases:
        transport = _SeqTransport(*answers)
        result = OpensearchClient(transport).ensure_index("logs")
        assert bool(result) is succeeds, label
        if succeeds:
            assert result.data is expected_data, label
        else:
            assert result.status in (401, 403), label
        assert transport.calls == requests, label


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
    with _recorded_delays():
        res = OpensearchClient(transport).bulk([{"a": 1}], max_retries=2)
    assert not res and res.data["failed"] == 1 and transport.calls == 3  # 1 + 2 retries


def test_bulk_waits_longer_before_each_retry_up_to_a_minute() -> None:
    down = Failure("503 unavailable", status=503)
    ok = Success({"items": [{"index": {"status": 201}}]})

    # Every attempt fails: one wait before each of the eight retries, doubling
    # from a second and capped at a minute, and none after the last attempt.
    transport = _SeqTransport(down)
    with _recorded_delays() as delays:
        result = OpensearchClient(transport).bulk([{"a": 1}], max_retries=8)
    assert not result
    assert transport.calls == 9
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]

    # The waiting stops as soon as a retry succeeds.
    transport = _SeqTransport(down, down, ok)
    with _recorded_delays() as delays:
        result = OpensearchClient(transport).bulk([{"a": 1}], max_retries=8)
    assert result
    assert transport.calls == 3
    assert delays == [1.0, 2.0]


def test_bulk_retries_only_transient_failures() -> None:
    # (what failed, whether bulk should retry it)
    cases: list[tuple[str, OpensearchResult[Any], bool]] = [
        ("no response", Failure("unreachable", status=None), True),
        ("503 unavailable", Failure("unavailable", status=503), True),
        ("418, a status that is not listed", Failure("teapot", status=418), True),
        ("400 bad request", Failure("bad request", status=400), False),
        ("413 on a single document", Failure("too large", status=413), False),
        ("item 400 with no error type", _item_error(400), False),
        # A listed error type beats the status in both directions.
        (
            "item 403 cluster_block_exception",
            _item_error(403, "cluster_block_exception"),
            True,
        ),
        (
            "item 404 index_not_found_exception",
            _item_error(404, "index_not_found_exception"),
            True,
        ),
        (
            "item 503 mapper_parsing_exception",
            _item_error(503, "mapper_parsing_exception"),
            False,
        ),
    ]

    for label, response, retried in cases:
        transport = _SeqTransport(response)
        with _recorded_delays() as delays:
            result = OpensearchClient(transport).bulk([{"a": 1}], max_retries=2)
        assert not result, label
        assert result.data["failed"] == 1, label
        assert transport.calls == (3 if retried else 1), label
        assert delays == ([1.0, 2.0] if retried else []), label


def test_bulk_resends_only_the_transient_document_of_a_mixed_batch() -> None:
    """The permanent document is reported as failed without being sent again."""
    permanent = {"name": "permanent"}
    transient = {"name": "transient"}
    rejected = {"type": "mapper_parsing_exception"}
    first_pass = Success(
        {
            "errors": True,
            "items": [
                {"index": {"status": 400, "error": rejected}},
                {"index": {"status": 503}},
            ],
        }
    )
    second_pass = Success({"items": [{"index": {"status": 201}}]})
    transport = _SeqTransport(first_pass, second_pass)
    with _recorded_delays():
        result = OpensearchClient(transport).bulk([permanent, transient])
    assert not result
    assert result.data["indexed"] == 1
    assert [failure["document"] for failure in result.data["failures"]] == [permanent]
    assert transport.calls == 2
    resent = transport.bodies[1]
    assert resent is not None
    assert b"transient" in resent
    assert b"permanent" not in resent
