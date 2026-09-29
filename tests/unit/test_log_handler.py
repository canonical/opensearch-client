# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for opensearch_client.log_handler.OpensearchHandler."""

import json
import logging
import sys
import threading
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
    """Keeps every document sent in a bulk request."""

    def __init__(self) -> None:
        self.documents: list[dict[str, Any]] = []

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


class ErrorRecordingHandler(OpensearchHandler):
    """Keeps the records passed to handleError instead of printing a traceback."""

    def __init__(self, client: OpensearchClient, index: str, **kwargs: Any) -> None:
        super().__init__(client, index, **kwargs)
        self.errored_records: list[logging.LogRecord] = []

    def handleError(self, record: logging.LogRecord) -> None:
        self.errored_records.append(record)


def _handler(transport: Any, **kwargs: Any) -> OpensearchHandler:
    return OpensearchHandler(OpensearchClient(transport), "logs", **kwargs)


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
    handler = _handler(transport, buffer_size=1)
    assert transport.calls == []

    handler.emit(_record("one"))
    handler.emit(_record("two"))

    assert transport.calls == [
        ("GET", "logs"),
        ("PUT", "logs"),
        ("POST", "_bulk"),
        ("POST", "_bulk"),
    ]


def test_records_logged_while_sending_are_not_shipped() -> None:
    """On the root logger, the handler ignores what its own transport logs."""
    transport = ChattyTransport()
    handler = _handler(transport, buffer_size=1)
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        logging.getLogger("app").info("hello")
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)

    assert len(transport.bulk_bodies) == 1
    assert b"hello" in transport.bulk_bodies[0]
    assert b"chatter" not in transport.bulk_bodies[0]


def test_failed_send_is_counted_as_dropped() -> None:
    """A failed send does not raise; the lost records show up in ``dropped``."""
    handler = _handler(FakeTransport(Failure("down", status=500)))

    handler.emit(_record("one"))
    handler.emit(_record("two"))
    handler.flush()

    assert handler.dropped == 2


def test_emit_never_raises() -> None:
    """An error while shipping goes to ``handleError``, not to the caller."""
    handler = ErrorRecordingHandler(
        OpensearchClient(FakeTransport(Success({}), error=RuntimeError("boom"))),
        "logs",
        buffer_size=1,
    )

    handler.emit(_record("one"))

    assert len(handler.errored_records) == 1


def test_slow_send_neither_blocks_other_threads_nor_loses_records() -> None:
    """Records emitted during a send are not blocked, and arrive in the next flush."""
    transport = BlockingTransport()
    handler = _handler(transport, buffer_size=2)
    handler.emit(_record("a"))
    # Emitting "b" fills the buffer, so this thread's flush blocks inside the send.
    first_sender = threading.Thread(target=handler.emit, args=(_record("b"),))
    first_sender.start()
    assert transport.send_started.wait(_WAIT_SECONDS), "the send never started"

    emitted = threading.Event()
    other_sender = threading.Thread(
        target=_emit_then_signal, args=(handler, _record("c"), emitted)
    )
    other_sender.start()
    emit_returned = emitted.wait(_WAIT_SECONDS)
    transport.release_send.set()
    first_sender.join()
    other_sender.join()
    handler.flush()

    assert emit_returned, "emit blocked while another thread was sending"
    assert transport.messages == ["a", "b", "c"]


def test_args_and_exceptions_become_plain_json_documents() -> None:
    """Formatted args and exception text are indexed; raw args and exc_info are not."""
    transport = RecordingTransport()
    handler = _handler(transport, buffer_size=1)
    try:
        raise ValueError("bad value")
    except ValueError:
        exc_info = sys.exc_info()
    record = logging.LogRecord(
        "test", logging.ERROR, __file__, 1, "got %s and %d", ("text", 3), exc_info
    )

    handler.emit(record)

    assert handler.dropped == 0
    assert len(transport.documents) == 1
    document = transport.documents[0]
    assert document["message"] == "got text and 3"
    assert document["levelname"] == "ERROR"
    assert document["name"] == "test"
    assert "ValueError: bad value" in document["exc_text"]
    assert "args" not in document
    assert "exc_info" not in document
