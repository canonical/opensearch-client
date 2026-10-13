# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""``os-cli cluster``: cluster-level inspection and resource management."""

import logging
import sys
from argparse import Namespace, _SubParsersAction
from typing import Any

from opensearch_client.cli.diagnostics import diagnose
from opensearch_client.cli.io import (
    add_format_argument,
    emit,
    parse_json_object,
    render,
    resolve_source,
)
from opensearch_client.client import OpensearchClient

NAME = "cluster"


def add_subparser(subparsers: _SubParsersAction) -> None:
    """Register ``cluster`` and its inspection verbs."""
    parser = subparsers.add_parser(
        NAME, help="cluster-level inspection and resource management"
    )
    operations = parser.add_subparsers(dest="operation", required=True)

    versions = operations.add_parser(
        "versions", help="show OpenSearch and installed-plugin versions"
    )
    add_format_argument(versions)

    pipeline = operations.add_parser(
        "pipeline", help="show ingest pipelines (all, or one named)"
    )
    pipeline.add_argument(
        "name", nargs="?", metavar="NAME", help="pipeline to show (default: all)"
    )
    add_format_argument(pipeline)

    template = operations.add_parser(
        "template", help="show index templates (all, or those matching NAME)"
    )
    template.add_argument(
        "name",
        nargs="?",
        metavar="NAME",
        help="template to show, wildcards allowed (default: all)",
    )
    template.add_argument(
        "--legacy",
        action="store_true",
        help="show legacy (_template) templates instead of composable ones",
    )
    add_format_argument(template)

    component = operations.add_parser(
        "component-template",
        help="show component templates (all, or those matching NAME)",
    )
    component.add_argument(
        "name",
        nargs="?",
        metavar="NAME",
        help="component template to show, wildcards allowed (default: all)",
    )
    add_format_argument(component)

    set_parser = operations.add_parser(
        "set", help="set a cluster-level resource (e.g. an ingest pipeline)"
    )
    set_targets = set_parser.add_subparsers(dest="set_target", required=True)
    set_pipeline = set_targets.add_parser(
        "pipeline",
        help="create or replace a named ingest pipeline",
        epilog="SOURCE is the pipeline definition as JSON: '@PATH' reads a file, "
        "'-' reads stdin, or give the text literally.",
    )
    set_pipeline.add_argument("name", metavar="NAME", help="the pipeline name")
    set_pipeline.add_argument(
        "source",
        metavar="SOURCE",
        help="the pipeline definition as JSON ('@PATH', '-', or literal)",
    )
    add_format_argument(set_pipeline)
    set_template = set_targets.add_parser(
        "template",
        help="create or replace a composable index template",
        epilog="SOURCE is the template definition as JSON: '@PATH' reads a file, "
        "'-' reads stdin, or give the text literally.",
    )
    set_template.add_argument("name", metavar="NAME", help="the template name")
    set_template.add_argument(
        "source",
        metavar="SOURCE",
        help="the template definition as JSON ('@PATH', '-', or literal)",
    )
    add_format_argument(set_template)
    set_component = set_targets.add_parser(
        "component-template",
        help="create or replace a component template",
        epilog="SOURCE is the component template definition as JSON: '@PATH' reads "
        "a file, '-' reads stdin, or give the text literally.",
    )
    set_component.add_argument(
        "name", metavar="NAME", help="the component template name"
    )
    set_component.add_argument(
        "source",
        metavar="SOURCE",
        help="the component template definition as JSON ('@PATH', '-', or literal)",
    )
    add_format_argument(set_component)

    delete_parser = operations.add_parser(
        "delete", help="delete a cluster-level resource; dry-run unless --apply"
    )
    delete_targets = delete_parser.add_subparsers(dest="delete_target", required=True)
    delete_template = delete_targets.add_parser(
        "template",
        help="delete a composable index template; dry-run unless --apply",
    )
    delete_template.add_argument(
        "name",
        metavar="NAME",
        help="the template to delete (one name; no wildcards or commas)",
    )
    delete_template.add_argument(
        "--apply",
        action="store_true",
        help="actually delete; without it, only report the patterns it covers",
    )
    add_format_argument(delete_template)
    delete_component = delete_targets.add_parser(
        "component-template",
        help="delete a component template; dry-run unless --apply",
    )
    delete_component.add_argument(
        "name",
        metavar="NAME",
        help="the component template to delete (one name; no wildcards or commas)",
    )
    delete_component.add_argument(
        "--apply",
        action="store_true",
        help="actually delete; without it, only report the index templates using it",
    )
    add_format_argument(delete_component)

    simulate = operations.add_parser(
        "simulate", help="resolve the settings and mappings a new index would get"
    )
    simulate_targets = simulate.add_subparsers(dest="simulate_target", required=True)
    simulate_template = simulate_targets.add_parser(
        "template",
        help="resolve what putting a template definition as NAME would give a "
        "new index, without installing it",
        epilog="SOURCE is the template definition as JSON: '@PATH' reads a file, "
        "'-' reads stdin, or give the text literally.",
    )
    simulate_template.add_argument(
        "name", metavar="NAME", help="the template name the definition would be put as"
    )
    simulate_template.add_argument(
        "source",
        metavar="SOURCE",
        help="the template definition as JSON ('@PATH', '-', or literal)",
    )
    add_format_argument(simulate_template)
    simulate_index = simulate_targets.add_parser(
        "index", help="resolve what the installed templates give a new index"
    )
    simulate_index.add_argument(
        "index", metavar="INDEX", help="the index name to resolve"
    )
    add_format_argument(simulate_index)


