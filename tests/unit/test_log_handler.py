# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for opensearch_client.log_handler.OpensearchHandler.

Batching, retries and request building belong to OpensearchClient.bulk and are
tested in test_client.py, and delivery to a real cluster is covered by the
functional tests. These tests cover what the handler adds on top.
"""

import contextlib
import json
import logging
import sys
import threading
import time
from collections.abc import Generator
from typing import Any

from opensearch_client.client import OpensearchClient
from opensearch_client.log_handler import OpensearchHandler
from opensearch_client.result import Failure, OpensearchResult, Success

# How long the tests wait on another thread before deciding it is stuck. Only
# reached when the code under test is broken.
_WAIT_SECONDS = 5


def _parse_bulk_documents(body: bytes | None) -> list[dict[str, Any]]:
    """Return the documents in a bulk body, skipping each document's action line."""
    lines = (body or b"").decode().splitlines()
    documents = []
    for position in range(1, len(lines), 2):
        documents.append(json.loads(lines[position]))
    return documents


class FakeCluster:
    """A transport that records what the handler sends and can misbehave on bulk.

    Attributes:
        calls (list[tuple[str, str]]): every (method, path) requested, in order.
        documents (list[dict[str, Any]]): the documents of each bulk request.
        bulk_received (threading.Event): set once a bulk request has been recorded.
    """

    def __init__(
        self,
        *,
        index_exists: bool = True,
        bulk_result: OpensearchResult[Any] | None = None,
        bulk_error: Exception | None = None,
        chatter: bool = False,
    ) -> None:
        """Create the fake.

        Args:
            index_exists (bool): whether the index is already there.
            bulk_result (OpensearchResult[Any] | None): the answer to bulk
                requests. Defaults to success.
            bulk_error (Exception | None): raised on every bulk request.
            chatter (bool): log a warning on the root logger during every bulk
                request, as real transports do.
        """
        self.index_exists = index_exists
        self.bulk_result = (
            Success({"items": []}) if bulk_result is None else bulk_result
        )
        self.bulk_error = bulk_error
        self.chatter = chatter
        self.calls: list[tuple[str, str]] = []
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
        self.calls.append((method, path))
        if path != "_bulk":
            if method == "GET":
                return Success({}) if self.index_exists else Failure("x", status=404)
            if method == "PUT":
                self.index_exists = True
            return Success({})

        if self.chatter:
            logging.warning("transport chatter")
        if self.bulk_error is not None:
            raise self.bulk_error
        self.documents.extend(_parse_bulk_documents(body))
        self.bulk_received.set()
        return self.bulk_result


class BlockingCluster(FakeCluster):
    """A FakeCluster that holds the first bulk request open until released."""

    def __init__(self) -> None:
        super().__init__()
        self.send_started = threading.Event()
        self.release_send = threading.Event()
        self.is_first_send = True

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: str = "application/json",
        timeout: int = 30,
    ) -> OpensearchResult[Any]:
        if path == "_bulk" and self.is_first_send:
            self.is_first_send = False
            self.send_started.set()
            # Outlasts the test's own wait, so a broken handler cannot hang it.
            if not self.release_send.wait(_WAIT_SECONDS * 2):
                raise RuntimeError("the send was never released")
        return super().request(method, path, body, content_type, timeout)


class ErrorRecordingHandler(OpensearchHandler):
    """Keeps the records passed to handleError instead of printing a traceback."""

    def __init__(self, client: OpensearchClient, index: str, **kwargs: Any) -> None:
        super().__init__(client, index, **kwargs)
        self.errored_records: list[logging.LogRecord] = []

    def handleError(self, record: logging.LogRecord) -> None:
        self.errored_records.append(record)


def _handler(cluster: FakeCluster, **kwargs: Any) -> OpensearchHandler:
    # An hour, so the timer never fires during a test unless the test sets it.
    kwargs.setdefault("flush_interval", 3600.0)
    return OpensearchHandler(OpensearchClient(cluster), "logs", **kwargs)


@contextlib.contextmanager
def _running(handler: OpensearchHandler) -> Generator[OpensearchHandler]:
    """Yield the handler, then close it so its flush thread stops."""
    try:
        yield handler
    finally:
        handler.close()


def _record(message: str) -> logging.LogRecord:
    return logging.LogRecord("test", logging.INFO, __file__, 1, message, (), None)


def _emit_all_then_signal(
    handler: OpensearchHandler,
    records: list[logging.LogRecord],
    emitted: threading.Event,
) -> None:
    for record in records:
        handler.emit(record)
    emitted.set()


def test_emit_queues_plain_documents_and_contains_bad_records() -> None:
    """Good records become plain documents; a bad one is reported and skipped."""
    cluster = FakeCluster()
    handler = ErrorRecordingHandler(
        OpensearchClient(cluster), "logs", flush_interval=3600.0
    )
    try:
        raise ValueError("bad value")
    except ValueError:
        exc_info = sys.exc_info()
    good = logging.LogRecord(
        "test", logging.ERROR, __file__, 1, "got %s and %d", ("text", 3), exc_info
    )
    bad = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "%d", ("not a number",), None
    )

    with _running(handler):
        handler.emit(bad)
        handler.emit(good)
        handler.flush()

        assert handler.errored_records == [bad]
        assert handler.dropped == 0
        assert len(cluster.documents) == 1
        document = cluster.documents[0]
        assert document["message"] == "got text and 3"
        assert document["levelname"] == "ERROR"
        assert document["name"] == "test"
        assert "ValueError: bad value" in document["exc_text"]
        assert "args" not in document
        assert "exc_info" not in document


