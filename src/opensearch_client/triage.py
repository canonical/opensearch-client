# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Functions supporting threat hunting / log triage.

See docs/cli.md for the workflow, and docs/sql-explain.md for the SQL ``_explain``
output that ``where_to_dsl`` and its helpers parse.
"""

import json
import logging
import time
from argparse import Namespace
from datetime import datetime, timezone
from typing import Any

from opensearch_client.client import OpensearchClient
from opensearch_client.result import Failure, OpensearchResult, Success

# The untriaged sentinel. init tags every copied document with this; eliminate
# only ever matches documents still holding it.
UNTRIAGED = -1

# Longer timeout for the tagging update_by_query, which starts a server-side task.
_TAG_TIMEOUT = 300

# Explicit mapping for the triage fields on the destination index. Setting these
# up front (rather than letting them be mapped dynamically on the first tag) keeps
# triage.query/explanation exact (keyword, not analysed text) and triage.layer
# numeric so SQL comparisons like "triage.layer < 3" behave.
_TRIAGE_MAPPING = {
    "properties": {
        "layer": {"type": "integer"},
        "query": {"type": "keyword"},
        "explanation": {"type": "keyword"},
        "at": {"type": "date"},
        "history": {"type": "object", "enabled": False},
    }
}

# The tag is written by this Painless script so multiple fields land in one pass.
# The null guard covers a document that somehow lacks the triage object; after
# init every document already has triage.layer == -1.
_TAG_SCRIPT = (
    "if (ctx._source.triage == null) { ctx._source.triage = new HashMap(); }\n"
    "ctx._source.triage.layer = params.layer;\n"
    "ctx._source.triage.query = params.query;\n"
    "ctx._source.triage.explanation = params.explanation;\n"
    "ctx._source.triage.at = params.at;"
)

# restore records the eliminated state onto triage.history (so the undo is part of
# the audit trail, not a silent reset) before returning the document to untriaged.
_RESTORE_SCRIPT = (
    "if (ctx._source.triage.history == null) {\n"
    "  ctx._source.triage.history = new ArrayList();\n"
    "}\n"
    "ctx._source.triage.history.add([\n"
    "  'layer': ctx._source.triage.layer,\n"
    "  'query': ctx._source.triage.query,\n"
    "  'explanation': ctx._source.triage.explanation,\n"
    "  'at': ctx._source.triage.at,\n"
    "  'restored_at': params.at\n"
    "]);\n"
    "ctx._source.triage.layer = params.untriaged;\n"
    "ctx._source.triage.query = null;\n"
    "ctx._source.triage.explanation = null;\n"
    "ctx._source.triage.at = null;"
)


class TriageError(RuntimeError):
    """An OpenSearch call failed, or a predicate could not be tagged safely."""


def _data(res: OpensearchResult[Any]) -> Any:
    """Return a result's data, or raise TriageError on failure.

    Triage runs many calls in sequence; unwrapping here keeps each step readable,
    and :func:`run` catches the TriageError at the boundary and turns it back into
    a result.
    """
    if not res:
        raise TriageError(res.reason)
    return res.data


def _dig(data: Any, *path: Any) -> Any:
    """Return the value at ``path`` inside a response, or raise TriageError.

    Args:
        data: the response, as nested dicts and lists.
        *path: the keys and indexes to follow, in order.

    Returns:
        The value found at the end of the path.

    Raises:
        TriageError: if the response does not have that shape.
    """
    try:
        for step in path:
            data = data[step]
    except (KeyError, IndexError, TypeError) as e:
        raise TriageError(
            f"unexpected response from OpenSearch: no {list(path)}"
        ) from e
    return data


def _find_pushed_down_request(node: Any) -> str | None:
    """Find the OpenSearchQueryRequest string in a SQL _explain plan, if present.

    The v2 SQL engine embeds the pushed-down DSL as JSON inside a stringified
    ``OpenSearchQueryRequest(... sourceBuilder={...} ...)`` under a scan node's
    ``description.request``. Walk the plan tree to find that string.
    """
    if isinstance(node, dict):
        description = node.get("description")
        if isinstance(description, dict):
            request = description.get("request")
            if isinstance(request, str) and "sourceBuilder=" in request:
                return request
        for value in node.values():
            found = _find_pushed_down_request(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_pushed_down_request(item)
            if found is not None:
                return found
    return None


def _extract_json_object(text: str, start: int) -> str:
    """Return the balanced ``{...}`` substring of text beginning at index start.

    Brace-matches while respecting JSON string literals, so braces appearing inside
    string values do not throw off the depth count. ``start`` must be the index of
    the opening brace.
    """
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        char = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise ValueError("unbalanced braces in pushed-down request")


def where_to_dsl(client: OpensearchClient, index: str, where: str) -> dict[str, Any]:
    """Translate a SQL WHERE predicate into the query DSL it pushes down to.

    Runs ``SELECT * FROM <index> WHERE <where>`` through the SQL _explain endpoint and
    extracts ``query`` from the pushed-down request. Raises if the predicate yields no
    pushed-down query (e.g. it uses SQL features computed in-engine, not pushed down).
    See docs/sql-explain.md for an example of the _explain output parsed here.

    Args:
        client: the client used to reach the cluster.
        index: the index the predicate is explained against.
        where: the SQL WHERE predicate.

    Returns:
        The pushed-down query DSL.

    Raises:
        ValueError: if the explain output has no pushed-down query.
    """
    sql = f"SELECT * FROM {index} WHERE {where}"
    explain = _data(client.explain(sql))
    request = _find_pushed_down_request(explain)
    if request is None:
        raise ValueError(
            "SQL _explain returned no pushed-down request; the WHERE clause may use "
            "features that do not translate to a filter. Explain output: "
            + json.dumps(explain)
        )
    brace = request.index("{", request.index("sourceBuilder="))
    source_builder = json.loads(_extract_json_object(request, brace))
    query = source_builder.get("query")
    if query is None:
        raise ValueError("pushed-down request had no query DSL")
    return query


def _combined_query(user_query: dict[str, Any]) -> dict[str, Any]:
    """Combine a query with still-untriaged: the exact set eliminate will tag.

    Args:
        user_query: the query selecting the documents to eliminate.

    Returns:
        A bool filter of ``user_query`` and the untriaged sentinel.
    """
    return {"bool": {"filter": [user_query, {"term": {"triage.layer": UNTRIAGED}}]}}


def sql_count(client: OpensearchClient, index: str, where: str) -> int:
    """Exact count of documents the WHERE predicate matches, via SQL COUNT(*).

    COUNT(*) returns a single row, so unlike collecting rows it is not subject to the
    SQL result-size limit. Used to cross-check the DSL translation. Reads the raw
    jdbc response so the single count value is taken by position, not column name.

    Args:
        client: the client used to reach the cluster.
        index: the index to count in.
        where: the SQL WHERE predicate.

    Returns:
        The number of matching documents.
    """
    sql = f"SELECT COUNT(*) FROM {index} WHERE {where}"
    resp = _data(client.sql_raw(sql))
    return _dig(resp, "datarows", 0, 0)


def dsl_count(client: OpensearchClient, index: str, query: dict[str, Any]) -> int:
    """Count documents matching a DSL query via the _count API.

    Args:
        client: the client used to reach the cluster.
        index: the index to count in.
        query: the query DSL.

    Returns:
        The number of matching documents.
    """
    return _data(client.count(query, index=index))


def sample_untriaged(
    client: OpensearchClient, index: str, user_query: dict[str, Any], size: int = 1
) -> list:
    """Return up to size _source docs from the untriaged matches, for a preview.

    Args:
        client: the client used to reach the cluster.
        index: the index to search.
        user_query: the query selecting the documents to eliminate.
        size: the most documents to return.

    Returns:
        The ``_source`` of each sampled document.
    """
    return _data(
        client.search({"size": size, "query": _combined_query(user_query)}, index=index)
    )


def _iso8601(at: datetime) -> str:
    """Render a datetime as the ISO-8601 UTC string OpenSearch stores on triage.at."""
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _tag_script(
    layer: int, where: str, explanation: str, at: datetime
) -> dict[str, Any]:
    """Build the Painless script that stamps the triage fields for this layer.

    Args:
        layer: the elimination pass number.
        where: the SQL WHERE predicate, recorded on each document.
        explanation: the rationale, recorded on each document.
        at: when the elimination happened.

    Returns:
        The script, with its parameters.
    """
    return {
        "lang": "painless",
        "source": _TAG_SCRIPT,
        "params": {
            "layer": layer,
            "query": where,
            "explanation": explanation,
            "at": _iso8601(at),
        },
    }


def eliminate(
    client: OpensearchClient,
    index: str,
    where: str,
    layer: int | None,
    explanation: str,
    apply: bool = False,
) -> dict[str, Any]:
    """Tag the still-untriaged documents matching a SQL WHERE predicate with a layer.

    The predicate is translated to the query DSL it pushes down to (via SQL
    ``_explain``) and cross-checked: the DSL must match exactly as many documents
    as ``SELECT COUNT(*)`` for the same predicate, or this refuses rather than risk
    tagging the wrong set. With ``apply`` the matches are tagged by one server-side
    ``_update_by_query``, polled to completion so a large pass does not time out.

    Args:
        client: the client used to reach the cluster.
        index: the triage index to tag.
        where: the SQL WHERE predicate selecting documents to eliminate.
        layer: the elimination pass number (>= 1), or None to auto-increment to
            one past the highest layer already used (1 on a fresh index).
        explanation: a short, human rationale stored on each tagged document.
        apply: if False, only dry-run counts, the translated DSL, and a sample; if
            True, the tags are written (stamped with the current time).

    Returns:
        A summary mapping: the layer, predicate, and match counts, plus either the
        dry-run sample or the applied-update stats.

    Raises:
        ValueError: if ``layer`` is < 1, or the predicate does not translate
            exactly to a pushed-down filter.
    """
    if layer is None:
        layer = next_layer(client, index)
    elif layer < 1:
        raise ValueError(f"layer must be >= 1 (an elimination pass), got {layer}")

    user_query = where_to_dsl(client, index, where)
    sql_matched = sql_count(client, index, where)
    dsl_matched = dsl_count(client, index, user_query)
    if sql_matched != dsl_matched:
        raise ValueError(
            f"predicate did not translate exactly: SQL COUNT(*)={sql_matched} but the "
            f"translated DSL matches {dsl_matched}. The WHERE clause uses SQL features "
            "that are not fully pushed down; simplify it to a plain boolean predicate."
        )

    to_tag = dsl_count(client, index, _combined_query(user_query))
    summary: dict[str, Any] = {
        "index": index,
        "layer": layer,
        "where": where,
        "explanation": explanation,
        "matched": sql_matched,
        "untriaged_to_tag": to_tag,
        "already_triaged_skipped": sql_matched - to_tag,
    }

    if not apply:
        summary["dry_run"] = True
        summary["query_dsl"] = user_query
        summary["sample"] = sample_untriaged(client, index, user_query)
        return summary

    started = _data(
        client.update_by_query(
            _combined_query(user_query),
            index=index,
            script=_tag_script(layer, where, explanation, datetime.now(timezone.utc)),
            conflicts="proceed",
            wait_for_completion=False,
            refresh=True,
            timeout=_TAG_TIMEOUT,
        )
    )
    task = _await_task(client, _dig(started, "task"))
    response = task.get("response", {})
    failures = response.get("failures", [])
    summary["applied"] = True
    summary["updated"] = response.get("updated")
    summary["version_conflicts"] = response.get("version_conflicts")
    summary["failures"] = len(failures)
    if failures:
        summary["failure_sample"] = failures[:3]
    return summary


def _restore_script(at: datetime) -> dict[str, Any]:
    """Build the Painless script that records the eliminated state, then resets.

    Args:
        at: when the restore happened.

    Returns:
        The script, with its parameters.
    """
    return {
        "lang": "painless",
        "source": _RESTORE_SCRIPT,
        "params": {"untriaged": UNTRIAGED, "at": _iso8601(at)},
    }


def restore(
    client: OpensearchClient,
    index: str,
    layer: int | None,
    from_layer: int | None,
    apply: bool = False,
) -> dict[str, Any]:
    """Undo an elimination: reset the matching documents to untriaged.

    Before returning each document to ``triage.layer = -1`` and clearing its
    active predicate/explanation, the eliminated state is appended to
    ``triage.history``, so the undo is part of the audit trail rather than a silent
    reset. Mirrors :func:`eliminate` in shape.

    Args:
        client: the client used to reach the cluster.
        index: the triage index to restore in.
        layer: reset exactly this elimination pass (mutually exclusive with
            ``from_layer``).
        from_layer: reset this pass and every higher one.
        apply: if False, only the dry-run match count, query, and a sample; if
            True, the restore is written (each undo stamped with the current time).

    Returns:
        A summary mapping: the selector and match count, plus either the dry-run
        sample or the applied-update stats.

    Raises:
        ValueError: if the selected layer is < 1, or neither selector is given.
    """
    if layer is not None:
        if layer < 1:
            raise ValueError(f"layer must be >= 1, got {layer}")
        query: dict[str, Any] = {"term": {"triage.layer": layer}}
        summary: dict[str, Any] = {"index": index, "layer": layer}
    elif from_layer is not None:
        if from_layer < 1:
            raise ValueError(f"from-layer must be >= 1, got {from_layer}")
        query = {"range": {"triage.layer": {"gte": from_layer}}}
        summary = {"index": index, "from_layer": from_layer}
    else:
        raise ValueError("restore needs a layer or a from-layer")

    summary["matched"] = dsl_count(client, index, query)

    if not apply:
        summary["dry_run"] = True
        summary["query_dsl"] = query
        summary["sample"] = _data(
            client.search({"size": 1, "query": query}, index=index)
        )
        return summary

    started = _data(
        client.update_by_query(
            query,
            index=index,
            script=_restore_script(datetime.now(timezone.utc)),
            conflicts="proceed",
            wait_for_completion=False,
            refresh=True,
            timeout=_TAG_TIMEOUT,
        )
    )
    task = _await_task(client, _dig(started, "task"))
    response = task.get("response", {})
    failures = response.get("failures", [])
    summary["applied"] = True
    summary["restored"] = response.get("updated")
    summary["version_conflicts"] = response.get("version_conflicts")
    summary["failures"] = len(failures)
    if failures:
        summary["failure_sample"] = failures[:3]
    return summary


def _deep_merge(into: dict[str, Any], other: dict[str, Any]) -> None:
    """Recursively merge other into into (used to union index mapping properties).

    Nested dicts are merged; on a leaf conflict the later value wins. Source
    indices sharing a template have identical types, so conflicts are not expected
    in practice.

    Args:
        into: the dict to update.
        other: the dict to merge in.
    """
    for key, value in other.items():
        if key in into and isinstance(into[key], dict) and isinstance(value, dict):
            _deep_merge(into[key], value)
        else:
            into[key] = value


def dest_properties(client: OpensearchClient, source: str) -> dict[str, Any]:
    """Build the destination mapping properties: the source's, plus triage.

    ``source`` may match several indices (e.g. daily indices); their properties
    are merged. Dynamic templates and other mapping settings are intentionally not
    copied: only explicit field types are carried over, which is what SQL and the
    dashboard need for the copied logs.

    Args:
        client: the client used to reach the cluster.
        source: the source index or pattern.

    Returns:
        The merged source properties, plus the triage fields.
    """
    resp = _data(client.get_mapping(source))
    merged: dict[str, Any] = {}
    for index_body in resp.values():
        properties = index_body.get("mappings", {}).get("properties", {})
        _deep_merge(merged, properties)
    merged["triage"] = _TRIAGE_MAPPING
    return merged


# The reindex script run by init: tag every copied document untriaged.
_REINDEX_SCRIPT = {
    "lang": "painless",
    "source": "ctx._source.triage = ['layer': params.untriaged];",
    "params": {"untriaged": UNTRIAGED},
}


def _await_task(
    client: OpensearchClient, task_id: str, poll_seconds: float = 2
) -> dict[str, Any]:
    """Poll _tasks/<task_id> until the task completes and return its final document.

    Args:
        client: the client used to reach the cluster.
        task_id: the task to poll.
        poll_seconds: how long to wait between polls.

    Returns:
        The completed task document.
    """
    while True:
        resp = _data(client.get_task(task_id))
        if resp.get("completed"):
            return resp
        progress = resp.get("task", {}).get("status", {})
        # reindex reports "created", update_by_query reports "updated"; show whichever.
        done = progress.get("updated") or progress.get("created", "?")
        logging.info("task progress: %s/%s", done, progress.get("total", "?"))
        time.sleep(poll_seconds)


def init(
    client: OpensearchClient, source: str, dest: str, poll_seconds: float = 2
) -> dict[str, Any]:
    """Create dest with a triage mapping and reindex source into it, all untriaged.

    The reindex runs asynchronously (so a large copy does not hit a request
    timeout) and this polls the task to completion before returning a summary. dest
    must not already exist; creating it will fail loudly if it does.

    Args:
        client: the client used to reach the cluster.
        source: the index or pattern to copy.
        dest: the new index to copy into.
        poll_seconds: how long to wait between polls of the reindex task.

    Returns:
        A summary mapping: source, destination, document total, tagged count and
        failures.
    """
    properties = dest_properties(client, source)
    _data(client.create_index({"mappings": {"properties": properties}}, index=dest))

    started = _data(
        client.reindex(
            source,
            dest,
            script=_REINDEX_SCRIPT,
            wait_for_completion=False,
            refresh=True,
        )
    )
    task = _await_task(client, _dig(started, "task"), poll_seconds)

    response = task.get("response", {})
    failures = response.get("failures", [])
    summary: dict[str, Any] = {
        "source": source,
        "destination": dest,
        "total": response.get("total"),
        "tagged_untriaged": response.get("created"),
        "failures": len(failures),
    }
    if failures:
        summary["failure_sample"] = failures[:3]
    return summary


def status(client: OpensearchClient, index: str) -> dict[str, Any]:
    """Summarize triage progress for the index: totals and per-layer counts.

    Returns a mapping with the total document count, the number still untriaged
    (``triage.layer == -1``), the number eliminated (any layer >= 1) broken down
    per layer, and any documents missing the field entirely (which should be zero
    once the index has been through init).

    Args:
        client: the client used to reach the cluster.
        index: the triage index.

    Returns:
        The totals and per-layer counts.
    """
    body = {
        "size": 0,
        "track_total_hits": True,
        "aggs": {
            "by_layer": {"terms": {"field": "triage.layer", "size": 1000}},
            "untagged": {"missing": {"field": "triage.layer"}},
        },
    }
    resp = _data(client.search_raw(body, index=index))

    total = _dig(resp, "hits", "total", "value")
    untagged = _dig(resp, "aggregations", "untagged", "doc_count")
    layers = {
        int(_dig(bucket, "key")): _dig(bucket, "doc_count")
        for bucket in _dig(resp, "aggregations", "by_layer", "buckets")
    }
    remaining = layers.get(UNTRIAGED, 0)
    eliminated = {layer: count for layer, count in layers.items() if layer >= 1}
    return {
        "index": index,
        "total": total,
        "untriaged": remaining,
        "eliminated_total": sum(eliminated.values()),
        "eliminated_by_layer": dict(sorted(eliminated.items())),
        "untagged_missing_field": untagged,
    }


def next_layer(client: OpensearchClient, index: str) -> int:
    """Get the next elimination layer: one past the highest already used, else 1.

    Args:
        client: the client used to reach the cluster.
        index: the triage index.

    Returns:
        The next layer number.
    """
    used = status(client, index)["eliminated_by_layer"]
    return max(used) + 1 if used else 1


def run(args: Namespace, client: OpensearchClient) -> OpensearchResult[dict[str, Any]]:
    """Dispatch to the chosen subcommand and return its result mapping.

    The failures this module raises on purpose (a failed OpenSearch call or an
    unexpected response, both TriageError, and a predicate or argument that cannot
    be used safely, ValueError) are caught here and returned as ``ok=False`` with a
    reason. Any other exception is a bug and propagates.

    Args:
        args: the parsed command-line arguments.
        client: the client used to reach the cluster.

    Returns:
        The subcommand's summary mapping, or a failure.
    """
    try:
        data = _dispatch(args, client)
    # Boundary: translate this module's own errors to a result value.
    except (TriageError, ValueError) as e:
        return Failure(str(e))
    return Success(data)


def _dispatch(args: Namespace, client: OpensearchClient) -> dict[str, Any]:
    """Run the chosen subcommand, returning its summary (may raise).

    Args:
        args: the parsed command-line arguments.
        client: the client used to reach the cluster.

    Returns:
        The subcommand's summary mapping.

    Raises:
        ValueError: if the command is unknown.
    """
    if args.command == "init":
        return init(client, args.source, args.dest)
    if args.command == "status":
        return status(client, args.index)
    if args.command == "eliminate":
        return eliminate(
            client, args.index, args.where, args.layer, args.explanation, args.apply
        )
    if args.command == "restore":
        return restore(client, args.index, args.layer, args.from_layer, args.apply)
    raise ValueError(f"unknown command: {args.command!r}")
