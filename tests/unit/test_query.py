# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for opensearch_client.cli.query helpers."""

from argparse import Namespace

from opensearch_client.cli import query


def _args(since=None, until=None, time_field="@timestamp") -> Namespace:
    return Namespace(since=since, until=until, time_field=time_field)


def test_parse_dsl_returns_a_json_object() -> None:
    assert query._parse_dsl('{"query": {"match_all": {}}}') == {
        "query": {"match_all": {}}
    }


def test_parse_dsl_exits_on_invalid_json() -> None:
    try:
        query._parse_dsl("{not json")
        assert False, "expected SystemExit for invalid JSON"
    except SystemExit as exit_error:
        assert exit_error.code == 2


def test_parse_dsl_exits_when_top_level_is_not_an_object() -> None:
    try:
        query._parse_dsl("[1, 2, 3]")
        assert False, "expected SystemExit for a non-object DSL"
    except SystemExit as exit_error:
        assert exit_error.code == 2


def test_search_body_wraps_a_bare_query_object() -> None:
    bare = {"term": {"event.action": "logon"}}
    assert query._search_body_from_dsl(bare) == {"query": bare}


def test_search_body_passes_through_a_full_search_body() -> None:
    body = {"size": 5, "query": {"match_all": {}}}
    assert query._search_body_from_dsl(body) == body


def test_query_from_dsl_extracts_the_query_of_a_full_body() -> None:
    body = {"size": 5, "query": {"match_all": {}}}
    assert query._query_from_dsl(body) == {"match_all": {}}


def test_query_from_dsl_returns_a_bare_query_object_as_is() -> None:
    bare = {"term": {"event.action": "logon"}}
    assert query._query_from_dsl(bare) == bare


def test_with_time_filter() -> None:
    inner = {"term": {"a": 1}}
    time_filter = {"range": {"@timestamp": {"gte": "X"}}}
    assert query._with_time_filter(inner, time_filter) == {
        "bool": {"filter": [inner, time_filter]}
    }
    assert query._with_time_filter(inner, None) is inner


def test_ppl_with_time() -> None:
    args = _args(since="2026-03-14T00:00:00", until="2026-03-15T00:00:00")
    assert query._ppl_with_time("source=logs-*", args) == (
        "source=logs-* | where `@timestamp` >= '2026-03-14T00:00:00' "
        "and `@timestamp` < '2026-03-15T00:00:00'"
    )
    assert query._ppl_with_time("source=logs-*", _args()) == "source=logs-*"