def get_versions(client: OpensearchClient) -> dict[str, Any]:
    """Collect OpenSearch and installed-plugin versions.

    SQL and PPL are both provided by the single ``opensearch-sql`` plugin, so its
    version covers both. Each lookup is independent: if one fails its error is
    recorded and the other is still returned.
    """
    versions: dict[str, Any] = {}
    opensearch = client.opensearch_version()
    versions["opensearch"] = (
        opensearch.data if opensearch.ok else {"error": opensearch.reason}
    )
    plugins = client.plugin_versions()
    versions["plugins"] = plugins.data if plugins.ok else {"error": plugins.reason}
    return versions


def run(args: Namespace, client: OpensearchClient) -> None:
    """Run the chosen operation against the client."""
    if args.operation == "versions":
        print(render(get_versions(client), args.format))
    elif args.operation == "pipeline":
        emit(diagnose(client.get_pipeline(args.name)), "Pipeline", args.format)
    elif args.operation == "set" and args.set_target == "pipeline":
        body = parse_json_object(resolve_source(args.source), "pipeline definition")
        if body is None:
            sys.exit(2)
        result = client.put_pipeline(args.name, body)
        emit(diagnose(result), "Set pipeline", args.format)
    elif args.operation == "template":
        if args.legacy:
            result = client.get_legacy_template(args.name)
        else:
            result = client.get_index_template(args.name)
        emit(diagnose(result), "Template", args.format)
    elif args.operation == "set" and args.set_target == "template":
        body = parse_json_object(resolve_source(args.source), "template definition")
        if body is None:
            sys.exit(2)
        result = client.put_index_template(args.name, body)
        emit(diagnose(result), "Set template", args.format)
    elif args.operation == "delete" and args.delete_target == "template":
        if "*" in args.name or "," in args.name:
            logging.error(
                f"template name {args.name!r} must not contain a wildcard or comma"
            )
            sys.exit(2)
        if args.apply:
            result = client.delete_index_template(args.name)
            emit(diagnose(result), "Delete template", args.format)
            return

        result = client.get_index_template(args.name)
        if not result:
            emit(diagnose(result), "Delete template", args.format)
            return
        [entry] = result.data["index_templates"]
        patterns = entry["index_template"].get("index_patterns", [])
        summary = {"template": args.name, "dry_run": True, "index_patterns": patterns}
        print(render(summary, args.format))
    elif args.operation == "component-template":
        result = client.get_component_template(args.name)
        emit(diagnose(result), "Component template", args.format)
    elif args.operation == "set" and args.set_target == "component-template":
        body = parse_json_object(
            resolve_source(args.source), "component template definition"
        )
        if body is None:
            sys.exit(2)
        result = client.put_component_template(args.name, body)
        emit(diagnose(result), "Set component template", args.format)
    elif args.operation == "delete" and args.delete_target == "component-template":
        if "*" in args.name or "," in args.name:
            logging.error(
                f"component template name {args.name!r} must not contain a wildcard "
                "or comma"
            )
            sys.exit(2)
        if args.apply:
            result = client.delete_component_template(args.name)
            emit(diagnose(result), "Delete component template", args.format)
            return
        result = client.get_component_template(args.name)
        if not result:
            emit(diagnose(result), "Delete component template", args.format)
            return
        result = client.get_index_template()
        if not result:
            emit(diagnose(result), "Delete component template", args.format)
            return
        used_by = [
            entry["name"]
            for entry in result.data.get("index_templates", [])
            if args.name in entry["index_template"].get("composed_of", [])
        ]
        summary = {"component_template": args.name, "dry_run": True, "used_by": used_by}
        print(render(summary, args.format))
    elif args.operation == "simulate" and args.simulate_target == "template":
        body = parse_json_object(resolve_source(args.source), "template definition")
        if body is None:
            sys.exit(2)
        result = client.simulate_template(args.name, body)
        emit(diagnose(result), "Simulate", args.format)
    elif args.operation == "simulate" and args.simulate_target == "index":
        emit(diagnose(client.simulate_index(args.index)), "Simulate", args.format)
