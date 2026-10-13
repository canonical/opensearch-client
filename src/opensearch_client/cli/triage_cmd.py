# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""The ``os-cli triage`` subcommand: layered triage of a copied log index.

Thin CLI over :mod:`opensearch_client.triage`. Builds the client from the OPENSEARCH_*
environment variables, dispatches the chosen triage subcommand, and prints its
result as YAML (or the failure reason to stderr, exiting non-zero).

The triage index is named explicitly with --index / --dest on every subcommand;
it is not taken from OPENSEARCH_INDEX, to avoid ever tagging the wrong (or a
wildcard) index by accident.
"""

import sys
from argparse import Namespace, _SubParsersAction

from opensearch_client import triage
from opensearch_client.cli.diagnostics import diagnose
from opensearch_client.cli.io import add_format_argument, render, resolve_source
from opensearch_client.client import OpensearchClient

NAME = "triage"


def add_subparser(subparsers: _SubParsersAction) -> None:
    """Register the ``triage`` subcommand and its init/eliminate/status verbs."""
    parser = subparsers.add_parser(
        NAME, help="layered threat-hunt triage of a copied log index"
    )
    verbs = parser.add_subparsers(dest="command", required=True)

    init_parser = verbs.add_parser(
        "init",
        help="copy --source into a new writable --dest index, tagging every "
        "document triage.layer=-1 (untriaged)",
    )
    init_parser.add_argument(
        "--source",
        required=True,
        help="source index or pattern to copy the logs of interest from",
    )
    init_parser.add_argument(
        "--dest",
        required=True,
        help="destination triage index to create (must not already exist)",
    )
    add_format_argument(init_parser)

    status_parser = verbs.add_parser(
        "status", help="print triage progress (counts by layer), and exit"
    )
    status_parser.add_argument(
        "--index", required=True, help="the triage index to summarize"
    )
    add_format_argument(status_parser)

    elim = verbs.add_parser(
        "eliminate",
        help="tag the untriaged docs matching a SQL WHERE predicate with a layer, "
        "the predicate, and an explanation (dry-run unless --apply)",
    )
    elim.add_argument("--index", required=True, help="the triage index to tag")
    elim.add_argument(
        "--layer",
        type=int,
        default=None,
        help="the elimination pass number (>= 1); omit to auto-increment to one past "
        "the highest layer already used on the index",
    )
    elim.add_argument(
        "--where",
        required=True,
        help="SQL WHERE predicate selecting documents to eliminate, e.g. "
        '"rule.level < 3"; pass "-" to read the predicate from stdin or "@PATH" to '
        "read it from a file",
    )
    elim.add_argument(
        "--explanation",
        required=True,
        help="short human rationale, stored on each tagged document",
    )
    elim.add_argument(
        "--apply",
        action="store_true",
        help="actually write the tags; without it, only a dry-run count and sample",
    )
    add_format_argument(elim)

    restore = verbs.add_parser(
        "restore",
        help="undo an elimination: reset the matching docs to untriaged, recording "
        "the undo on triage.history (dry-run unless --apply)",
    )
    restore.add_argument(
        "--index", required=True, help="the triage index to restore in"
    )
    which = restore.add_mutually_exclusive_group(required=True)
    which.add_argument(
        "--layer", type=int, help="reset exactly this elimination pass to untriaged"
    )
    which.add_argument(
        "--from-layer",
        type=int,
        metavar="LAYER",
        help="reset this elimination pass and every higher one to untriaged",
    )
    restore.add_argument(
        "--apply",
        action="store_true",
        help="actually restore; without it, only a dry-run count and sample",
    )
    add_format_argument(restore)


def run(args: Namespace, client: OpensearchClient) -> None:
    """Run the chosen triage verb against the client."""
    if getattr(args, "where", None) is not None:
        args.where = resolve_source(args.where)
    result = diagnose(triage.run(args, client))
    if not result.ok:
        print(f"error: {args.command} failed: {result.reason}", file=sys.stderr)
        sys.exit(1)
    print(render(result.data, args.format))
