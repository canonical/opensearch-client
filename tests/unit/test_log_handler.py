# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for opensearch_client.log_handler.OpensearchHandler."""

import contextlib
import json
import logging
import sys
import threading
import time
from collections.abc import Iterator
from typing import Any

from opensearch_client.client import OpensearchClient
from opensearch_client.log_handler import OpensearchHandler
from opensearch_client.result import Failure, OpensearchResult, Success

# How long the tests wait on another thread before deciding it is stuck. Only
# reached when the code under test is broken.
_WAIT_SECONDS = 5


class FakeTransport:
    """Answers every request with a preset result; can raise on the bulk request."""

    def __init__(
        self, result: OpensearchResult[Any], error: Exception | None = None
    ) -> None:
        self.result = result
        self.error = error

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: str = "application/json",
        timeout: int = 30,
    ) -> OpensearchResult[Any]:
        if self.error is not None and path == "_bulk":
            raise self.error
        return self.result


class ChattyTransport:
    """Logs through the root logger on every bulk request, as real transports do."""

    def __init__(self) -> None:
        self.bulk_bodies: list[bytes] = []

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: str = "application/json",
        timeout: int = 30,
    ) -> OpensearchResult[Any]:
        if path == "_bulk":
            self.bulk_bodies.append(body or b"")
            logging.warning("transport chatter")
        return Success({"items": []})


class MissingIndexTransport:
    """Reports the index missing until it is created; records every request."""

    def __init__(self) -> None:
        self.created = False
        self.calls: list[tuple[str, str]] = []

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: str = "application/json",
        timeout: int = 30,
    ) -> OpensearchResult[Any]:
        self.calls.append((method, path))
        if method == "GET":
            return Success({}) if self.created else Failure("missing", status=404)
        if method == "PUT":
            self.created = True
            return Success({})
        return Success({"items": []})


def _parse_bulk_documents(body: bytes | None) -> list[dict[str, Any]]:
    """Return the documents in a bulk body, skipping each document's action line."""
    lines = (body or b"").decode().splitlines()
    documents = []
    for position in range(1, len(lines), 2):
        documents.append(json.loads(lines[position]))
    return documents


class RecordingTransport:
    """Keeps every document sent in a bulk request, and signals each arrival."""

    def __init__(self) -> None:
        self.documents: list[dict[str, Any]] = []
        self.bulk_received = threading.Event()

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: str = "application/json",
        timeout: int = 30,
    ) -> OpensearchResult[Any]:
        if path == "_bulk":
            self.documents.extend(_parse_bulk_documents(body))
            self.bulk_received.set()
        return Success({"items": []})


class BlockingTransport:
    """Holds the first bulk request open until the test releases it."""

    def __init__(self) -> None:
        self.send_started = threading.Event()
        self.release_send = threading.Event()
        self.is_first_send = True
        self.messages: list[str] = []

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: str = "application/json",
        timeout: int = 30,
    ) -> OpensearchResult[Any]:
        if path == "_bulk":
            if self.is_first_send:
                self.is_first_send = False
                self.send_started.set()
                # Outlasts the test's own wait, so a broken handler cannot hang it.
                if not self.release_send.wait(_WAIT_SECONDS * 2):
                    raise RuntimeError("the send was never released")
            for document in _parse_bulk_documents(body):
                self.messages.append(document["message"])
        return Success({"items": []})


class RaisingTransport:
    """Raises on every bulk request, signalling each attempt."""

    def __init__(self) -> None:
        self.bulk_attempted = threading.Event()

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: str = "application/json",
        timeout: int = 30,
    ) -> OpensearchResult[Any]:
        if path == "_bulk":
            self.bulk_attempted.set()
            raise RuntimeError("boom")
        return Success({})


class ErrorRecordingHandler(OpensearchHandler):
    """Keeps the records passed to handleError instead of printing a traceback."""

    def __init__(self, client: OpensearchClient, index: str, **kwargs: Any) -> None:
        super().__init__(client, index, **kwargs)
        self.errored_records: list[logging.LogRecord] = []

    def handleError(self, record: logging.LogRecord) -> None:
        self.errored_records.append(record)