def test_stalled_send_does_not_block_emit_and_the_queue_stays_bounded() -> None:
    """While a send hangs, emit returns, the oldest waiting record is dropped."""
    cluster = BlockingCluster()
    with _running(_handler(cluster, buffer_size=1, max_queue=2)) as handler:
        handler.emit(_record("a"))
        assert cluster.send_started.wait(_WAIT_SECONDS), "the send never started"

        emitted = threading.Event()
        records = [_record("b"), _record("c"), _record("d")]
        emitter = threading.Thread(
            target=_emit_all_then_signal, args=(handler, records, emitted)
        )
        emitter.start()
        emit_returned = emitted.wait(_WAIT_SECONDS)
        cluster.release_send.set()
        emitter.join()
        handler.flush()

        assert emit_returned, "emit blocked while a send was in progress"
        assert [document["message"] for document in cluster.documents] == [
            "a",
            "c",
            "d",
        ]
        assert handler.dropped == 1


def test_queued_records_are_sent_without_an_explicit_flush() -> None:
    """A full buffer and an elapsed interval each send the queue on their own."""
    full_buffer = {"buffer_size": 2, "flush_interval": 3600.0}
    interval_elapsed = {"buffer_size": 1000, "flush_interval": 0.05}

    for settings, record_count in ((full_buffer, 2), (interval_elapsed, 1)):
        cluster = FakeCluster()
        with _running(_handler(cluster, **settings)) as handler:
            for number in range(record_count):
                handler.emit(_record(str(number)))

            assert cluster.bulk_received.wait(_WAIT_SECONDS), f"not sent: {settings}"
            assert len(cluster.documents) == record_count


def test_send_path_records_are_ignored_and_a_locked_flush_does_not_stall() -> None:
    """Chatter from the send is never shipped, and logging.shutdown cannot stall."""
    cluster = FakeCluster(chatter=True)
    with _running(_handler(cluster)) as handler:
        root = logging.getLogger()
        previous_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        try:
            handler.emit(_record("hello"))
            handler.flush()
            handler.flush()  # would send the chatter, had it been queued
            messages = [document["message"] for document in cluster.documents]
            assert messages == ["hello"]

            handler.emit(_record("goodbye"))
            started = time.monotonic()
            handler.acquire()  # logging.shutdown flushes with this lock held
            try:
                handler.flush()
            finally:
                handler.release()
            elapsed = time.monotonic() - started
        finally:
            root.removeHandler(handler)
            root.setLevel(previous_level)

        assert elapsed < _WAIT_SECONDS, "flush stalled while the handler lock was held"


def test_index_is_created_only_when_missing_and_only_on_first_flush() -> None:
    """Building the handler makes no requests; the first flush checks the index."""
    index_missing = (
        False,
        [("GET", "logs"), ("PUT", "logs"), ("POST", "_bulk"), ("POST", "_bulk")],
    )
    index_present = (True, [("GET", "logs"), ("POST", "_bulk"), ("POST", "_bulk")])

    for index_exists, expected_calls in (index_missing, index_present):
        cluster = FakeCluster(index_exists=index_exists)
        with _running(_handler(cluster)) as handler:
            assert cluster.calls == []

            handler.emit(_record("one"))
            handler.flush()
            handler.emit(_record("two"))
            handler.flush()

            assert cluster.calls == expected_calls


def test_failed_send_is_retried_twice_then_counted_as_dropped() -> None:
    """A failing send is tried three times in all, and its records are dropped."""
    cluster = FakeCluster(bulk_result=Failure("down", status=500))
    with _running(_handler(cluster)) as handler:
        handler.emit(_record("one"))
        handler.emit(_record("two"))
        handler.flush()

        assert cluster.calls.count(("POST", "_bulk")) == 3
        assert handler.dropped == 2


def test_a_send_that_raises_is_contained() -> None:
    """Records are counted as dropped; the thread lives on; close does not raise."""
    cluster = FakeCluster(bulk_error=RuntimeError("boom"))
    handler = _handler(cluster)

    with _running(handler):
        handler.emit(_record("one"))
        handler.flush()
        assert handler.dropped == 1

        handler.emit(_record("two"))
        handler.flush()
        assert handler.dropped == 2  # the flush thread survived the first failure

        handler.emit(_record("three"))
    # Leaving the block closed the handler. Its final send raised, and that must
    # not have escaped.

    assert handler.dropped == 3
    assert not handler._flush_thread.is_alive()


def test_close_sends_the_remaining_records_and_stops_the_flush_thread() -> None:
    """Closing flushes what is queued; a later flush returns at once."""
    cluster = FakeCluster()
    handler = _handler(cluster)
    handler.emit(_record("last"))

    handler.close()

    assert [document["message"] for document in cluster.documents] == ["last"]
    assert not handler._flush_thread.is_alive()

    started = time.monotonic()
    handler.flush()  # logging.shutdown flushes handlers that were already closed
    assert time.monotonic() - started < _WAIT_SECONDS
