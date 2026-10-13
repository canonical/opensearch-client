# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for opensearch_client.cli.index."""

import json
from typing import Any

from helpers import RecordingTransport, run_cli

from opensearch_client.cli import index


def test_load_documents_parses_each_format() -> None:
    docs = [{"a": 1}, {"a": 2}]
    assert index._load_documents('[{"a": 1}, {"a": 2}]', "json") == docs
    assert index._load_documents('{"a": 1}\n{"a": 2}\n', "jsonl") == docs
    assert index._load_documents("- a: 1\n- a: 2\n", "yaml") == docs
    # A single object is wrapped into a one-document list.
    assert index._load_documents('{"a": 1}', "json") == [{"a": 1}]


def test_load_documents_exits_on_bad_or_non_object_input() -> None:
    for text, fmt in [("{not json", "json"), ("[1, 2]", "json")]:
        try:
            index._load_documents(text, fmt)
            assert False, "expected SystemExit"
        except SystemExit as exit_error:
            assert exit_error.code == 2


def test_rollover_is_a_dry_run_unless_applied_and_passes_settings_through() -> None:
    settings = {"index.number_of_shards": 1}
    cases: list[tuple[list[str], tuple[str, str, Any]]] = [
        (["rollover", "w"], ("POST", "w/_rollover?dry_run=true", None)),
        (["rollover", "w", "--apply"], ("POST", "w/_rollover?dry_run=false", None)),
        (
            ["rollover", "w", "--apply", "--settings", json.dumps(settings)],
            ("POST", "w/_rollover?dry_run=false", {"settings": settings}),
        ),
    ]
    for argv, expected in cases:
        transport = RecordingTransport()
        run_cli(index, "index", argv, transport)
        assert transport.calls == [expected], argv


def test_rollover_rejects_settings_that_are_not_a_json_object() -> None:
    transport = RecordingTransport()
    try:
        run_cli(
            index, "index", ["rollover", "w", "--apply", "--settings", "[1]"], transport
        )
        assert False, "expected SystemExit"
    except SystemExit as exit_error:
        assert exit_error.code == 2
    assert transport.calls == []
