# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""The high-level OpenSearch client (transport-agnostic)."""

import json
from collections.abc import Iterable
from typing import Any, NamedTuple

from opensearch_client.jdbc import rows_from_sql_response
from opensearch_client.result import Failure, OpensearchResult, Success
from opensearch_client.transport import REQUEST_TIMEOUT, Transport

DEFAULT_INDEX = "*"

# Default byte ceiling for one _bulk request body; batches are packed under it.
BULK_MAX_BYTES = 10_000_000


class BulkItem(NamedTuple):
    """A document's encoded NDJSON lines, plus the source kept for failure reports.

    ``payload`` is stored (not recomputed) so each document is serialized exactly
    once; ``document`` is kept because a failure reports the original source, which
    cannot be recovered from the encoded bytes.
    """

    payload: bytes
    document: dict[str, Any]

    @classmethod
    def build(cls, action: str, index: str, document: dict[str, Any]) -> "BulkItem":
        """Encode one document's action + source NDJSON lines, once."""
        meta = json.dumps({action: {"_index": index}})
        return cls(f"{meta}\n{json.dumps(document)}\n".encode(), document)


BulkBatch = list[BulkItem]


def _pack_batches(items: list[BulkItem], max_bytes: int) -> list[BulkBatch]:
    """Group items into batches whose combined payloads fit max_bytes.

    Each item's ``payload`` is already-encoded bytes, so its length is its wire
    size and no re-serialization is needed to measure it. A single item larger
    than max_bytes still goes in a batch of its own: it cannot be split further,
    and will be reported as a failure if rejected.
    """
    batches: list[BulkBatch] = []
    current: list[BulkItem] = []
    size = 0
    for item in items:
        if current and size + len(item.payload) > max_bytes:
            batches.append(current)
            current, size = [], 0
        current.append(item)
        size += len(item.payload)
    if current:
        batches.append(current)
    return batches


BulkFailure = tuple[BulkItem, dict[str, Any]]


def _tally_bulk_items(
    response: dict[str, Any],
    batch: BulkBatch,
    action: str,
    summary: dict[str, Any],
    failures: list[BulkFailure],
) -> None:
    """Fold one 200 bulk response's per-item results into the run.

    The bulk API returns 200 even when individual items fail, so each item's own
    status/error is what decides success, not the request status. A success counts
    into ``summary['indexed']``; a failed item is appended to ``failures`` (with
    its error) so the caller can retry it before recording it as failed.
    """
    results = response.get("items", [])
    for item, result in zip(batch, results, strict=False):
        outcome = result.get(action, {}) if isinstance(result, dict) else {}
        error = outcome.get("error")
        if error is None and outcome.get("status", 0) < 300:
            summary["indexed"] += 1
        else:
            failures.append((item, {"status": outcome.get("status"), "error": error}))


def _query_string(params: dict[str, Any]) -> str:
    """Render params as a URL query string (bools lowercased)."""
    parts = []
    for key, value in params.items():
        if isinstance(value, bool):
            value = "true" if value else "false"
        parts.append(f"{key}={value}")
    return "?" + "&".join(parts) if parts else ""


