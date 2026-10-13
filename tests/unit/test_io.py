# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for opensearch_client.cli.io: input resolution and output rendering."""

import io
import json
import os
import sys
import tempfile
from argparse import Namespace
from datetime import datetime, timezone

import yaml

from opensearch_client.cli.io import (
    emit,
    parse_json_object,
    render,
    resolve_source,
    resolve_time,
    time_range_filter,
)
from opensearch_client.result import Failure


def _args(since=None, until=None, time_field="@timestamp") -> Namespace:
    return Namespace(since=since, until=until, time_field=time_field)


def test_resolve_source_returns_a_literal_value_unchanged() -> None:
    assert resolve_source("`f` = 'v'") == "`f` = 'v'"


def test_parse_json_object_returns_none_on_bad_or_non_object_input() -> None:
    assert parse_json_object('{"a": 1}', "body") == {"a": 1}
    # A non-object (a list) and invalid JSON both return None, not exit.
    assert parse_json_object("[1, 2]", "body") is None
    assert parse_json_object("{not json", "body") is None


def test_resolve_source_reads_and_strips_an_at_prefixed_file() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "predicate.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("  rule.level < 3\n")
        assert resolve_source("@" + path) == "rule.level < 3"


def test_resolve_source_reads_and_strips_stdin_on_dash() -> None:
    saved_stdin = sys.stdin
    sys.stdin = io.StringIO("rule.level < 3\n")
    try:
        assert resolve_source("-") == "rule.level < 3"
    finally:
        sys.stdin = saved_stdin


def test_resolve_source_exits_with_code_2_on_an_unreadable_file() -> None:
    with tempfile.TemporaryDirectory() as directory:
        missing = os.path.join(directory, "nope.txt")
        try:
            resolve_source("@" + missing)
            assert False, "expected SystemExit for a missing file"
        except SystemExit as exit_error:
            assert exit_error.code == 2


def test_emit_exits_with_code_1_on_a_failed_result() -> None:
    try:
        emit(Failure("boom", status=500), "Anything", "json")
        assert False, "expected SystemExit for a failed result"
    except SystemExit as exit_error:
        assert exit_error.code == 1


def test_resolve_time() -> None:
    # Absolute values pass through; a relative offset is resolved against now.
    assert resolve_time("2026-03-14T00:00:00") == "2026-03-14T00:00:00"
    moment = datetime.strptime(resolve_time("-2h"), "%Y-%m-%dT%H:%M:%SZ")
    delta = datetime.now(timezone.utc) - moment.replace(tzinfo=timezone.utc)
    assert abs(delta.total_seconds() - 7200) < 5


def test_time_range_filter() -> None:
    assert time_range_filter(_args()) is None
    both = time_range_filter(_args(since="2026-03-14", until="2026-03-15"))
    assert both == {"range": {"@timestamp": {"gte": "2026-03-14", "lt": "2026-03-15"}}}
    custom = time_range_filter(_args(since="2026-03-14", time_field="event.created"))
    assert custom == {"range": {"event.created": {"gte": "2026-03-14"}}}


def test_yaml_is_insertion_ordered_without_trailing_newline() -> None:
    rendered = render({"b": 1, "a": 2}, "yaml")
    assert rendered == "b: 1\na: 2"
    assert yaml.safe_load(rendered) == {"b": 1, "a": 2}


def test_json_round_trips() -> None:
    data = [{"a": 1}, {"a": 2}]
    assert json.loads(render(data, "json")) == data


def test_csv_flattens_nested_objects_into_dotted_columns() -> None:
    rows = [{"source": {"ip": "10.0.0.5"}, "rule": {"level": 3}}]
    assert render(rows, "csv") == "source.ip,rule.level\n10.0.0.5,3"


def test_csv_unions_columns_and_blanks_missing_cells() -> None:
    rows = [{"a": 1}, {"a": 2, "b": 3}]
    # 'b' first appears in the second row, so it is appended after 'a'.
    assert render(rows, "csv") == "a,b\n1,\n2,3"


def test_csv_json_encodes_list_cells() -> None:
    rows = [{"tags": ["x", "y"]}]
    assert render(rows, "csv") == 'tags\n"[""x"", ""y""]"'


def test_csv_wraps_a_single_object_as_one_row() -> None:
    assert render({"a": 1, "b": 2}, "csv") == "a,b\n1,2"


def test_csv_puts_bare_scalars_under_a_value_column() -> None:
    assert render(["7.10.2", "2.11.0"], "csv") == "value\n7.10.2\n2.11.0"


def test_tsv_uses_tabs() -> None:
    assert render([{"a": 1, "b": 2}], "tsv") == "a\tb\n1\t2"
