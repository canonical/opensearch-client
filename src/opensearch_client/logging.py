# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""A logging handler that ships log records to OpenSearch.

The handler is a standard :class:`logging.Handler`: attach it to a logger and
every record that reaches it is indexed into OpenSearch through an
:class:`~osclient.client.OpensearchClient`. Batching, retries and transport
selection are delegated to :meth:`OpensearchClient.bulk`; this module only adds
what a log handler needs on top of it:

- a bounded in-memory buffer, so logging never blocks on the network;
- a background thread that flushes the buffer by size or by interval;
- a final flush on shutdown, so the last records are not lost;
- conversion of a ``LogRecord`` into an ECS-style document;
- a guarantee that a shipping failure never raises into the application and
  never feeds back into the handler itself.
"""

import logging
from typing import Any

from osclient.client import OpensearchClient


class OpensearchHandler(logging.Handler):
    """Ship log records to an OpenSearch index.

    ``emit`` only enqueues a record; a background thread sends queued records
    with ``OpensearchClient.bulk``. If OpenSearch is unreachable the queue is
    bounded and the oldest records are dropped, and the drop count is kept in
    ``dropped``.
    """

    def __init__(
        self,
        client: OpensearchClient,
        index: str,
        *,
        level: int = logging.NOTSET,
        buffer_size: int = 1000,
        flush_interval: float = 5.0,
        max_queue: int = 10_000,
        extra_fields: dict[str, Any] | None = None,
    ) -> None:
        """Create the handler and start its background flush thread.

        Args:
            client (OpensearchClient): the client used to index documents.
            index (str): the base index name records are written to.
            level (int): the minimum level the handler accepts.
            buffer_size (int): flush as soon as this many records are queued.
            flush_interval (float): flush at least this often, in seconds.
            max_queue (int): the most records held in memory; beyond this the
                oldest are dropped and counted in ``dropped``.
            extra_fields (dict[str, Any] | None): fields added to every
                document, e.g. ``{"service": {"name": "superset-collector"}}``.
        """
        raise NotImplementedError

    @property
    def dropped(self) -> int:
        """The number of records discarded so far (queue overflow or send failure)."""
        raise NotImplementedError

    def emit(self, record: logging.LogRecord) -> None:
        """Queue one record for shipping; never block and never raise.

        Records from the libraries the handler itself uses to send (``requests``,
        ``urllib3``, ``osclient``) are ignored, so shipping a batch cannot
        generate new records that get shipped in turn.

        Args:
            record (logging.LogRecord): the record to ship.
        """
        raise NotImplementedError

    def flush(self) -> None:
        """Send everything currently queued, and wait until the send finishes."""
        raise NotImplementedError

    def close(self) -> None:
        """Stop the background thread after a final flush, then close the handler."""
        raise NotImplementedError

    def _to_document(self, record: logging.LogRecord) -> dict[str, Any]:
        """Convert a log record into an ECS-style document.

        Covers the timestamp, level, logger name, message, exception details when
        present, the host name, the program name, any ``extra=`` fields on the
        record, and the handler's ``extra_fields``.

        Args:
            record (logging.LogRecord): the record to convert.

        Returns:
            dict[str, Any]: the document to index.
        """
        raise NotImplementedError

    def _index_for(self, record: logging.LogRecord) -> str:
        """Return the index a record belongs in (the base name, plus a date if used).

        Args:
            record (logging.LogRecord): the record being indexed.

        Returns:
            str: the target index name.
        """
        raise NotImplementedError

    def _ensure_index(self, index: str) -> bool:
        """Create the index if it does not exist yet (checked once per index).

        Args:
            index (str): the index to check or create.

        Returns:
            bool: True if the index exists or was created, False on failure.
        """
        raise NotImplementedError

    def _send(self, documents: list[tuple[str, dict[str, Any]]]) -> None:
        """Index ``(index, document)`` pairs with ``OpensearchClient.bulk``.

        A failure is never raised or logged through ``logging``; the affected
        documents are added to ``dropped`` instead.

        Args:
            documents (list[tuple[str, dict[str, Any]]]): the documents to send,
                each paired with its target index.
        """
        raise NotImplementedError

    def _run(self) -> None:
        """Run the background loop: flush when the buffer fills or the interval passes.

        Exits after a final flush once ``close`` signals it to stop.
        """
        raise NotImplementedError