def _handler(transport: Any, **kwargs: Any) -> OpensearchHandler:
    # An hour, so the timer never fires during a test unless the test sets it.
    kwargs.setdefault("flush_interval", 3600.0)
    return OpensearchHandler(OpensearchClient(transport), "logs", **kwargs)


@contextlib.contextmanager
def _running(handler: OpensearchHandler) -> Iterator[OpensearchHandler]:
    """Yield the handler, then close it so its flush thread stops."""
    try:
        yield handler
    finally:
        handler.close()


def _record(message: str) -> logging.LogRecord:
    return logging.LogRecord("test", logging.INFO, __file__, 1, message, (), None)


def _emit_then_signal(
    handler: OpensearchHandler, record: logging.LogRecord, emitted: threading.Event
) -> None:
    handler.emit(record)
    emitted.set()


def test_index_is_created_on_first_flush_only() -> None:
    """Building the handler makes no requests; the first flush creates the index."""
    transport = MissingIndexTransport()
    with _running(_handler(transport)) as handler:
        assert transport.calls == []

        handler.emit(_record("one"))
        handler.flush()
        handler.emit(_record("two"))
        handler.flush()

        assert transport.calls == [
            ("GET", "logs"),
            ("PUT", "logs"),
            ("POST", "_bulk"),
            ("POST", "_bulk"),
        ]


def test_records_logged_while_sending_are_not_shipped() -> None:
    """On the root logger, the handler ignores what its own send path logs."""
    transport = ChattyTransport()
    with _running(_handler(transport)) as handler:
        root = logging.getLogger()
        previous_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        try:
            logging.getLogger("app").info("hello")
            handler.flush()
        finally:
            root.removeHandler(handler)
            root.setLevel(previous_level)

        assert len(transport.bulk_bodies) == 1
        assert b"hello" in transport.bulk_bodies[0]
        assert b"chatter" not in transport.bulk_bodies[0]


def test_flush_while_holding_the_handler_lock_does_not_stall() -> None:
    """logging.shutdown holds the handler lock while flushing; it must not stall."""
    transport = ChattyTransport()
    with _running(_handler(transport)) as handler:
        root = logging.getLogger()
        previous_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        try:
            handler.emit(_record("hello"))
            started = time.monotonic()
            handler.acquire()
            try:
                handler.flush()
            finally:
                handler.release()
            elapsed = time.monotonic() - started
        finally:
            root.removeHandler(handler)
            root.setLevel(previous_level)

        assert elapsed < _WAIT_SECONDS, "flush stalled while the handler lock was held"


def test_failed_send_is_counted_as_dropped() -> None:
    """A failed send does not raise; the lost records show up in ``dropped``."""
    transport = FakeTransport(Failure("down", status=500))
    with _running(_handler(transport)) as handler:
        handler.emit(_record("one"))
        handler.emit(_record("two"))
        handler.flush()

        assert handler.dropped == 2


def test_exception_while_sending_is_counted_as_dropped() -> None:
    """A send that raises loses its records, and they are counted."""
    with _running(_handler(RaisingTransport())) as handler:
        handler.emit(_record("one"))
        handler.flush()

        assert handler.dropped == 1


def test_emit_never_raises() -> None:
    """A record that cannot be formatted goes to ``handleError``, not the caller."""
    handler = ErrorRecordingHandler(
        OpensearchClient(FakeTransport(Success({}))), "logs", flush_interval=3600.0
    )
    bad_record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "%d", ("not a number",), None
    )
    with _running(handler):
        handler.emit(bad_record)

        assert len(handler.errored_records) == 1


def test_slow_send_does_not_block_emit_or_lose_records() -> None:
    """Emitting during a send does not block, and those records arrive afterwards."""
    transport = BlockingTransport()
    with _running(_handler(transport, buffer_size=1)) as handler:
        handler.emit(_record("a"))
        assert transport.send_started.wait(_WAIT_SECONDS), "the send never started"

        emitted = threading.Event()
        emitter = threading.Thread(
            target=_emit_then_signal, args=(handler, _record("b"), emitted)
        )
        emitter.start()
        emit_returned = emitted.wait(_WAIT_SECONDS)
        transport.release_send.set()
        emitter.join()
        handler.flush()

        assert emit_returned, "emit blocked while a send was in progress"
        assert transport.messages == ["a", "b"]


