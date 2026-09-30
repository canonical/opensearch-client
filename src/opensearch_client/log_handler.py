# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""A logging handler that ships log records to OpenSearch.

The handler is a standard :class:`logging.Handler`: attach it to a logger, and
every record that reaches it is indexed into OpenSearch through an
:class:`~opensearch_client.client.OpensearchClient`. Batching, retries and transport
selection are delegated to :meth:`OpensearchClient.bulk`; this module only adds
what a log handler needs on top of it:

- a bounded in-memory buffer, so logging never blocks on the network;
- a background thread that flushes the buffer by size or by interval;
- a final flush on shutdown, so the last records are not lost;
- conversion of a ``LogRecord`` into an ECS-style document;
- a guarantee that a shipping failure never raises into the application and
  never feeds back into the handler itself.
"""

import copy
import logging
import traceback
from datetime import datetime, timezone
from threading import Event, Lock, Thread, local
from typing import Any, Dict, List

from opensearch_client.client import OpensearchClient

# How long close() waits for an in-progress send before flushing the rest itself.
_CLOSE_JOIN_SECONDS = 5.0


class OpensearchHandler(logging.Handler):
    """Ship log records to an OpenSearch index.

    ``emit`` queues a record and sends the queue with ``OpensearchClient.bulk``
    once ``buffer_size`` records are waiting. A background thread also sends the
    queue every ``flush_interval`` seconds, so records from a quiet process are
    not held indefinitely. Records the cluster rejects are counted in ``dropped``.
    """

    def __init__(
        self,
        client: OpensearchClient,
        index: str,
        *,
        level: int = logging.NOTSET,
        buffer_size: int = 1000,
        flush_interval: float = 5.0,
        max_queue: int = 10000,
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
                document, e.g. ``{"service": {"name": "my-service"}}``.
        """
        logging.Handler.__init__(self)

        self.buffer_size = buffer_size
        self.flush_interval = flush_interval
        self.index = index
        self.level = level
        self.max_queue = max_queue
        if extra_fields is None:
            extra_fields = {}
        self.extra_fields = copy.deepcopy(extra_fields.copy())

        self._client = client
        self._index_ready = False

        # Guards _buffer, _dropped and _index_ready. Held only for quick updates,
        # never while sending, so a slow send does not block other threads' logging.
        self._lock = Lock()
        self._buffer: List[Dict[str, Any]] = []
        self._dropped = 0
        # Per thread, so records logged on another thread are still shipped.
        self._local = local()

        self._stop_event = Event()
        self._flush_thread = Thread(
            target=self._run, name="osclient-log-flush", daemon=True
        )
        self._flush_thread.start()

    @property
    def dropped(self) -> int:
        """The number of records discarded so far (queue overflow or send failure)."""
        return self._dropped

    def emit(self, record: logging.LogRecord) -> None:
        """Queue one record for shipping; never block and never raise.

        Records logged while this handler is itself sending (for example by the
        transport or ``requests``) are ignored, so shipping a batch cannot
        generate new records that get shipped in turn.

        Args:
            record (logging.LogRecord): the record to ship.
        """
        if getattr(self._local, "sending", False):
            return
        try:
            self.format(record)
            doc = self._to_document(record)

            # Add record to buffer
            with self._lock:
                self._buffer.append(doc)
                full = len(self._buffer) >= self.buffer_size
            if full:
                self.flush()
        except Exception:
            self.handleError(record)

    def flush(self) -> None:
        """Send everything currently queued, and wait until the send finishes.

        Documents the cluster rejects are added to ``dropped``.
        """
        with self._lock:
            if not self._buffer:
                return
            logs_buffer = self._buffer
            self._buffer = []
            index_ready = self._index_ready

        self._local.sending = True
        try:
            if not index_ready and self._ensure_index(self.index):
                with self._lock:
                    self._index_ready = True
            result = self._client.bulk(logs_buffer, self.index)
        finally:
            self._local.sending = False
        if not result:
            failed = result.data["failed"] if result.data else len(logs_buffer)
            with self._lock:
                self._dropped += failed

    def close(self) -> None:
        """Stop the background thread, send what is still queued, then close."""
        self._stop_event.set()
        self._flush_thread.join(_CLOSE_JOIN_SECONDS)
        self._flush_and_report_errors()
        super().close()

    def _to_document(self, record: logging.LogRecord) -> Dict[str, Any]:
        """Convert a log record into a document.

        Covers the timestamp, level, logger name, message, exception details when
        present, the host name, the program name, any ``extra=`` fields on the
        record, and the handler's ``extra_fields``.

        Args:
            record (logging.LogRecord): the record to convert.

        Returns:
            dict[str, Any]: the document to index.
        """
        timestamp = datetime.fromtimestamp(record.created, timezone.utc)
        doc: dict[str, Any] = {
            "@timestamp": timestamp.isoformat(),
            "message": record.getMessage(),
            "levelname": record.levelname,
            "name": record.name,
        }
        if record.exc_text:
            doc["exc_text"] = record.exc_text
        return doc

    def _index_for(self, record: logging.LogRecord) -> str:
        """Return the index a record belongs in (the base name, plus a date if used).

        Args:
            record (logging.LogRecord): the record being indexed.

        Returns:
            str: the target index name.
        """
        raise NotImplementedError

    def _ensure_index(self, index: str) -> bool:
        """Create the index if it does not exist yet.

        Args:
            index (str): the index to check or create.

        Returns:
            bool: True if the index exists or was created, False on failure.
        """
        exists = self._client.index_exists(index)
        if not exists:
            return False
        if exists.data:
            return True
        return bool(self._client.create_index({}, index))

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
        """Flush every ``flush_interval`` seconds until ``close`` is called.

        A failed flush never ends the loop.
        """
        while not self._stop_event.wait(self.flush_interval):
            self._flush_and_report_errors()

    def _flush_and_report_errors(self) -> None:
        """Flush, reporting any error to stderr as ``handleError`` does.

        Used where there is no record to hand to ``handleError`` and the caller
        must not see an exception: the background thread and ``close``.
        """
        try:
            self.flush()
        except Exception:
            if logging.raiseExceptions:
                traceback.print_exc()
