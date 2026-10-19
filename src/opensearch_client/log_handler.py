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
- a guarantee that a shipping failure never raises into the application and
  never feeds back into the handler itself.

What is indexed for a record is up to the handler's formatter. For ECS
documents, set an ``ECSLogFormatter`` on the handler, and add an
``ECSLogContextFilter`` for the host and service the logs come from, or pass
``ecs_formatter=True`` and ``ecs_context=True`` to the handler.

The threads, the lock and the events are described in
``docs/explanation/log-handler-concurrency.md``.
"""

import json
import logging
import os
import socket
import sys
import uuid
from collections import deque
from datetime import datetime, timezone
from threading import Condition, Event, Lock, Thread, current_thread
from typing import Any

from opensearch_client.client import OpensearchClient

logger = logging.getLogger(__name__)

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
# have come from ``extra=``. ``ecs_context`` is set by ECSLogContextFilter.
_STANDARD_RECORD_ATTRIBUTES = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None))
) | {"message", "asctime", "ecs_context"}


def _format_timestamp(created: float) -> str:
    """Format a record's creation time as UTC to the millisecond, e.g. ``...Z``."""
    timestamp = datetime.fromtimestamp(created, timezone.utc)
    return timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


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


class ECSLogContextFilter(logging.Filter):
    """Add the context the logs come from to every record.

    The context is what describes where the logs come from, not what was logged:
    ``host.name``, ``service.name``, ``service.ephemeral_id``, ``event.dataset``
    and the caller's ``extra_fields``. The filter sets it on the record as
    ``ecs_context``, and :class:`ECSLogFormatter` merges it into the document.
    """

    def __init__(
        self,
        *,
        service_name: str | None = None,
        extra_fields: dict[str, Any] | None = None,
    ) -> None:
        """Create the filter.

        Args:
            service_name (str | None): the name of the service that is producing the
                logs. Defaults to the name of the running program.
            extra_fields (dict[str, Any] | None): ECS fields merged into every
                document. They replace any field of the same name, including
                those taken from the record, e.g.
                ``{"service": {"version": "1.2"}, "labels": {"env": "prod"}}``.
        """
        super().__init__()
        json.dumps(extra_fields)  # Ensure JSON serializeable

        if service_name is None:
            # python -m package.module gives the module, a script gives its name.
            main_spec = getattr(sys.modules.get("__main__"), "__spec__", None)
            if main_spec is not None and main_spec.name:
                service_name = main_spec.name
            elif sys.argv and sys.argv[0]:
                service_name = os.path.splitext(os.path.basename(sys.argv[0]))[0]
            else:
                service_name = "unknown_service"

        self._context: dict[str, Any] = {
            "host": {"name": socket.gethostname()},
            # systemd sets INVOCATION_ID to a new value each time the unit starts.
            "service": {
                "name": service_name,
                "ephemeral_id": os.environ.get("INVOCATION_ID") or uuid.uuid4().hex,
            },
            "event": {"dataset": service_name},
        }
        _deep_merge(self._context, extra_fields or {})

    def filter(self, record: logging.LogRecord) -> bool:
        """Set the context on the record, and let the record through.

        Args:
            record (logging.LogRecord): the record to add the context to.

        Returns:
            bool: always True.
        """
        setattr(record, "ecs_context", self._context)
        return True


