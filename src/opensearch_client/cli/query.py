# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""``os-cli query``: run a query in a chosen language against the cluster.

One verb per invocation, each printing its result as YAML by default:
  query sql <query>   run an OpenSearch SQL query (--explain shows the plan)
  query ppl <query>   run an OpenSearch PPL query (--explain shows the plan)
  query dsl <query>   run a raw query DSL (--count-only returns just the count)

Each <query> may be given literally, as ``-`` to read it from stdin, or as
``@PATH`` to read it from a file.

Connection is read from the OPENSEARCH_* environment variables (see
opensearch_client.config) so the password never lands in shell history.
"""

import json
import logging
import sys
from argparse import Namespace, _SubParsersAction
from typing import Any

from opensearch_client.cli.diagnostics import diagnose
from opensearch_client.cli.io import (
    add_format_argument,
    add_time_range_arguments,
    emit,
    resolve_source,
    resolve_time,
    time_range_filter,
)
from opensearch_client.client import OpensearchClient

NAME = "query"

_SOURCE_EPILOG = (
    "The <query> may be given literally, as '-' to read it from stdin, or as "
    "'@PATH' to read it from a file."
)


def add_subparser(subparsers: _SubParsersAction) -> None:
    """Register ``query`` and its sql/ppl/dsl language verbs."""
    parser = subparsers.add_parser(NAME, help="run a query (SQL, PPL, or raw DSL)")
    languages = parser.add_subparsers(dest="language", required=True)

    sql = languages.add_parser(
        "sql", help="run an OpenSearch SQL query", epilog=_SOURCE_EPILOG
    )
    sql.add_argument("query", help="the SQL query (or '-' / '@PATH')")
    sql.add_argument(
        "--explain",
        action="store_true",
        help="print the execution plan (the pushed-down DSL) instead of running it",
    )
    add_time_range_arguments(sql)
    add_format_argument(sql)

    ppl = languages.add_parser(
        "ppl", help="run an OpenSearch PPL query (v2 engine)", epilog=_SOURCE_EPILOG
    )
    ppl.add_argument("query", help="the PPL query (or '-' / '@PATH')")
    ppl.add_argument(
        "--explain",
        action="store_true",
        help="print the execution plan (the pushed-down DSL) instead of running it",
    )
    add_time_range_arguments(ppl)
    add_format_argument(ppl)

    dsl = languages.add_parser(
        "dsl",
        help="run a raw query DSL (a query object or full search body)",
        epilog=_SOURCE_EPILOG,
    )
    dsl.add_argument("query", help="the query DSL as JSON (or '-' / '@PATH')")
    dsl.add_argument(
        "--count-only",
        action="store_true",
        help="route to _count and print only the number of matching documents",
    )
    add_time_range_arguments(dsl)
    add_format_argument(dsl)


def _parse_dsl(text: str) -> dict[str, Any]:
    """Parse dsl text into a JSON object, or log an instructive error and exit 2."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        logging.error(f"query dsl is not valid JSON: {error}")
        sys.exit(2)
    if not isinstance(parsed, dict):
        logging.error(
            "query dsl must be a JSON object: a bare query object or a full search body"
        )
        sys.exit(2)
    return parsed


def _search_body_from_dsl(parsed: dict[str, Any]) -> dict[str, Any]:
    """A _search body from parsed DSL: used as-is if it already has a top-level
    ``query``, otherwise wrapped so the object is treated as the query."""
    return parsed if "query" in parsed else {"query": parsed}


def _query_from_dsl(parsed: dict[str, Any]) -> dict[str, Any]:
    """The bare query DSL for _count: the ``query`` of a full body, else the object itself."""
    return parsed["query"] if "query" in parsed else parsed


def _reject_time_with_explain(args: Namespace) -> None:
    """--explain shows the query's plan, which a separate time filter cannot alter."""
    if args.since is not None or args.until is not None:
        logging.error("--since / --until cannot be combined with --explain")
        sys.exit(2)


def _with_time_filter(
    query: dict[str, Any], time_filter: dict[str, Any] | None
) -> dict[str, Any]:
    """Combine a query DSL with a time-range filter (via a bool filter), if any."""
    if time_filter is None:
        return query
    return {"bool": {"filter": [query, time_filter]}}


def _ppl_with_time(ppl: str, args: Namespace) -> str:
    """Append a ``| where`` stage bounding the time field, if --since / --until set."""
    conditions: list[str] = []
    if args.since is not None:
        conditions.append(f"`{args.time_field}` >= '{resolve_time(args.since)}'")
    if args.until is not None:
        conditions.append(f"`{args.time_field}` < '{resolve_time(args.until)}'")
    if not conditions:
        return ppl
    return f"{ppl} | where {' and '.join(conditions)}"


def _run_sql(client: OpensearchClient, args: Namespace) -> None:
    text = resolve_source(args.query)
    if args.explain:
        _reject_time_with_explain(args)
        emit(diagnose(client.explain(text)), "Explain", args.format)
    else:
        result = client.sql(text, time_range_filter(args))
        emit(diagnose(result), "SQL query", args.format)


def _run_ppl(client: OpensearchClient, args: Namespace) -> None:
    text = resolve_source(args.query)
    if args.explain:
        _reject_time_with_explain(args)
        result = client.explain(text, query_type="ppl")
        emit(diagnose(result), "PPL explain", args.format)
    else:
        result = client.ppl(_ppl_with_time(text, args))
        emit(diagnose(result), "PPL query", args.format)


def _run_dsl(client: OpensearchClient, args: Namespace) -> None:
    parsed = _parse_dsl(resolve_source(args.query))
    time_filter = time_range_filter(args)
    if args.count_only:
        query = _with_time_filter(_query_from_dsl(parsed), time_filter)
        emit(diagnose(client.count(query)), "Count", args.format)
    else:
        body = _search_body_from_dsl(parsed)
        body["query"] = _with_time_filter(body["query"], time_filter)
        emit(diagnose(client.search(body)), "DSL query", args.format)


def run(args: Namespace, client: OpensearchClient) -> None:
    """Run the chosen language verb against the client."""
    if args.language == "sql":
        _run_sql(client, args)
    elif args.language == "ppl":
        _run_ppl(client, args)
    elif args.language == "dsl":
        _run_dsl(client, args)