def test_args_and_exceptions_become_plain_json_documents() -> None:
    """Formatted args and exception text are indexed; raw args and exc_info are not."""
    transport = RecordingTransport()
    with _running(_handler(transport)) as handler:
        try:
            raise ValueError("bad value")
        except ValueError:
            exc_info = sys.exc_info()
        record = logging.LogRecord(
            "test", logging.ERROR, __file__, 1, "got %s and %d", ("text", 3), exc_info
        )

        handler.emit(record)
        handler.flush()

        assert handler.dropped == 0
        assert len(transport.documents) == 1
        document = transport.documents[0]
        assert document["message"] == "got text and 3"
        assert document["levelname"] == "ERROR"
        assert document["name"] == "test"
        assert "ValueError: bad value" in document["exc_text"]
        assert "args" not in document
        assert "exc_info" not in document


def test_buffered_records_are_flushed_on_the_interval() -> None:
    """A quiet handler still ships its records, without an explicit flush."""
    transport = RecordingTransport()
    handler = _handler(transport, buffer_size=1000, flush_interval=0.05)
    with _running(handler):
        handler.emit(_record("quiet"))

        assert transport.bulk_received.wait(_WAIT_SECONDS), "nothing was flushed"
    assert transport.documents[0]["message"] == "quiet"


def test_full_buffer_is_sent_without_waiting_for_the_interval() -> None:
    """Reaching buffer_size wakes the flush thread; emit itself does not send."""
    transport = RecordingTransport()
    with _running(_handler(transport, buffer_size=2)) as handler:
        handler.emit(_record("one"))
        handler.emit(_record("two"))

        assert transport.bulk_received.wait(_WAIT_SECONDS), "full buffer not sent"


def test_oldest_records_are_dropped_beyond_max_queue() -> None:
    """With a stalled sender the buffer stays bounded and loss is counted."""
    transport = RecordingTransport()
    with _running(_handler(transport, buffer_size=1000, max_queue=3)) as handler:
        for number in range(1, 6):
            handler.emit(_record(str(number)))
        assert handler.dropped == 2

        handler.flush()

        messages = [document["message"] for document in transport.documents]
        assert messages == ["3", "4", "5"]


def test_close_sends_remaining_records_and_stops_the_flush_thread() -> None:
    """Closing flushes what is queued and leaves no thread running."""
    transport = RecordingTransport()
    handler = _handler(transport, buffer_size=1000)
    handler.emit(_record("last"))

    handler.close()

    assert transport.documents[0]["message"] == "last"
    assert not handler._flush_thread.is_alive()


def test_close_never_raises() -> None:
    """A failing final flush is reported, not raised into the shutting-down app."""
    handler = _handler(RaisingTransport(), buffer_size=1000)
    handler.emit(_record("last"))

    handler.close()

    assert not handler._flush_thread.is_alive()


def test_flush_after_close_returns_immediately() -> None:
    """logging.shutdown flushes handlers that were already closed; it must not wait."""
    handler = _handler(RecordingTransport())
    handler.close()

    started = time.monotonic()
    handler.flush()

    assert time.monotonic() - started < _WAIT_SECONDS


def test_flush_thread_survives_a_failing_flush() -> None:
    """One failed flush must not stop later flushes."""
    transport = RaisingTransport()
    handler = _handler(transport, buffer_size=1000, flush_interval=0.05)
    with _running(handler):
        handler.emit(_record("one"))
        assert transport.bulk_attempted.wait(_WAIT_SECONDS), "no flush was attempted"
        transport.bulk_attempted.clear()

        handler.emit(_record("two"))

        assert transport.bulk_attempted.wait(_WAIT_SECONDS), "the thread stopped"
