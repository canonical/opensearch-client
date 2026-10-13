# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for opensearch_client.jdbc.rows_from_sql_response."""

from opensearch_client.jdbc import rows_from_sql_response


def test_rows_pair_columns_with_values_preferring_alias() -> None:
    payload = {
        "schema": [{"name": "a"}, {"name": "rule.level", "alias": "level"}],
        "datarows": [[1, 7], [2, 8]],
    }
    assert rows_from_sql_response(payload) == [
        {"a": 1, "level": 7},
        {"a": 2, "level": 8},
    ]


def test_empty_or_malformed_response_yields_no_rows() -> None:
    assert rows_from_sql_response({}) == []
    assert rows_from_sql_response({"schema": "nope", "datarows": []}) == []
    assert rows_from_sql_response({"schema": [{"name": "a"}]}) == []
