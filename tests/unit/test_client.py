# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for opensearch_client.client.OpensearchClient."""

import json
from typing import Any

from opensearch_client.client import (
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

    Counts the calls and records each one's path.
    """

    def __init__(self, *results: OpensearchResult[Any]) -> None:
        self._results = results
        self.calls = 0
        self.paths: list[str] = []

    def request(
        self, method: str, path: str, *_: Any, **__: Any
    ) -> OpensearchResult[Any]:
        self.calls += 1
        self.paths.append(path)
        return self._results[min(self.calls - 1, len(self._results) - 1)]


def test_request_delegates_to_the_transport() -> None:
    transport = FakeTransport(Success({"x": 1}))
    res = OpensearchClient(transport).request("PUT", "idx", {"b": 2}, timeout=99)
    assert res.data == {"x": 1}
    method, path, body, timeout = transport.calls[0]
    assert (method, path, timeout) == ("PUT", "idx", 99)
    assert json.loads(body) == {"b": 2}  # the client JSON-encodes before the transport


def test_search_extracts_each_hit_source() -> None:
    hits = {"hits": {"hits": [{"_source": {"a": 1}}, {"_source": {"a": 2}}]}}
    res = OpensearchClient(FakeTransport(Success(hits))).search({"q": 1}, "*")
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
    res = OpensearchClient(FakeTransport(Failure("500 boom"))).search({"q": 1}, "*")
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
    res = OpensearchClient(transport).bulk([{"a": 1}, {"a": 2}], "*")
    assert res and res.data["indexed"] == 2 and transport.calls == 3


def test_bulk_retries_failed_docs_then_fails_with_the_summary() -> None:
    transport = _SeqTransport(
        Failure("503 unavailable", status=503)
    )  # every attempt fails
    res = OpensearchClient(transport).bulk([{"a": 1}], "*", max_retries=2)
    assert not res and res.data["failed"] == 1 and transport.calls == 3  # 1 + 2 retries


POLICY_ID_KEY = "index.plugins.index_state_management.policy_id"
# a and c show no policy, b is managed by "old".
STATUS = Success(
    {
        "a": {POLICY_ID_KEY: None},
        "b": {POLICY_ID_KEY: "old"},
        "c": {POLICY_ID_KEY: None},
        "total_managed_indices": 1,
    }
)
# c was attached too recently for the status to show it, so the add API refuses it.
ADD_REFUSES_C = Success(
    {
        "updated_indices": 1,
        "failures": True,
        "failed_indices": [
            {"index_name": "c", "reason": "This index already has a policy"}
        ],
    }
)
ALL_DONE = Success({"updated_indices": 2, "failures": False, "failed_indices": []})


def test_attach_ism_policy_adds_unmanaged_indices_and_moves_the_rest() -> None:
    transport = _SeqTransport(STATUS, ADD_REFUSES_C, ALL_DONE)
    res = OpensearchClient(transport).attach_ism_policy("x-*", "p")
    assert res
    assert transport.paths == [
        "_plugins/_ism/explain/x-*",
        "_plugins/_ism/add/a,c",
        "_plugins/_ism/change_policy/b,c",
    ]


def test_attach_ism_policy_without_force_reports_indices_with_a_policy() -> None:
    transport = _SeqTransport(STATUS, ADD_REFUSES_C)
    res = OpensearchClient(transport).attach_ism_policy("x-*", "p", force=False)
    assert not res
    assert res.reason == (
        "b: already has a policy old; c: This index already has a policy"
    )
    assert transport.paths == ["_plugins/_ism/explain/x-*", "_plugins/_ism/add/a,c"]


def test_attach_ism_policy_reports_an_index_the_move_refused() -> None:
    also_refused = Success(
        {
            "updated_indices": 1,
            "failures": True,
            "failed_indices": [
                {
                    "index_name": "c",
                    "reason": "This index already has a policy; then index is closed",
                }
            ],
        }
    )
    transport = _SeqTransport(STATUS, ADD_REFUSES_C, also_refused)
    res = OpensearchClient(transport).attach_ism_policy("x-*", "p")
    assert not res
    assert res.reason == "c: This index already has a policy; then index is closed"