class ECSLogFormatter(logging.Formatter):
    """Format log records as ECS documents.

    ``format`` returns a record's document as JSON text, and ``format_document``
    returns it as a dict. The document gets its host and service fields from the
    ``ecs_context`` that an :class:`ECSLogContextFilter` sets on the record, so
    add one wherever this formatter is used.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Return the record's ECS document as JSON text.

        Args:
            record (logging.LogRecord): the record to convert.

        Returns:
            str: the document, serialized.
        """
        return json.dumps(self.format_document(record))

    def format_document(self, record: logging.LogRecord) -> dict[str, Any]:
        """Convert a log record into an ECS document.

        The document starts with these fields from the record:
        - ``@timestamp``: when the record was created, in UTC to the millisecond;
        - ``message``: the message with its arguments merged in;
        - ``log.level`` (lower case), ``log.logger``, and the call site as
          ``log.origin.file.name``, ``log.origin.file.line`` and
          ``log.origin.function``;
        - ``event.severity``: the numeric level;
        - ``process.pid``, ``process.thread.id`` and ``process.thread.name``;
        - ``error.type``, ``error.message`` and ``error.stack_trace`` for an
          exception, or ``error.stack_trace`` alone for ``stack_info``;
        - ``labels``: the record's ``extra=`` attributes, as strings;
        - ``python.msg`` (the format string), ``python.pathname`` (the full path,
          since file names repeat across programs) and ``python.task_name``.

        It adds ``ecs.version`` and ``agent.type``, then merges in the record's
        ``ecs_context``, if an ``ECSLogContextFilter`` set one: ``host.name``,
        ``service.name``, ``service.ephemeral_id``, ``event.dataset`` and the
        caller's ``extra_fields``. Where a context field has the same name as
        another field, the context field wins. The ``extra_fields`` are assumed to
        be ECS compliant, and are not reformatted.

        Args:
            record (logging.LogRecord): the record to convert.

        Returns:
            dict[str, Any]: the document to index.
        """
        fields: dict[str, Any] = {
            "@timestamp": _format_timestamp(record.created),
            "message": record.getMessage(),
            "log": {
                "level": record.levelname.lower(),
                "logger": record.name,
                "origin": {
                    "file": {"name": record.filename, "line": record.lineno},
                    "function": record.funcName,
                },
            },
            "ecs": {"version": _ECS_VERSION},
            "agent": {"type": "opensearch_client"},
            "event": {"severity": record.levelno},
            "process": {
                "pid": record.process,
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
                "stack_trace": record.exc_text or self.formatException(exc_info),
            }
        elif record.stack_info:
            fields["error"] = {"stack_trace": self.formatStack(record.stack_info)}

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
            except Exception:  # noqa: BLE001
                labels[safe_key] = "<unprintable>"
        if labels:
            fields["labels"] = labels

        _deep_merge(fields, getattr(record, "ecs_context", {}))
        return self._strip_null_fields(fields)

    def _strip_null_fields(self, mapping: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of ``mapping`` without ``None`` values or empty dicts.

        Args:
            mapping (dict[str, Any]): the dict to clean, including nested dicts.

        Returns:
            dict[str, Any]: the cleaned copy.
        """
        cleaned = {}
        for key, value in mapping.items():
            cleaned_value = value
            if isinstance(value, dict):
                cleaned_value = self._strip_null_fields(value)
            if cleaned_value is None or cleaned_value == {}:
                continue
            cleaned[key] = cleaned_value
        return cleaned


class OpensearchLogHandler(logging.Handler):
    """A :class:`logging.Handler` that queues records and ships them to OpenSearch.

    See the module documentation for the buffer, flush thread and retry
    behaviour.
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
        add_ecs_formatter: bool = False,
        add_ecs_context: bool = False,
    ) -> None:
        """Create the handler and start its background flush thread.

        Args:
            client (OpensearchClient): the client used to index documents.
            index (str): the base index name records are written to.
            level (int): the minimum level the handler accepts.
            flush_threshold (int): flush as soon as a new record brings the queue
                to this many records. While a send is running, or after one
                failed, the queue can grow past this, up to ``buffer_limit``,
                without starting more sends.
            flush_interval (float): flush at least this often, in seconds.
            buffer_limit (int): the most records held in the buffer; beyond this
                the oldest are discarded, and a warning says how many. Keep it at
                or above ``flush_threshold``.
            add_ecs_formatter (bool): set an ``ECSLogFormatter`` on the handler.
            add_ecs_context (bool): add an ``ECSLogContextFilter`` with its defaults to
                the handler. For a service name or extra fields, add one yourself.
        """
        super().__init__(level)

        self._client = client
        self.index = index
        self._should_check_index = True

        # The one lock: it guards the buffer, _overflowed and the send counters,
        # and _send_finished is built on it. Held only for quick updates, never
        # while sending, so a slow send does not block logging.
        self._lock = Lock()

        self.flush_threshold = flush_threshold
        self.buffer_limit = buffer_limit
        # Appending to a full deque discards its oldest record.
        self._buffer: deque[dict[str, Any]] = deque(maxlen=buffer_limit)
        self._overflowed = 0
        # See "Waiting for a flush" in docs/explanation/log-handler-concurrency.md.
        self._sends_started = 0
        self._sends_finished = 0
        self._send_finished = Condition(self._lock)

        # The thread starts last so that attributes it uses exist before it runs.
        self.flush_interval = flush_interval
        self._flush_thread_wakeup = Event()
        self._flush_thread_stop = Event()
        self._flush_thread = Thread(
            target=self._run_flush_loop, name="opensearch_client-log-flush", daemon=True
        )
        # Filter out records emitted by the flushing thread
        self.addFilter(lambda _: current_thread() is not self._flush_thread)
        if add_ecs_formatter:
            self.setFormatter(ECSLogFormatter())
        if add_ecs_context:
            self.addFilter(ECSLogContextFilter())
        self._flush_thread.start()

    def emit(self, record: logging.LogRecord) -> None:
        """Queue one record for the flush thread; never block and never raise.

        ``record`` is formatted in one of three ways:

        - If a logging.Formatter is attached and has a ``format_document`` method (see
          ``ECSLogFormatter`` as an example), it is used to format the record into a
          dict. No further transformations are applied, so long as the result is a dict.
          If it is not, the string from the formatter's ``format`` is embedded as in
          the last case below.
        - Otherwise, if a logging.Formatter is attached, its ``format`` method is
          applied, then:
          - The formatted record string is parsed as a JSON dict. If this succeeds, the
            dict is used with no further transformations.
          - Otherwise, the formatted string is embedded in a new dict with
            ``@timestamp`` and ``message``.

        If ``close`` has been called, records are dropped instead of queued.

        Args:
            record (logging.LogRecord): the record to ship.
        """
        try:
            document: dict | None = None
            text = None

            format_document = getattr(self.formatter, "format_document", None)
            if format_document is not None:
                document = format_document(record)
            else:
                text = self.format(record)
                try:
                    document = json.loads(text)
                except json.JSONDecodeError:
                    document = None

            if not isinstance(document, dict):
                document = {
                    "@timestamp": _format_timestamp(record.created),
                    "message": text if text is not None else self.format(record),
                }

            with self._lock:
                if self._flush_thread_stop.is_set():
                    return
                if len(self._buffer) == self.buffer_limit:
                    self._overflowed += 1
                # Wake the flush thread only when this record fills the buffer.
                was_below_threshold = len(self._buffer) < self.flush_threshold
                self._buffer.append(document)
                reached_threshold = len(self._buffer) >= self.flush_threshold
            if was_below_threshold and reached_threshold:
                self._flush_thread_wakeup.set()
        except RecursionError:
            raise
        except Exception:  # noqa: BLE001
            self.handleError(record)

    def flush(self) -> None:
        """Ask the flush thread to send the queue now, and wait for it to finish.

        Every record queued before the call is in the send that ``flush`` waits
        for. This tries again even while the handler is waiting after a failed
        send. Waits at most 10 seconds, and returns at once if the handler is
        closed.
        """
        if not self._flush_thread.is_alive():
            return
        with self._lock:
            # See "Waiting for a flush" in docs/explanation/log-handler-concurrency.md.
            awaited_send = self._sends_started + 1
            self._flush_thread_wakeup.set()
            self._send_finished.wait_for(
                lambda: self._sends_finished >= awaited_send, _FLUSH_WAIT_SECONDS
            )

    def close(self) -> None:
        """Stop the flush thread after its final send, then close the handler.

        Waits at most 5 seconds for that final send. Records it cannot send are
        discarded, and a warning says how many.
        """
        self._flush_thread_stop.set()
        self._flush_thread_wakeup.set()
        self._flush_thread.join(_CLOSE_JOIN_SECONDS)
        super().close()

    def _run_flush_loop(self) -> None:
        """Send the queue on the flush thread until ``close`` is called.

        Sends every ``flush_interval`` seconds, when woken by a full buffer, and
        when ``flush`` asks. A failed send never ends the loop. After ``close``
        the loop makes one last send and exits.

        Records lost along the way are reported here, through ``logger``: this
        handler ignores its own thread's records, so the warnings cannot feed
        back into it.
        """
        stopping = False
        while not stopping:
            self._flush_thread_wakeup.wait(self.flush_interval)
            # See "Starting a send" in docs/explanation/log-handler-concurrency.md.
            with self._lock:
                self._flush_thread_wakeup.clear()
                self._sends_started += 1
            # Read after the clear: close sets the stop request, then the wakeup.
            stopping = self._flush_thread_stop.is_set()
            try:
                self._send_buffered()
            except Exception:
                logger.exception("sending buffered log records failed, they are lost")
            with self._lock:
                self._sends_finished += 1
                self._send_finished.notify_all()

        with self._lock:
            unsent = len(self._buffer)
            self._buffer.clear()
        if unsent:
            logger.warning(
                "%d log records were not sent before the handler closed", unsent
            )

    def _send_buffered(self) -> None:
        """Send everything queued with ``OpensearchClient.bulk``, if anything is.

        Runs only on the flush thread. It first reports the records discarded
        because the buffer was full since the last send. Records that still fail
        for a temporary reason after ``bulk``'s retries go back to the front of the
        queue. Records the cluster rejects are discarded, with a warning.
        """
        with self._lock:
            overflowed = self._overflowed
            self._overflowed = 0
            documents = self._buffer
            self._buffer = deque(maxlen=self.buffer_limit)
        if overflowed:
            logger.warning(
                "%d log records were discarded because the buffer was full", overflowed
            )
        if not documents:
            return

        if self._should_check_index:
            # _should_check_index is always cleared, regardless of the outcome. An
            # account may have 'write' but not 'read metadata' permissions. In this
            # case, clearing only on success would lead to repeated, unnecessary calls.
            self._should_check_index = False
            self._client.ensure_index(self.index)
        result = self._client.bulk(documents, self.index, max_retries=_SEND_RETRIES)
        if result:
            return

        kept = []
        rejected = 0
        for failure in result.data["failures"]:
            status = failure["status"]
            error_type = (failure.get("error") or {}).get("type")
            if error_type == "index_not_found_exception":
                self._should_check_index = True  # create the index before the next send
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
        if rejected:
            logger.warning("the cluster rejected %d log records", rejected)
        with self._lock:
            # Built with maxlen, the deque keeps the newest: the kept records, the
            # oldest, are the first to go if they no longer all fit.
            requeued = deque([*kept, *self._buffer], maxlen=self.buffer_limit)
            self._overflowed += len(kept) + len(self._buffer) - len(requeued)
            self._buffer = requeued
