# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for opensearch_client.triage.

The functional tests (tests/functional/) drive init/eliminate/status against a
real cluster, so these cover only what that layer cannot: the explain-plan
parsing, the dry-run and cross-check safety paths, and validation/errors.
"""

from argparse import Namespace
from typing import Any

from opensearch_client import triage
from opensearch_client.client import OpensearchClient
from opensearch_client.result import Failure, OpensearchResult, Success


class FakeTransport:
    """Returns preset responses in FIFO order (the last repeats) and records calls."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, Any]] = []

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        content_type: str = "application/json",
        timeout: int = 30,
    ) -> OpensearchResult[Any]:
        self.calls.append((method, path, body))
        data = (
            self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        )
        return Success(data)


def _client(responses: list[dict[str, Any]]) -> tuple[OpensearchClient, FakeTransport]:
    transport = FakeTransport(responses)
    return OpensearchClient(transport), transport


def _explain(query_json: str) -> dict[str, Any]:
    # The pushed-down DSL lives as JSON inside a stringified OpenSearchQueryRequest.
    return {
        "root": {
            "description": {"request": 'sourceBuilder={"query": ' + query_json + "}"}
        }
    }


def _status(counts: dict[int, int]) -> dict[str, Any]:
    buckets = [{"key": k, "doc_count": c} for k, c in counts.items()]
    return {
        "hits": {"total": {"value": sum(counts.values())}},
        "aggregations": {
            "untagged": {"doc_count": 0},
            "by_layer": {"buckets": buckets},
        },
    }


def test_extract_json_object_respects_string_literals() -> None:
    # Braces inside a string value must not end the object early.
    text = 'x {"a": "has } brace", "b": {"c": 1}} y'
    assert (
        triage._extract_json_object(text, text.index("{"))
        == '{"a": "has } brace", "b": {"c": 1}}'
    )


def test_where_to_dsl_parses_pushdown_and_errors_without_it() -> None:
    client, transport = _client([_explain('{"term": {"f.keyword": {"value": "v"}}}')])
    assert triage.where_to_dsl(client, "idx", "`f` = 'v'") == {
        "term": {"f.keyword": {"value": "v"}}
    }
    assert transport.calls[0][:2] == ("POST", "_plugins/_sql/_explain")

    client, _ = _client([{"root": {"name": "in-engine", "children": []}}])
    try:
        triage.where_to_dsl(client, "idx", "x = 1")
        assert False, "expected ValueError when nothing pushes down"
    except ValueError as e:
        assert "pushed-down" in str(e)


def test_eliminate_dry_run_counts_and_cross_checks_without_writing() -> None:
    responses = [
        _explain('{"term": {"a.keyword": {"value": "x"}}}'),
        {"datarows": [[3]]},  # SQL COUNT(*) = 3
        {"count": 3},  # DSL matches 3 -> cross-check passes
        {"count": 2},  # 2 still untriaged -> would be tagged
        {"hits": {"hits": [{"_source": {"rule": {"level": 1}}}]}},
    ]
    client, transport = _client(responses)
    result = triage.eliminate(client, "idx", "a = 'x'", 1, "noise", apply=False)
    assert result["matched"] == 3
    assert result["untriaged_to_tag"] == 2
    assert result["already_triaged_skipped"] == 1
    assert result["sample"] == [{"rule": {"level": 1}}]
    assert not any("_update_by_query" in path for _, path, _ in transport.calls)


def test_eliminate_refuses_when_translation_is_inexact() -> None:
    # SQL COUNT(*)=3 but the translated DSL matches 5 -> refuse rather than mis-tag.
    client, _ = _client(
        [_explain('{"term": {"a": {"value": "x"}}}'), {"datarows": [[3]]}, {"count": 5}]
    )
    try:
        triage.eliminate(client, "idx", "a = 'x'", 1, "x", apply=False)
        assert False, "expected ValueError on inexact translation"
    except ValueError as e:
        assert "translate exactly" in str(e)


def test_restore_dry_run_selects_by_layer_or_range_without_writing() -> None:
    sample = {"hits": {"hits": [{"_source": {"a": 1}}]}}
    # --layer: an exact term query on that pass.
    client, transport = _client([{"count": 3}, sample])
    result = triage.restore(client, "idx", 2, None, apply=False)
    assert result["matched"] == 3
    assert result["query_dsl"] == {"term": {"triage.layer": 2}}
    # --from-layer: a range query over that pass and above.
    client, _ = _client([{"count": 5}, sample])
    result = triage.restore(client, "idx", None, 2, apply=False)
    assert result["query_dsl"] == {"range": {"triage.layer": {"gte": 2}}}
    assert not any("_update_by_query" in path for _, path, _ in transport.calls)


def test_restore_rejects_layer_below_one() -> None:
    client, _ = _client([{}])
    try:
        triage.restore(client, "idx", 0, None, apply=False)
        assert False, "expected ValueError for layer < 1"
    except ValueError as e:
        assert "layer" in str(e)


def test_eliminate_rejects_layer_below_one() -> None:
    client, _ = _client([{}])
    try:
        triage.eliminate(client, "idx", "x = 1", 0, "x", apply=False)
        assert False, "expected ValueError for layer < 1"
    except ValueError as e:
        assert "layer" in str(e)


def test_next_layer_is_one_past_the_highest_used_else_one() -> None:
    client, _ = _client([_status({-1: 5, 1: 3, 2: 2})])
    assert triage.next_layer(client, "idx") == 3
    client, _ = _client([_status({-1: 5})])
    assert triage.next_layer(client, "idx") == 1


def test_status_summarizes_layers_and_missing_field() -> None:
    client, transport = _client([_status({-1: 4, 1: 3, 2: 2})])
    result = triage.status(client, "hunt")
    assert result["untriaged"] == 4
    assert result["eliminated_by_layer"] == {1: 3, 2: 2}
    assert transport.calls[0][:2] == (
        "POST",
        "hunt/_search",
    )  # the named index, not a default

    # An index never through init has no triage.layer anywhere.
    missing = {
        "hits": {"total": {"value": 5}},
        "aggregations": {"untagged": {"doc_count": 5}, "by_layer": {"buckets": []}},
    }
    assert triage.status(_client([missing])[0], "hunt")["untagged_missing_field"] == 5


def test_run_wraps_a_failed_call_as_a_result() -> None:
    class Failing:
        """A transport that answers every request with a failure."""

        def request(
            self, method, path, body=None, content_type="application/json", timeout=30
        ):
            return Failure("503 unavailable")

    result = triage.run(
        Namespace(command="status", index="x"), OpensearchClient(Failing())
    )
    assert not result.ok
    assert "503 unavailable" in result.reason
