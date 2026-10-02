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

import json
import logging
import os
import socket
import sys
import traceback
import uuid
from collections import deque
from datetime import datetime, timezone
from threading import Event, Lock, Thread, current_thread
from typing import Any

from opensearch_client.client import OpensearchClient

# How long flush() waits for the flush thread to finish a send. Bounded so that
# logging.shutdown cannot hang when OpenSearch is unreachable.
_FLUSH_WAIT_SECONDS = 10.0

# How long close() waits for the flush thread to finish its final send.
_CLOSE_JOIN_SECONDS = 5.0

# How many more times bulk() immediately resends documents that failed to index.
# Records that still fail are kept and tried again on a later send.
_SEND_RETRIES = 2

# Errors that are classified as transient, and should be retried: the disk watermark
# blocking writes, and an index that was deleted and will be created again.
_TEMPORARY_ERROR_TYPES = ("cluster_block_exception", "index_not_found_exception")

# The ECS version the documents conform to.
_ECS_VERSION = "9.0"

# Every attribute a LogRecord has, so that anything else on a record is known to
# have come from ``extra=``. Taken from a real record, so attributes added by a
# later Python version are not mistaken for extras.
_STANDARD_RECORD_ATTRIBUTES = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None))
) | {"message", "asctime"}


def _deep_merge(target: dict[str, Any], source: dict[str, Any]) -> None:
    """Merge ``source`` into ``target``, descending into nested dicts.

    Where both hold a dict under the same key their contents are merged. Anything
    else in ``source`` replaces what ``target`` has.

    Args:
        target (dict[str, Any]): the dict to update.
        source (dict[str, Any]): the values to merge in.
    """
    for key, value in source.items():
        existing = target.get(key)
        if isinstance(value, dict) and isinstance(existing, dict):
            _deep_merge(existing, value)
        else:
            target[key] = value


