# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for osclient.logging.OpensearchHandler."""

import logging
from typing import Any

import pytest

from osclient.client import OpensearchClient
from osclient.logging import OpensearchHandler
from osclient.result import Failure, OpensearchResult, Success


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


def _handler(transport: Any, **kwargs: Any) -> OpensearchHandler:
    return OpensearchHandler(OpensearchClient(transport), "logs", **kwargs)


def _record(message: str) -> logging.LogRecord:
    return logging.LogRecord("test", logging.INFO, __file__, 1, message, (), None)


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


def test_rejected_documents_are_counted_as_dropped() -> None:
    """A failed send does not raise; the lost records show up in ``dropped``."""
    handler = _handler(FakeTransport(Failure("down", status=500)))

    handler.emit(_record("one"))
    handler.emit(_record("two"))
    handler.flush()

    assert handler.dropped == 2


def test_emit_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """An error while shipping goes to ``handleError``, not to the caller."""
    handler = _handler(
        FakeTransport(Success({}), error=RuntimeError("boom")), buffer_size=1
    )
    handled: list[logging.LogRecord] = []
    monkeypatch.setattr(handler, "handleError", handled.append)

    handler.emit(_record("one"))

    assert len(handled) == 1
