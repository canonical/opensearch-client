# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""``os-cli search``: find the newest documents matching exact FIELD=VALUE terms.

A convenience term-lookup (not a query language): each FIELD=VALUE is an exact
match, all ANDed, and the newest ``--count`` matches are returned most-recent
first. ``--count-only`` prints just the number of matches instead.
"""

import logging
import sys
from argparse import Namespace, _SubParsersAction
from typing import Any

from opensearch_client.cli.diagnostics import diagnose
from opensearch_client.cli.io import (
    add_format_argument,
    add_time_range_arguments,
    emit,
    time_range_filter,
)
from opensearch_client.client import OpensearchClient

NAME = "search"


def add_subparser(subparsers: _SubParsersAction) -> None:
    """Register the ``search`` subcommand.

    Args:
        subparsers: the top-level subparsers action to register under.
    """
    parser = subparsers.add_parser(
        NAME, help="find the newest documents matching exact FIELD=VALUE terms"
    )
    parser.add_argument(
        "terms",
        nargs="+",
        metavar="FIELD=VALUE",
        help="exact field=value term filters, ANDed together",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        help="number of most-recent documents to return (default: 1)",
    )
    parser.add_argument(
        "--count-only",
        action="store_true",
        help="print only the number of matching documents (via _count)",
    )
    parser.add_argument(
        "--index",
        help="index or pattern to search (default: the configured OPENSEARCH_INDEX)",
    )
    add_time_range_arguments(parser)
    add_format_argument(parser)


def build_term_search(terms: list[tuple[str, list[str]]], size: int) -> dict[str, Any]:
    """Build an OpenSearch _search body that ANDs the given term filters.

    A single value becomes a ``term`` filter; several become a ``terms`` filter.
    These match exact (keyword) field values, mirroring a dashboard filter.

    Results are sorted by ``@timestamp`` descending so we always get the most
    recent ``size`` documents. ``unmapped_type`` keeps the sort from erroring on
    an index in the pattern that lacks the field.

    Args:
        terms: each field name with the values it may match.
        size: how many documents to return.

    Returns:
        The ``_search`` body.
    """
    filters: list[dict[str, Any]] = []
    for field, values in terms:
        if len(values) == 1:
            filters.append({"term": {field: values[0]}})
        else:
            filters.append({"terms": {field: values}})
    return {
        "size": size,
        "sort": [{"@timestamp": {"order": "desc", "unmapped_type": "date"}}],
        "query": {"bool": {"filter": filters}},
    }


def _unmapped_fields(
    client: OpensearchClient, fields: list[str], index: str | None
) -> list[str]:
    """Return the term fields that are absent from the index mapping.

    An unmapped field never matches a term filter (though it may sit in _source), a
    common and confusing cause of an empty result. A field whose mapping cannot be
    read is treated as mapped, so the caller never warns on a guess.

    Args:
        client: used to look up each field's mapping.
        fields: the term field names to check.
        index: index or pattern to check the mapping in (None uses the default).

    Returns:
        The subset of ``fields`` that are confidently unmapped.
    """
    unmapped: list[str] = []
    for field in fields:
        res = client.field_mapping(field, index=index)
        if not res or not res.data:
            continue  # cannot read the mapping; do not guess
        # field_mapping returns {index: {"mappings": {field: {...}}}}; empty = unmapped.
        if not any(body.get("mappings") for body in res.data.values()):
            unmapped.append(field)
    return unmapped


def run(args: Namespace, client: OpensearchClient) -> None:
    """Run the term search against the client.

    Args:
        args: the parsed command-line arguments.
        client: the client to search with.
    """
    terms: list[tuple[str, list[str]]] = []
    for token in args.terms:
        if "=" not in token:
            logging.error("search term must be FIELD=VALUE: %r", token)
            sys.exit(2)
        field, value = token.split("=", 1)
        terms.append((field, [value]))

    body = build_term_search(terms, size=args.count)
    time_filter = time_range_filter(args)
    if time_filter is not None:
        body["query"]["bool"]["filter"].append(time_filter)

    if args.count_only:
        result = client.count(body["query"], index=args.index)
        label, empty = "Count", bool(result) and result.data == 0
    else:
        result = client.search(body, index=args.index)
        label, empty = "Search", bool(result) and not result.data

    if empty:
        unmapped = _unmapped_fields(client, [field for field, _ in terms], args.index)
        if unmapped:
            logging.warning(
                "no matches: %s not mapped, so a term filter never matches them "
                "(they may still be in _source); check `os-cli index mapping ...`",
                ", ".join(repr(field) for field in unmapped),
            )
    emit(diagnose(result), label, args.format)