class OpensearchClient:
    """Query and administer an OpenSearch cluster over a transport.

    Most operations funnel through :meth:`request`, which JSON-encodes the body
    and hands the bytes to the transport; :meth:`bulk` encodes newline-delimited
    JSON and sends it the same way. Each helper returns an
    :class:`~opensearch_client.result.OpensearchResult`.

    Index-scoped helpers (``search``, ``count``, ``create_index``, ...) target
    :attr:`default_index` unless an explicit ``index`` argument is given.
    """

    def __init__(
        self, transport: Transport, default_index: str = DEFAULT_INDEX
    ) -> None:
        """Initialize the client.

        Args:
            transport (Transport): how requests reach the cluster (direct, proxy,
                probe, or failover).
            default_index (str): the index pattern index-scoped helpers target when
                no explicit ``index`` is passed, e.g. ``logs-*``.
        """
        self._transport = transport
        self.default_index = default_index

    # -- core -------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: int = REQUEST_TIMEOUT,
    ) -> OpensearchResult[Any]:
        """Send a JSON request to the cluster (the primitive most helpers build on).

        Args:
            method (str): the HTTP method, e.g. ``GET``, ``POST`` or ``PUT``.
            path (str): the OpenSearch path (no leading slash), query string
                allowed, e.g. ``my-index/_search``.
            body (dict[str, Any] | None): optional request body, encoded as JSON.
            timeout (int): request timeout in seconds; override for operations that
                can run longer than a search.
        """
        encoded = None if body is None else json.dumps(body).encode()
        return self._transport.request(method, path, encoded, timeout=timeout)

    def get(self, path: str) -> OpensearchResult[Any]:
        """GET an arbitrary OpenSearch path, e.g. ``_cat/plugins?format=json``."""
        return self.request("GET", path)

    # -- reads ------------------------------------------------------------

    def search_raw(
        self, query: dict[str, Any], index: str | None = None
    ) -> OpensearchResult[dict[str, Any]]:
        """Run a search and return the full response (hits, aggregations, ...)."""
        return self.request("POST", f"{index or self.default_index}/_search", query)

    def search(
        self, query: dict[str, Any], index: str | None = None
    ) -> OpensearchResult:
        """Run a search against the index, returning each hit's ``_source``."""
        res = self.search_raw(query, index)
        if not res:
            return res
        hits = res.data.get("hits", {}).get("hits", [])
        return Success([hit.get("_source", {}) for hit in hits])

    def count(
        self, query: dict[str, Any], index: str | None = None
    ) -> OpensearchResult[int]:
        """Count documents matching a DSL query via the ``_count`` API."""
        res = self.request(
            "POST", f"{index or self.default_index}/_count", {"query": query}
        )
        if not res:
            return res
        return Success(res.data.get("count", 0))

    def sql_raw(
        self, sql_query: str, filter_dsl: dict[str, Any] | None = None
    ) -> OpensearchResult[dict[str, Any]]:
        """Run a SQL query and return the raw jdbc response (schema + datarows).

        ``filter_dsl`` is an optional query DSL the SQL engine ANDs with the query.
        """
        body: dict[str, Any] = {"query": sql_query}
        if filter_dsl is not None:
            body["filter"] = filter_dsl
        return self.request("POST", "_plugins/_sql", body)

    def sql(
        self, sql_query: str, filter_dsl: dict[str, Any] | None = None
    ) -> OpensearchResult:
        """Run a SQL query against the index, returning the rows as dicts."""
        res = self.sql_raw(sql_query, filter_dsl)
        if not res:
            return res
        return Success(rows_from_sql_response(res.data))

    def ppl(self, ppl_query: str) -> OpensearchResult[list[dict[str, Any]]]:
        """Run a PPL query against the index, returning the rows as dicts."""
        res = self.request("POST", "_plugins/_ppl", {"query": ppl_query})
        if not res:
            return res
        return Success(rows_from_sql_response(res.data))

    def explain(
        self, query: str, query_type: str = "sql"
    ) -> OpensearchResult[dict[str, Any]]:
        """Return the SQL execution plan (the pushed-down query DSL), unexecuted.

        query_type must be one of: sql, ppl.
        """
        return self.request(
            "POST", f"_plugins/_{query_type}/_explain", {"query": query}
        )

    def get_mapping(self, index: str | None = None) -> OpensearchResult[dict[str, Any]]:
        """Return the full mapping for the index (or pattern)."""
        return self.request("GET", f"{index or self.default_index}/_mapping")

    def field_mapping(
        self, field: str, index: str | None = None
    ) -> OpensearchResult[Any]:
        """Return the mapping for one or more fields (wildcards allowed) on the index.

        An empty ``mappings`` for an index means the field is not mapped there, and
        so cannot be resolved by SQL or a term filter even when it appears in a
        document's ``_source``.
        """
        return self.request(
            "GET", f"{index or self.default_index}/_mapping/field/{field}"
        )

    def opensearch_version(self) -> OpensearchResult[list[str]]:
        """Return the distinct OpenSearch versions running across the cluster's nodes."""
        res = self.get("_cat/nodes?h=version&format=json")
        if not res:
            return res
        return Success(sorted({node.get("version") for node in res.data}))

    def plugin_versions(self) -> OpensearchResult[dict[str, str]]:
        """Return the installed plugins mapped to their versions."""
        res = self.get("_cat/plugins?h=component,version&format=json")
        if not res:
            return res
        return Success({p.get("component"): p.get("version") for p in res.data})

    # -- writes / admin ---------------------------------------------------

    def _send_bulk(
        self,
        items: list[BulkItem],
        max_bytes: int,
        action: str,
        summary: dict[str, Any],
    ) -> list[BulkFailure]:
        """POST one batch of NDJSON to ``_bulk``.

        Successes count into ``summary`` as they land; the items that failed this
        pass (a whole-batch failure, or a per-item error in a 200) are returned so
        the caller can retry them. A 413 is not a failure: the batch is halved and
        the halves are sent in this same pass.
        """
        failures: list[BulkFailure] = []
        queue = _pack_batches(items, max_bytes)
        while queue:
            batch = queue.pop(0)
            payload = b"".join(item.payload for item in batch)
            result = self._transport.request(
                "POST", "_bulk", payload, content_type="application/x-ndjson"
            )
            if result:
                summary["batches"] += 1
                _tally_bulk_items(result.data, batch, action, summary, failures)
            elif result.status == 413 and len(batch) > 1:
                middle = len(batch) // 2
                queue.insert(0, batch[middle:])
                queue.insert(0, batch[:middle])
            else:
                summary["batches"] += 1
                info = {"status": result.status, "reason": result.reason}
                failures.extend((item, info) for item in batch)
        return failures

    def bulk(
        self,
        documents: Iterable[dict[str, Any]],
        index: str | None = None,
        *,
        action: str = "index",
        max_bytes: int = BULK_MAX_BYTES,
        max_retries: int = 3,
    ) -> OpensearchResult[dict[str, Any]]:
        """Index many documents via the ``_bulk`` API, in byte-bounded batches.

        Documents are serialized to newline-delimited JSON and sent in batches no
        larger than ``max_bytes``. A batch rejected with 413 (too large) is halved.
        Every 200's per-item results are inspected, so a document that failed
        inside an otherwise-2xx bulk response is not trusted as written. Any failed
        document, whether from a failed batch or a per-item error, is retried up to
        ``max_retries`` times (``max_retries=0`` disables retries).

        Returns a summary: ``indexed`` and ``failed`` document counts, the number
        of ``batches`` sent, and a ``failures`` list (each with the offending
        ``document`` and its error). The result is a ``Success`` only if every
        document was indexed; if any failed, it is a ``Failure`` whose ``data``
        still carries the same summary for inspection.
        """
        idx = index or self.default_index
        pending = [BulkItem.build(action, idx, doc) for doc in documents]
        total = len(pending)
        summary: dict[str, Any] = {
            "indexed": 0,
            "failed": 0,
            "batches": 0,
            "failures": [],
        }

        failures = self._send_bulk(pending, max_bytes, action, summary)
        for _ in range(max_retries):
            if not failures:
                break
            failures = self._send_bulk(
                [item for item, _ in failures], max_bytes, action, summary
            )

        for item, info in failures:
            summary["failed"] += 1
            summary["failures"].append({**info, "document": item.document})

        if summary["failed"]:
            reason = f"{summary['failed']} of {total} documents failed to index"
            return Failure(reason, data=summary)
        return Success(summary)

    def index_document(
        self,
        document: dict[str, Any],
        index: str | None = None,
        *,
        doc_id: str | None = None,
        refresh: bool = False,
    ) -> OpensearchResult[dict[str, Any]]:
        """Index a single document.

        Without ``doc_id`` OpenSearch assigns one (``POST <index>/_doc``); with it
        the document is created or replaced at that id (``PUT <index>/_doc/<id>``).
        ``refresh=True`` makes the document immediately searchable.
        """
        idx = index or self.default_index
        if doc_id is not None:
            method, path = "PUT", f"{idx}/_doc/{doc_id}"
        else:
            method, path = "POST", f"{idx}/_doc"
        if refresh:
            path += "?refresh=true"
        return self.request(method, path, document)

    def create_index(
        self, body: dict[str, Any], index: str | None = None
    ) -> OpensearchResult[dict[str, Any]]:
        """Create an index with the given settings/mappings body (``PUT <index>``)."""
        return self.request("PUT", index or self.default_index, body)

    def put_mapping(
        self, mapping: dict[str, Any], index: str | None = None
    ) -> OpensearchResult[dict[str, Any]]:
        """Add or update field mappings on an existing index (``PUT <index>/_mapping``).

        OpenSearch can add new fields but cannot change an existing field's type; a
        conflicting change comes back as a failure.

        Args:
            mapping: the mappings body, e.g.
                ``{"properties": {"source.ip": {"type": "ip"}}}``.
            index: the index (or pattern) to update; defaults to the configured index.

        Returns:
            The put-mapping acknowledgement.
        """
        return self.request("PUT", f"{index or self.default_index}/_mapping", mapping)

    def get_pipeline(self, name: str | None = None) -> OpensearchResult[dict[str, Any]]:
        """Get ingest pipelines: all of them, or one by name.

        Args:
            name: the pipeline to fetch; None fetches every pipeline
                (``GET _ingest/pipeline`` vs ``GET _ingest/pipeline/<name>``).

        Returns:
            The pipeline definitions keyed by name.
        """
        path = "_ingest/pipeline" if name is None else f"_ingest/pipeline/{name}"
        return self.request("GET", path)

    def put_pipeline(
        self, name: str, body: dict[str, Any]
    ) -> OpensearchResult[dict[str, Any]]:
        """Create or replace a named ingest pipeline (``PUT _ingest/pipeline/<name>``).

        Args:
            name: the pipeline name.
            body: the pipeline definition, e.g.
                ``{"description": "...", "processors": [...]}``.

        Returns:
            The put-pipeline acknowledgement.
        """
        return self.request("PUT", f"_ingest/pipeline/{name}", body)

    def get_index_template(
        self, name: str | None = None
    ) -> OpensearchResult[dict[str, Any]]:
        """Get all composable index templates, or those matching ``name``."""
        path = "_index_template" if name is None else f"_index_template/{name}"
        return self.request("GET", path)

    def get_legacy_template(
        self, name: str | None = None
    ) -> OpensearchResult[dict[str, Any]]:
        """Get all legacy (``_template``) templates, or those matching ``name``."""
        path = "_template" if name is None else f"_template/{name}"
        return self.request("GET", path)

    def put_index_template(
        self, name: str, body: dict[str, Any]
    ) -> OpensearchResult[dict[str, Any]]:
        """Create or replace a composable index template."""
        return self.request("PUT", f"_index_template/{name}", body)

    def delete_index_template(self, name: str) -> OpensearchResult[dict[str, Any]]:
        """Delete a composable index template."""
        return self.request("DELETE", f"_index_template/{name}")

    def simulate_template(
        self, name: str, body: dict[str, Any]
    ) -> OpensearchResult[dict[str, Any]]:
        """Resolve the index configuration of putting ``body`` as template ``name``."""
        return self.request("POST", f"_index_template/_simulate/{name}", body)

    def simulate_index(
        self, index: str, *, timeout: int = 120
    ) -> OpensearchResult[dict[str, Any]]:
        """Resolve the index configuration the installed templates give ``index``."""
        # Simulate consistently exceeds the default REQUEST_TIMEOUT of 30 s.
        return self.request(
            "POST", f"_index_template/_simulate_index/{index}", timeout=timeout
        )

    def get_component_template(
        self, name: str | None = None
    ) -> OpensearchResult[dict[str, Any]]:
        """Get all component templates, or those matching ``name``."""
        path = "_component_template" if name is None else f"_component_template/{name}"
        return self.request("GET", path)

    def put_component_template(
        self, name: str, body: dict[str, Any]
    ) -> OpensearchResult[dict[str, Any]]:
        """Create or replace a component template."""
        return self.request("PUT", f"_component_template/{name}", body)

    def delete_component_template(self, name: str) -> OpensearchResult[dict[str, Any]]:
        """Delete a component template."""
        return self.request("DELETE", f"_component_template/{name}")

    def refresh(self, index: str | None = None) -> OpensearchResult[dict[str, Any]]:
        """Refresh an index so its recent writes become searchable.

        Args:
            index: the index or pattern to refresh; defaults to the client's
                configured index.

        Returns:
            The ``_refresh`` response (per-shard success counts).
        """
        return self.request("POST", f"{index or self.default_index}/_refresh")

    def delete_index(self, index: str) -> OpensearchResult[dict[str, Any]]:
        """Delete an index.

        ``index`` is required (never defaulted), so a bare call cannot delete the
        client's configured index by accident.

        Args:
            index: the index to delete.

        Returns:
            The delete acknowledgement.
        """
        return self.request("DELETE", index)

    def index_exists(self, index: str) -> OpensearchResult[bool]:
        """Report whether an index exists.

        Args:
            index: the index to test.

        Returns:
            True if the index exists, False if it does not (a 404). A non-404
            failure (e.g. an auth error) is propagated unchanged.
        """
        res = self.request("GET", index)
        if res:
            return Success(True)
        if res.status == 404:
            return Success(False)
        return res

    def list_indices(self, pattern: str = "*") -> OpensearchResult[list[str]]:
        """List the names of indices matching a pattern, sorted.

        Args:
            pattern: an index-name pattern (wildcards allowed); defaults to all.

        Returns:
            The matching index names, sorted.
        """
        res = self.get(f"_cat/indices/{pattern}?h=index&format=json")
        if not res:
            return res
        return Success(sorted(row["index"] for row in res.data))

    def rollover(
        self,
        alias: str,
        *,
        settings: dict[str, Any] | None = None,
        dry_run: bool = False,
        timeout: int = 120,
    ) -> OpensearchResult[dict[str, Any]]:
        """Roll the write alias ``alias`` over to a new index (``POST <alias>/_rollover``).

        ``settings`` apply to the new index only and override its template.
        ``dry_run=True`` reports the new index's name without creating it.
        """
        # Creating the new index takes over 30 s on staging.
        body = None if settings is None else {"settings": settings}
        path = f"{alias}/_rollover" + _query_string({"dry_run": dry_run})
        return self.request("POST", path, body, timeout)

    def reindex(
        self,
        source: str,
        dest: str,
        *,
        script: dict[str, Any] | None = None,
        wait_for_completion: bool = True,
        refresh: bool = False,
        timeout: int = REQUEST_TIMEOUT,
    ) -> OpensearchResult[dict[str, Any]]:
        """Copy documents from ``source`` into ``dest`` (``POST _reindex``).

        ``script`` optionally transforms each document as it is copied. With
        ``wait_for_completion=False`` the call returns a task id to poll (see
        :meth:`get_task`). For advanced options (a source query, a destination
        pipeline, ...) build the request with :meth:`request` directly.
        """
        body: dict[str, Any] = {"source": {"index": source}, "dest": {"index": dest}}
        if script is not None:
            body["script"] = script
        path = "_reindex" + _query_string(
            {"wait_for_completion": wait_for_completion, "refresh": refresh}
        )
        return self.request("POST", path, body, timeout)

    def update_by_query(
        self,
        query: dict[str, Any],
        index: str | None = None,
        *,
        script: dict[str, Any] | None = None,
        conflicts: str | None = None,
        wait_for_completion: bool = True,
        refresh: bool = False,
        timeout: int = REQUEST_TIMEOUT,
    ) -> OpensearchResult[dict[str, Any]]:
        """Update documents matching ``query`` in place (``POST _update_by_query``).

        ``script`` transforms each matched document. With
        ``wait_for_completion=False`` the call returns a task id to poll (see
        :meth:`get_task`). ``conflicts="proceed"`` continues past version conflicts
        instead of aborting.
        """
        body: dict[str, Any] = {"query": query}
        if script is not None:
            body["script"] = script
        params: dict[str, Any] = {}
        if conflicts is not None:
            params["conflicts"] = conflicts
        params["wait_for_completion"] = wait_for_completion
        params["refresh"] = refresh
        path = f"{index or self.default_index}/_update_by_query" + _query_string(params)
        return self.request("POST", path, body, timeout)

    def get_task(self, task_id: str) -> OpensearchResult[dict[str, Any]]:
        """Return a task document by id (``GET _tasks/<task_id>``)."""
        return self.request("GET", f"_tasks/{task_id}")