def _without_empty(mapping: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``mapping`` without ``None`` values or empty dicts.

    Args:
        mapping (dict[str, Any]): the dict to clean, including nested dicts.

    Returns:
        dict[str, Any]: the cleaned copy.
    """
    cleaned = {}
    for key, value in mapping.items():
        if isinstance(value, dict):
            value = _without_empty(value)
        if value is None or value == {}:
            continue
        cleaned[key] = value
    return cleaned


class OpensearchHandler(logging.Handler):
    """Ship log records to an OpenSearch index.

    ``emit`` only queues a record. A background thread sends the queue with
    ``OpensearchClient.bulk`` every ``flush_interval`` seconds, or as soon as
    ``flush_threshold`` records are waiting, so ``emit`` never blocks on the
    network. If more than ``buffer_limit`` records are waiting, the oldest are
    dropped.

    ``OpensearchClient.bulk`` sends documents that fail to index twice more. If
    they still fail for a temporary reason (the cluster is unreachable,
    overloaded or unavailable), they go back to the front of the queue and are
    tried again one ``flush_interval`` later, or when ``flush`` is called. A
    document the cluster rejects for its content is dropped at once. Records that
    are still unsent when the handler closes are dropped. Every dropped record is
    counted in ``dropped``. A retry can index a record twice if only the response
    was lost.

    Records logged by the flush thread itself, such as what ``requests`` logs
    while sending, are ignored so that sending cannot create records to send.

    Each record becomes an ECS document; ``_format_record`` lists the fields.
    """

    def __init__(
        self,
        client: OpensearchClient,
        index: str,
        *,
        level: int = logging.NOTSET,
        flush_threshold: int = 1000,
        flush_interval: float = 5.0,
        buffer_limit: int = 10000,
        service_name: str | None = None,
        extra_fields: dict[str, Any] | None = None,
    ) -> None:
        """Create the handler and start its background flush thread.

        Args:
            client (OpensearchClient): the client used to index documents.
            index (str): the base index name records are written to.
            level (int): the minimum level the handler accepts.
            flush_threshold (int): flush as soon as this many records are
                queued. The buffer can grow past this while a send is running
                or failing, up to ``buffer_limit``.
            flush_interval (float): flush at least this often, in seconds.
            buffer_limit (int): the most records held in the buffer; beyond this
                the oldest are dropped and counted in ``dropped``. Keep it at
                or above ``flush_threshold``.
            service_name (str | None): the ECS ``service.name`` of every document,
                for example the syslog tag the collector sends events under, so
                logs and events can be matched. Defaults to the name of the
                running program.
            extra_fields (dict[str, Any] | None): ECS fields merged into every
                document. They replace any field of the same name, including
                those taken from the record, e.g.
                ``{"service": {"version": "1.2"}, "labels": {"env": "prod"}}``.
        """
        super().__init__(level)

        # Get the client and index name
        self._client = client
        self.index = index
        self._index_ready = False

        # Create the lock
        # Guards _buffer, _dropped, _backing_off and _flush_waiters. Held
        # only for quick updates, never while sending, so a slow send does not
        # block other threads' logging.
        self._lock = Lock()

        # Configure the buffer
        self._buffer: deque[dict[str, Any]] = deque()
        self.flush_threshold = flush_threshold
        self.buffer_limit = buffer_limit
        self._dropped = 0
        # True after a send failed and its records were kept. While set, a full
        # buffer no longer wakes the flush thread, so it retries once per interval
        # instead of in a loop.
        self._backing_off = False
        self._flush_waiters: list[Event] = []

        # Creates the base document with fields that are shared between all logs
        self._static_document = self._build_static_document(
            service_name, extra_fields or {}
        )

        # Configure the flush mechanism / timer. The thread starts last, so that
        # everything it uses exists before it runs.
        self.flush_interval = flush_interval
        self._wake_event = Event()
        self._stop_event = Event()
        self._flush_thread = Thread(
            target=self._run, name="opensearch_client-log-flush", daemon=True
        )
        # Filter out records emitted by the flushing thread
        self.addFilter(lambda record: current_thread() is not self._flush_thread)
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
            doc = self._format_record(record)

            with self._lock:
                self._buffer.append(doc)
                self._drop_oldest_beyond_buffer_limit()
                full = len(self._buffer) >= self.flush_threshold
                wake = full and not self._backing_off
            if wake:
                self._wake_event.set()
        except Exception:
            self.handleError(record)

    def flush(self) -> None:
        """Ask the flush thread to send the queue now, and wait for it to finish.

        This tries again even while the handler is waiting after a failed send.
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

        Waits at most 5 seconds for that final send. Records it cannot send are
        dropped.
        """
        self._stop_event.set()
        self._wake_event.set()
        self._flush_thread.join(_CLOSE_JOIN_SECONDS)
        super().close()

    def _build_static_document(
        self,
        service_name: str | None,
        extra_fields: dict[str, Any],
    ) -> dict[str, Any]:
        """Build the fields that are the same in every document.

        Args:
            service_name (str | None): the ``service.name``, or None to derive it.
            extra_fields (dict[str, Any]): caller fields, merged last.

        Returns:
            dict[str, Any]: the fields every document starts from.
        """
        # If the handler was not given a `service_name` at initialization
        if service_name is None:
            # python -m package.module gives the module, a script gives its name.
            main_spec = getattr(sys.modules.get("__main__"), "__spec__", None)
            if main_spec is not None and main_spec.name:
                service_name = main_spec.name
            elif sys.argv and sys.argv[0]:
                service_name = os.path.splitext(os.path.basename(sys.argv[0]))[0]
            else:
                service_name = "unknown_service"

        document: dict[str, Any] = {
            "ecs": {"version": _ECS_VERSION},
            "host": {"name": socket.gethostname()},
            # systemd sets INVOCATION_ID to a new value each time the unit starts.
            "service": {
                "name": service_name,
                "ephemeral_id": os.environ.get("INVOCATION_ID") or uuid.uuid4().hex,
            },
            "event": {"dataset": service_name},
            "agent": {"type": "opensearch_client"},
        }

        _deep_merge(document, extra_fields)
        return document

    def _format_record(self, record: logging.LogRecord) -> dict[str, Any]:
        """Convert a log record into an ECS document.

        The document starts from the fields every document shares: ``ecs.version``,
        ``host.name``, ``service.name`` and ``service.ephemeral_id``,
        ``event.dataset``, ``agent.type`` and ``agent.version``, and the caller's
        ``extra_fields``. The record then adds, replacing any overlap:

        The document starts with these fields derived from the record content:
        - ``@timestamp``: when the record was created, in UTC to the millisecond;
        - ``message``: the message with its arguments merged in;
        - ``log.level`` (lower case), ``log.logger``, and the call site as
          ``log.origin.file.name``, ``log.origin.file.line`` and
          ``log.origin.function``;
        - ``event.severity``: the numeric level
        - ``process.pid``, ``process.name``, ``process.thread.id`` and
          ``process.thread.name``;
        - ``error.type``, ``error.message`` and ``error.stack_trace`` for an
          exception, or ``error.stack_trace`` alone for ``stack_info``;
        - ``labels``: the record's ``extra=`` attributes, as strings;
        - ``python.msg`` (the format string), ``python.pathname`` (the full path,
          since file names repeat across programs) and ``python.task_name``.

        Then, it merges in the fields every document shares: ``ecs.version``,
        ``host.name``, ``service.name``, ``service.ephemeral_id``, ``event.dataset``,
        ``agent.type``, and the caller's ``extra_fields``.

        NOTE: The ``extra_fields`` parameter is assumed to be ECS compliant, and no
        reformatting is applied. WARNING: If ``extra_fields`` contains a duplicate
        field name to one assigned in this function, the user-determined value in
        ``extra_fields`` will take precedence, and will overwrite the previously
        assigned value.

        Args:
            record (logging.LogRecord): the record to convert.

        Returns:
            dict[str, Any]: the document to index.
        """
        timestamp = datetime.fromtimestamp(record.created, timezone.utc)
        fields: dict[str, Any] = {
            "@timestamp": timestamp.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "message": record.getMessage(),
            "log": {
                "level": record.levelname.lower(),
                "logger": record.name,
                "origin": {
                    "file": {"name": record.filename, "line": record.lineno},
                    "function": record.funcName,
                },
            },
            "event": {"severity": record.levelno},
            "process": {
                "pid": record.process,
                "name": record.processName,
                "thread": {"id": record.thread, "name": record.threadName},
            },
            "python": {
                "pathname": record.pathname,
                "task_name": getattr(record, "taskName", None),
            },
        }
        if isinstance(record.msg, str):
            fields["python"]["msg"] = record.msg

        exc_info = record.exc_info
        if isinstance(exc_info, tuple) and exc_info[0] is not None:
            fields["error"] = {
                "type": exc_info[0].__name__,
                "message": None if exc_info[1] is None else str(exc_info[1]),
                "stack_trace": record.exc_text,
            }
        elif record.stack_info:
            fields["error"] = {"stack_trace": record.stack_info}

        # A log can be invoked with the `extra` parameter (example below):
        #   `logger.info("message", extra={"extra_field": "extra_value"})`
        # Any extra fields (fields already in the record that are NOT standard
        #    attributes) should be nested under the field "labels"
        labels = {}
        for key, value in vars(record).items():
            if key in _STANDARD_RECORD_ATTRIBUTES or value is None:
                continue
            safe_key = key.replace(".", "_").replace("*", "_").replace("\\", "_")
            try:
                if isinstance(value, (dict, list, tuple)):
                    labels[safe_key] = json.dumps(value, default=str)
                else:
                    labels[safe_key] = str(value)
            except Exception:
                labels[safe_key] = "<unprintable>"
        if labels:
            fields["labels"] = labels

        _deep_merge(fields, self._static_document)
        return _without_empty(fields)

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
                with self._lock:
                    self._dropped += len(self._buffer)
                    self._buffer.clear()
                break

    def _send_buffered(self) -> None:
        """Send everything queued with ``OpensearchClient.bulk``, if anything is.

        Runs only on the flush thread. Records that still fail for a temporary
        reason after ``bulk``'s retries go back to the front of the queue. Records
        the cluster rejects, or that are lost to an exception, are added to
        ``dropped``.
        """
        with self._lock:
            if not self._buffer:
                return
            documents = self._buffer
            self._buffer = deque()

        try:
            if not self._index_ready:
                self._index_ready = self._ensure_index(self.index)
            result = self._client.bulk(documents, self.index, max_retries=_SEND_RETRIES)
        except Exception:
            with self._lock:
                self._dropped += len(documents)
            raise
        if result:
            with self._lock:
                self._backing_off = False
            return

        kept = []
        rejected = 0
        for failure in result.data["failures"]:
            status = failure["status"]
            error_type = (failure.get("error") or {}).get("type")
            if error_type == "index_not_found_exception":
                self._index_ready = False  # create the index before the next send
            is_temporary = (
                status is None
                or status in (408, 429)
                or status >= 500
                or error_type in _TEMPORARY_ERROR_TYPES
            )
            if is_temporary:
                kept.append(failure["document"])
            else:
                rejected += 1
        with self._lock:
            self._dropped += rejected
            self._buffer.extendleft(reversed(kept))
            self._drop_oldest_beyond_buffer_limit()
            self._backing_off = bool(kept)

    def _drop_oldest_beyond_buffer_limit(self) -> None:
        """Drop the oldest queued records while more than ``buffer_limit`` wait.

        The caller must hold the lock.
        """
        while len(self._buffer) > self.buffer_limit:
            self._buffer.popleft()
            self._dropped += 1
