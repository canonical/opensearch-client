# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""CLI IO: resolve input arguments and render output."""

import csv
import json
import logging
import re
import sys
from argparse import ArgumentParser, Namespace
from datetime import datetime, timedelta, timezone
from io import StringIO
from typing import Any

import yaml

from opensearch_client.result import OpensearchResult

FORMATS = ("yaml", "json", "csv", "tsv")
DEFAULT_FORMAT = "yaml"

DEFAULT_TIME_FIELD = "@timestamp"

# A relative time offset like -24h or -7d: a minus, a count, and a unit.
_OFFSET = re.compile(r"^-(\d+)([smhdw])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def add_format_argument(parser: ArgumentParser) -> None:
    """Add the shared ``--format`` option (yaml/json/csv/tsv, default yaml).

    Args:
        parser: the parser to add the option to.
    """
    parser.add_argument(
        "--format",
        choices=FORMATS,
        default=DEFAULT_FORMAT,
        help="output format for data printed to stdout (default: yaml)",
    )


def resolve_source(value: str) -> str:
    """Resolve a CLI text argument that may name where to read the text from.

    One convention, shared by the query modes and triage's ``--where``:

        <text>   the value is the text itself
        -        read the text from stdin
        @PATH    read the text from the file at PATH

    Stdin and file contents are stripped of surrounding whitespace; a literal
    value is returned unchanged. On an unreadable ``@PATH`` this logs an
    instructive error and exits, rather than letting an OSError traceback reach
    the user.

    Args:
        value: the CLI argument: literal text, ``-``, or ``@PATH``.

    Returns:
        The resolved text.
    """
    if value == "-":
        return sys.stdin.read().strip()
    if value.startswith("@"):
        path = value[1:]
        try:
            with open(path, mode="r", encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError as error:
            logging.error("could not read file %r: %s", path, error)
            sys.exit(2)
    return value


def parse_json_object(text: str, label: str) -> dict[str, Any] | None:
    """Parse text into a JSON object, logging an instructive error on failure.

    Args:
        text: the JSON text to parse (typically from :func:`resolve_source`).
        label: a short noun naming the input for the error message (e.g.
            ``"mappings body"``).

    Returns:
        The parsed object, or None when the text is not a JSON object. The reason
        is logged; the caller decides how to handle it (e.g. exit).
    """
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        logging.error("%s is not valid JSON: %s", label, error)
        return None
    if not isinstance(parsed, dict):
        logging.error("%s must be a JSON object", label)
        return None
    return parsed


def add_time_range_arguments(parser: ArgumentParser) -> None:
    """Add the shared ``--since``, ``--until`` and ``--time-field`` options.

    Args:
        parser: the parser to add the options to.
    """
    parser.add_argument(
        "--since",
        metavar="WHEN",
        help="lower time bound, inclusive: an absolute time (e.g. 2026-03-14 or "
        "2026-03-14T00:00:00) or a relative offset. A relative offset starts with "
        "'-', so pass it with '=': --since=-24h, --since=-7d (units s/m/h/d/w)",
    )
    parser.add_argument(
        "--until",
        metavar="WHEN",
        help="upper time bound, exclusive: same forms as --since (e.g. --until=-1h)",
    )
    parser.add_argument(
        "--time-field",
        default=DEFAULT_TIME_FIELD,
        metavar="FIELD",
        help=f"timestamp field the bounds apply to (default: {DEFAULT_TIME_FIELD})",
    )


def resolve_time(value: str) -> str:
    """Resolve a ``--since`` / ``--until`` value to the string a filter should use.

    A relative offset ``-<N><unit>`` (unit s/m/h/d/w) is subtracted from now and
    returned as an absolute UTC timestamp; any other value is an absolute time and
    is returned unchanged for OpenSearch to parse.

    Args:
        value: the ``--since`` or ``--until`` argument.

    Returns:
        The absolute time string to use in a filter.
    """
    match = _OFFSET.match(value)
    if match is None:
        return value
    seconds = int(match.group(1)) * _UNIT_SECONDS[match.group(2)]
    moment = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def time_range_filter(args: Namespace) -> dict[str, Any] | None:
    """Build a ``range`` query for ``--since`` / ``--until``, if either is set.

    ``--since`` is the inclusive lower bound (``gte``) and ``--until`` the
    exclusive upper bound (``lt``), a half-open window so back-to-back ranges do
    not double-count the boundary.

    Args:
        args: the parsed arguments, with ``since``, ``until`` and ``time_field``.

    Returns:
        The ``range`` query DSL, or None if neither bound is set.
    """
    bounds: dict[str, str] = {}
    if args.since is not None:
        bounds["gte"] = resolve_time(args.since)
    if args.until is not None:
        bounds["lt"] = resolve_time(args.until)
    if not bounds:
        return None
    return {"range": {args.time_field: bounds}}


def _flatten(record: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten nested objects into dotted keys; leave list/scalar values intact.

    Args:
        record: the record to flatten.
        prefix: the dotted key prefix of the enclosing objects.

    Returns:
        The record with nested objects replaced by dotted keys.
    """
    flat: dict[str, Any] = {}
    for key, value in record.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(_flatten(value, dotted))
        else:
            flat[dotted] = value
    return flat


def _cell(value: Any) -> str:
    """Render a single flattened value for a csv/tsv cell.

    Args:
        value: the value to render.

    Returns:
        The cell text: empty for None, JSON for a list or dict.
    """
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, default=str)
    return str(value)


def _delimited(data: Any, delimiter: str) -> str:
    """Render data as delimited text: one row per record, dotted union columns.

    Args:
        data: a record or a list of records.
        delimiter: the column separator.

    Returns:
        The delimited text, without a trailing newline.
    """
    records = data if isinstance(data, list) else [data]
    flat_rows: list[dict[str, Any]] = []
    columns: dict[str, None] = {}
    for record in records:
        row = record if isinstance(record, dict) else {"value": record}
        flat = _flatten(row)
        flat_rows.append(flat)
        for column in flat:
            columns.setdefault(column, None)

    buffer = StringIO()
    writer = csv.writer(buffer, delimiter=delimiter, lineterminator="\n")
    writer.writerow(list(columns))
    for flat in flat_rows:
        writer.writerow([_cell(flat.get(column)) for column in columns])
    return buffer.getvalue().rstrip("\n")


def render(data: Any, fmt: str) -> str:
    """Render data in the chosen format, without a trailing newline.

    Args:
        data: the data to render.
        fmt: one of ``FORMATS``.

    Returns:
        The rendered text.

    Raises:
        ValueError: if ``fmt`` is not a known format.
    """
    if fmt == "yaml":
        return yaml.dump(data, indent=2, sort_keys=False).rstrip("\n")
    if fmt == "json":
        return json.dumps(data, indent=2, default=str)
    if fmt == "csv":
        return _delimited(data, ",")
    if fmt == "tsv":
        return _delimited(data, "\t")
    raise ValueError(f"unknown format: {fmt!r}")


def emit(result: OpensearchResult[Any], label: str, fmt: str) -> None:
    """Print a result's data in the chosen format, or log its reason and exit 1.

    Args:
        result: the result to print.
        label: a short name for the operation, used in the failure message.
        fmt: the output format.
    """
    if not result.ok:
        logging.error("%s failed: %s", label, result.reason)
        sys.exit(1)
    print(render(result.data, fmt))
