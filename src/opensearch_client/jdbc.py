# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Flatten OpenSearch SQL/PPL (jdbc-format) responses into row dicts."""

from typing import Any


def rows_from_sql_response(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn an OpenSearch SQL/PPL (jdbc) response into a list of row dicts.

    Each row pairs the response's column names (preferring an alias when present)
    with its values. An empty or malformed response yields an empty list.
    """
    schema = payload.get("schema")
    datarows = payload.get("datarows")
    if not isinstance(schema, list) or not isinstance(datarows, list):
        return []
    columns = [col.get("alias") or col.get("name") for col in schema]
    return [dict(zip(columns, row, strict=False)) for row in datarows]
