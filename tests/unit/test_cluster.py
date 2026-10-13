# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for opensearch_client.cli.cluster."""

import yaml
from helpers import RecordingTransport, run_cli

from opensearch_client.cli import cluster


def test_delete_template_dry_run_reports_patterns_without_deleting() -> None:
    template = {"name": "t", "index_template": {"index_patterns": ["a-*", "b-*"]}}
    transport = RecordingTransport({"index_templates": [template]})
    output = run_cli(cluster, "cluster", ["delete", "template", "t"], transport)
    assert [method for method, _, _ in transport.calls] == ["GET"]
    assert yaml.safe_load(output) == {
        "template": "t",
        "dry_run": True,
        "index_patterns": ["a-*", "b-*"],
    }


def test_delete_component_template_dry_run_names_its_users_without_deleting() -> None:
    users = [
        {"name": "uses-it", "index_template": {"composed_of": ["c", "other"]}},
        {"name": "does-not", "index_template": {"composed_of": ["other"]}},
    ]
    transport = RecordingTransport({"index_templates": users})
    output = run_cli(
        cluster, "cluster", ["delete", "component-template", "c"], transport
    )
    assert [method for method, _, _ in transport.calls] == ["GET", "GET"]
    assert yaml.safe_load(output) == {
        "component_template": "c",
        "dry_run": True,
        "used_by": ["uses-it"],
    }


def test_delete_refuses_a_name_that_could_match_several_templates() -> None:
    for target in ("template", "component-template"):
        for name in ("t-*", "a,b"):
            transport = RecordingTransport({})
            try:
                run_cli(
                    cluster, "cluster", ["delete", target, name, "--apply"], transport
                )
                assert False, "expected SystemExit"
            except SystemExit as exit_error:
                assert exit_error.code == 2
            assert transport.calls == [], (target, name)
