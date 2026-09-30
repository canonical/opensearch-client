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
from threading import Event, Lock, Thread, current_thread
from typing import Any, Dict, List

from opensearch_client.client import OpensearchClient

# How long flush() waits for the flush thread to finish a send. Bounded so that
# logging.shutdown cannot hang when OpenSearch is unreachable.
_FLUSH_WAIT_SECONDS = 10.0

# How long close() waits for the flush thread to finish its final send.
_CLOSE_JOIN_SECONDS = 5.0

# How many more times bulk() sends documents that failed to index.
_SEND_RETRIES = 2


class OpensearchHandler(logging.Handler):
    """Ship log records to an OpenSearch index.

    ``emit`` only queues a record. A background thread sends the queue with
    ``OpensearchClient.bulk`` every ``flush_interval`` seconds, or as soon as
    ``buffer_size`` records are waiting, so ``emit`` never blocks on the network.
    If more than ``max_queue`` records are waiting, the oldest are dropped.

    ``OpensearchClient.bulk`` sends documents that fail to index twice more, then
    gives up on them. Every dropped record is counted in ``dropped``. A retry can
    index a record twice if only the response was lost.

    Records logged by the flush thread itself, such as what ``requests`` logs
    while sending, are ignored so that sending cannot create records to send.
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

        # Guards _buffer, _dropped and _flush_waiters. Held only for quick updates,
        # never while sending, so a slow send does not block other threads' logging.
        self._lock = Lock()
        self._buffer: List[Dict[str, Any]] = []
        self._dropped = 0
        self._flush_waiters: List[Event] = []

        self._wake_event = Event()
        self._stop_event = Event()
        self._flush_thread = Thread(
            target=self._run, name="opensearch_client-log-flush", daemon=True
        )
        # Runs before handle() takes the handler lock, so the flush thread's own
        # records can never wait on a thread that is waiting for the flush thread.
        self.addFilter(self._is_not_from_flush_thread)
        self._flush_thread.start()

    @property
    def dropped(self) -> int:
        """The number of records discarded so far (queue overflow or send failure)."""
        return self._dropped

    def emit(self, record: logging.LogRecord) -> None:
        """Queue one record for the flush thread; never block and never raise.

        Args:
            record (logging.LogRecord): the record to ship.
        """
        try:
            self.format(record)
            doc = self._to_document(record)

            with self._lock:
                self._buffer.append(doc)
                if len(self._buffer) > self.max_queue:
                    del self._buffer[0]
                    self._dropped += 1
                full = len(self._buffer) >= self.buffer_size
            if full:
                self._wake_event.set()
        except Exception:
            self.handleError(record)

    def flush(self) -> None:
        """Ask the flush thread to send the queue, and wait for it to finish.

        Waits at most 10 seconds, and returns at once if the handler is closed.
        """
        if not self._flush_thread.is_alive():
            return
        done = Event()
        with self._lock:
            self._flush_waiters.append(done)
        self._wake_event.set()
        done.wait(_FLUSH_WAIT_SECONDS)

    def close(self) -> None:
        """Stop the flush thread after its final send, then close the handler.

        Waits at most 5 seconds for that final send.
        """
        self._stop_event.set()
        self._wake_event.set()
        self._flush_thread.join(_CLOSE_JOIN_SECONDS)
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

    def _run(self) -> None:
        """Send the queue on the flush thread until ``close`` is called.

        Sends every ``flush_interval`` seconds, when woken by a full buffer, and
        when ``flush`` asks. A failed send never ends the loop. After ``close``
        the loop makes one last send and exits.
        """
        while True:
            self._wake_event.wait(self.flush_interval)
            self._wake_event.clear()
            stopping = self._stop_event.is_set()
            with self._lock:
                waiters = self._flush_waiters
                self._flush_waiters = []
            try:
                self._send_buffered()
            except Exception:
                # Reported like handleError does, as there is no record to hand it.
                if logging.raiseExceptions:
                    traceback.print_exc()
            for waiter in waiters:
                waiter.set()
            if stopping:
                break

    def _send_buffered(self) -> None:
        """Send everything queued with ``OpensearchClient.bulk``, if anything is.

        Runs only on the flush thread. Records that still fail after ``bulk``'s
        retries, or that are lost to an exception, are added to ``dropped``.
        """
        with self._lock:
            if not self._buffer:
                return
            documents = self._buffer
            self._buffer = []

        try:
            if not self._index_ready:
                self._index_ready = self._ensure_index(self.index)
            result = self._client.bulk(documents, self.index, max_retries=_SEND_RETRIES)
        except Exception:
            with self._lock:
                self._dropped += len(documents)
            raise
        if not result:
            failed = result.data["failed"] if result.data else len(documents)
            with self._lock:
                self._dropped += failed

    def _is_not_from_flush_thread(self, record: logging.LogRecord) -> bool:
        """Filter out records created on the flush thread."""
        return current_thread() is not self._flush_thread
