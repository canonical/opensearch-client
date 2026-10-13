# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Identify the reason for a Failure."""

import os
from typing import Any

from opensearch_client.result import Failure, OpensearchResult


def _diagnosis(result: OpensearchResult[Any]) -> str | None:
    """Return a clearer reason for a recognized failure, or None to pass through.

    Recognizes a few common but cryptic failures from the HTTP status and the error
    body (auth, a missing SQL/PPL plugin, a timeout, a missing index).

    Args:
        result: the failed result to inspect.

    Returns:
        A clearer reason string, or None when the failure is unrecognized.
    """
    reason = result.reason
    lowered = reason.lower()
    body = result.data
    error = body.get("error") if isinstance(body, dict) else None
    if result.status == 401:
        user = os.environ.get("OPENSEARCH_USER")
        whose = f" for user {user!r}" if user else ""
        return (
            f"authentication failed{whose}. Credentials come from OPENSEARCH_USER "
            "and OPENSEARCH_PASSWORD."
        )
    if isinstance(error, dict) and error.get("type") == "index_not_found_exception":
        missing = error.get("index") or error.get("resource.id") or "?"
        return f"index {missing!r} not found. Try 'os-cli index list'."
    if "no handler found" in lowered and (
        "_plugins/_sql" in reason or "_plugins/_ppl" in reason
    ):
        return (
            "the SQL/PPL plugin does not appear to be installed; check with "
            "`os-cli cluster versions`."
        )
    if result.status is None and "timed out" in lowered:
        return (
            f"the request timed out ({reason}). Narrow the time range with --since, "
            "or add a LIMIT to the query."
        )
    return None


def diagnose(result: OpensearchResult[Any]) -> OpensearchResult[Any]:
    """Rewrite a recognized failure's reason into actionable guidance.

    A success passes through unchanged. A recognized failure (auth, a missing
    SQL/PPL plugin, a timeout, a missing index) is returned as a new failure with a
    clearer reason. An unrecognized failure passes through unchanged.

    Args:
        result: the result to diagnose.

    Returns:
        The result, with a recognized failure's reason rewritten.
    """
    if result.ok:
        return result
    better = _diagnosis(result)
    if better is None:
        return result
    return Failure(better, data=result.data, status=result.status)
