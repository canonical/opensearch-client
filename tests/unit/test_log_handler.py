# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for opensearch_client.log_handler.OpensearchLogHandler.

Batching, retries and request building belong to OpensearchClient.bulk and are
tested in test_client.py, and delivery to a real cluster is covered by the
functional tests. These tests cover what the handler adds on top.
"""

import contextlib
import logging
import logging.handlers
import sys
import threading
import time
from collections.abc import Generator, Iterable
from datetime import datetime, timezone
from typing import Any

import pytest

from opensearch_client.log_handler import (
    ECSLogContextFilter,
    ECSLogFormatter,
    OpensearchLogHandler,
)
from opensearch_client.result import Failure, OpensearchResult, Success

# How long the tests wait on another thread before deciding it is stuck. Only
# reached when the code under test is broken.
_WAIT_SECONDS = 5

_CLUSTER_DOWN = {"status": None, "reason": "connection refused"}


class FakeClient:
    """Stands in for OpensearchClient: answers the two calls the handler makes.

    Like a cluster that does not create indexes on its own, it rejects every
    document while the index does not exist.

    Attributes:
        calls (list[tuple[str, str | None]]): every call and the index it named,
            in order.
        documents (list[dict[str, Any]]): every document accepted, in order.
        send_started (threading.Event): set when a bulk call starts.
        bulk_received (threading.Event): set when a bulk call has been answered.
    """

    def __init__(
        self,
        *,
        index_present: bool = True,
        failure: dict[str, Any] | None = None,
        bulk_error: Exception | None = None,
        chatter: bool = False,
        send_delay: float = 0.0,
    ) -> None:
        """Create the fake.

        Args:
            index_present (bool): whether the index is already there.
            failure (dict[str, Any] | None): how every document of a bulk call
                fails, as bulk reports it (``status``, and ``error`` or
                ``reason``), or None to accept them.
            bulk_error (Exception | None): raised by every bulk call.
            chatter (bool): log a warning during every bulk call, as the
                libraries under a real client do.
            send_delay (float): how long every bulk call takes, in seconds.
        """
        self.index_present = index_present
        self.failure = failure
        self.bulk_error = bulk_error
        self.chatter = chatter
        self.send_delay = send_delay
        self.calls: list[tuple[str, str | None]] = []
        self.documents: list[dict[str, Any]] = []
        self.send_started = threading.Event()
        self.bulk_received = threading.Event()

    def ensure_index(self, index: str | None = None) -> OpensearchResult[bool]:
        self.calls.append(("ensure_index", index))
        created = not self.index_present
        self.index_present = True
        return Success(created)

    def bulk(
        self,
        documents: Iterable[dict[str, Any]],
        index: str | None = None,
        *,
        max_retries: int = 3,
    ) -> OpensearchResult[dict[str, Any]]:
        del max_retries  # retrying is the real client's job; see test_client.py
        self.calls.append(("bulk", index))
        self.send_started.set()
        time.sleep(self.send_delay)
        if self.chatter:
            logging.warning("client chatter")
        if self.bulk_error is not None:
            raise self.bulk_error
        sent = list(documents)
        failure = self.failure
        if not self.index_present:
            failure = {"status": 404, "error": {"type": "index_not_found_exception"}}
        if failure is None:
            self.documents.extend(sent)
            self.bulk_received.set()
            return Success({"failures": []})
        self.bulk_received.set()
        failures = [{**failure, "document": document} for document in sent]
        return Failure("documents failed to index", data={"failures": failures})

    def recover(self) -> None:
        """Start accepting documents."""
        self.failure = None


class ErrorRecordingHandler(OpensearchLogHandler):
    """Keeps the records passed to handleError instead of printing a traceback."""

    def __init__(self, client: FakeClient, index: str, **kwargs: Any) -> None:
        super().__init__(client, index, **kwargs)
        self.errored_records: list[logging.LogRecord] = []

    def handleError(self, record: logging.LogRecord) -> None:
        self.errored_records.append(record)


def _handler(client: FakeClient, **kwargs: Any) -> OpensearchLogHandler:
    # An hour, so the timer never fires during a test unless the test sets it.
    kwargs.setdefault("flush_interval", 3600.0)
    return OpensearchLogHandler(client, "logs", **kwargs)


@contextlib.contextmanager
def _running(handler: OpensearchLogHandler) -> Generator[OpensearchLogHandler]:
    """Yield the handler, then close it so its flush thread stops."""
    try:
        yield handler
    finally:
        handler.close()


def _record(message: str, extra: dict[str, Any] | None = None) -> logging.LogRecord:
    """A record as a logger makes it, with ``extra=`` attributes if given."""
    logger = logging.getLogger("test")
    return logger.makeRecord(
        "test", logging.INFO, __file__, 1, message, (), None, None, extra
    )


def _messages(client: FakeClient) -> list[str]:
    return [document["message"] for document in client.documents]


def test_a_document_keeps_the_traceback_and_the_time_in_utc() -> None:
    """An exception's traceback survives, and the time is the same instant in UTC."""
    try:
        raise ValueError("bad value")
    except ValueError:
        exc_info = sys.exc_info()
    record = logging.LogRecord(
        "test", logging.ERROR, __file__, 1, "lookup failed", (), exc_info
    )
    record.created = 1_700_000_000.5

    document = ECSLogFormatter().format_document(record)

    assert "ValueError: bad value" in document["error"]["stack_trace"]
    sent_at = document["@timestamp"].replace("Z", "+00:00")
    assert datetime.fromisoformat(sent_at) == datetime.fromtimestamp(
        record.created, timezone.utc
    )
    # Standard record attributes are not mistaken for extra= values.
    assert "labels" not in document


def test_context_reaches_every_document_and_extra_values_do_not_leak() -> None:
    """The context adds to the defaults and wins over them, in every document.

    One record's extra= values never reach the next record.
    """
    extra_fields = {
        "service": {"version": "1.2"},
        "event": {"dataset": "custom-dataset"},
        "labels": {"env": "test"},
    }
    context_filter = ECSLogContextFilter(
        service_name="superset", extra_fields=extra_fields
    )
    formatter = ECSLogFormatter()

    documents = []
    for record in (_record("one", {"collector": "superset"}), _record("two")):
        context_filter.filter(record)
        documents.append(formatter.format_document(record))

    first, second = documents
    for document in (first, second):
        assert document["service"]["name"] == "superset"
        assert document["service"]["version"] == "1.2"
        assert document["event"]["dataset"] == "custom-dataset"
    assert first["labels"] == {"env": "test", "collector": "superset"}
    assert second["labels"] == {"env": "test"}


def test_shipped_records_carry_the_context_and_a_bad_record_is_reported() -> None:
    """With no setup, documents name the service and go to the handler's index.

    A record that cannot be formatted goes to handleError, and is neither shipped
    nor counted as dropped.
    """
    client = FakeClient()
    handler = ErrorRecordingHandler(client, "logs", flush_interval=3600.0)
    bad = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "%d", ("not a number",), None
    )

    with _running(handler):
        # handle, unlike emit, runs the handler's filters, as logging does.
        handler.handle(bad)
        handler.handle(_record("good"))
        handler.flush()

        assert handler.errored_records == [bad]
        assert handler.dropped == 0
        [document] = client.documents
        assert document["service"]["name"]
        assert ("bulk", "logs") in client.calls


def test_a_slow_send_does_not_block_emit_and_the_queue_stays_bounded() -> None:
    """While a send runs, emit returns at once; a full buffer drops its oldest."""
    client = FakeClient(send_delay=0.3)
    with _running(_handler(client, flush_threshold=1, buffer_limit=2)) as handler:
        handler.emit(_record("a"))
        assert client.send_started.wait(_WAIT_SECONDS), "the send never started"

        started = time.monotonic()
        for name in ("b", "c", "d"):
            handler.emit(_record(name))
        emit_seconds = time.monotonic() - started
        handler.flush()

        assert emit_seconds < 0.15, "emit waited for the 0.3 s send"
        assert _messages(client) == ["a", "c", "d"]
        assert handler.dropped == 1


def test_queued_records_are_sent_without_an_explicit_flush() -> None:
    """A full buffer and an elapsed interval each send the queue on their own."""
    full_buffer = {"flush_threshold": 2, "flush_interval": 3600.0}
    interval_elapsed = {"flush_threshold": 1000, "flush_interval": 0.05}

    for settings, record_count in ((full_buffer, 2), (interval_elapsed, 1)):
        client = FakeClient()
        with _running(_handler(client, **settings)) as handler:
            for number in range(record_count):
                handler.emit(_record(str(number)))

            assert client.bulk_received.wait(_WAIT_SECONDS), f"not sent: {settings}"
            assert len(client.documents) == record_count


def test_flush_waits_for_a_send_that_holds_the_records_queued_before_it() -> None:
    """flush returns only once the records queued before it are sent.

    A send may already be running when flush is called, holding only older records.
    """
    client = FakeClient(send_delay=0.3)
    with _running(_handler(client, flush_threshold=1)) as handler:
        handler.emit(_record("a"))
        assert client.send_started.wait(_WAIT_SECONDS), "the send never started"
        handler.emit(_record("b"))  # queued while "a" is being sent

        handler.flush()

        assert _messages(client) == ["a", "b"]


def test_flush_does_not_stall_while_the_handler_lock_is_held() -> None:
    """logging.shutdown flushes with the handler lock held; that must not hang."""
    with _running(_handler(FakeClient())) as handler:
        handler.emit(_record("goodbye"))
        started = time.monotonic()
        handler.acquire()
        try:
            handler.flush()
        finally:
            handler.release()

        assert time.monotonic() - started < _WAIT_SECONDS


def test_records_logged_while_sending_are_not_shipped() -> None:
    """What the client logs while sending never feeds back into the queue."""
    client = FakeClient(chatter=True)
    root = logging.getLogger()
    previous_level = root.level
    with _running(_handler(client)) as handler:
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        try:
            handler.emit(_record("hello"))
            handler.flush()
            handler.flush()  # would send the chatter, had it been queued
        finally:
            root.removeHandler(handler)
            root.setLevel(previous_level)

        assert _messages(client) == ["hello"]


def test_records_reach_an_index_that_is_missing_or_deleted() -> None:
    """The handler creates a missing index, and creates it again if it is deleted.

    Building the handler sends nothing, so it cannot slow a program's start.
    """
    client = FakeClient(index_present=False)
    with _running(_handler(client)) as handler:
        assert client.calls == []

        handler.emit(_record("one"))
        handler.flush()

        client.index_present = False  # deleted while the handler runs
        handler.emit(_record("two"))
        handler.flush()  # rejected: the index is gone
        handler.flush()  # creates the index again and resends

        assert _messages(client) == ["one", "two"]
        assert handler.dropped == 0


def test_records_survive_an_outage_without_hammering_the_cluster() -> None:
    """While the cluster is down, records are kept and resent once it is back.

    Records kept after a failed send leave the buffer full, and new records must
    not each trigger a resend to the cluster that just failed: the next attempt
    waits for the interval or a flush. Records still unsent at close are counted
    as dropped.
    """
    client = FakeClient(failure=_CLUSTER_DOWN)
    with _running(_handler(client, flush_threshold=2)) as handler:
        handler.emit(_record("a"))
        handler.emit(_record("b"))  # reaches flush_threshold
        handler.flush()  # the cluster is down: "a" and "b" are kept
        assert handler.dropped == 0

        client.send_started.clear()
        for name in ("c", "d", "e"):
            handler.emit(_record(name))
        assert not client.send_started.wait(0.3), "resent at once while down"

        client.recover()
        handler.flush()
        assert _messages(client) == ["a", "b", "c", "d", "e"]

        client.failure = _CLUSTER_DOWN
        handler.emit(_record("f"))
        handler.close()
        assert handler.dropped == 1


def test_records_kept_after_a_failed_send_respect_buffer_limit_oldest_first() -> None:
    """Kept records go back in front, and the oldest are dropped past buffer_limit."""
    client = FakeClient(failure=_CLUSTER_DOWN, send_delay=0.3)
    with _running(_handler(client, flush_threshold=3, buffer_limit=3)) as handler:
        for name in ("a", "b", "c"):
            handler.emit(_record(name))  # the third fills the buffer and is sent
        assert client.send_started.wait(_WAIT_SECONDS), "the send never started"
        handler.emit(_record("d"))  # queued while the send is failing
        handler.emit(_record("e"))
        handler.flush()
        assert handler.dropped == 2

        client.recover()
        handler.flush()
        assert _messages(client) == ["c", "d", "e"]
        assert handler.dropped == 2


def test_temporary_failures_are_kept_and_rejected_documents_are_dropped() -> None:
    """Only a failure that can clear up later is worth sending again.

    The status alone does not decide: a 403 can be a disk-full block that clears,
    or a permission the account will never have.
    """
    cases = (
        (503, "unavailable_shards_exception", True),
        (403, "cluster_block_exception", True),
        (400, "mapper_parsing_exception", False),
        (403, "security_exception", False),
    )

    for status, error_type, is_kept in cases:
        client = FakeClient(failure={"status": status, "error": {"type": error_type}})
        with _running(_handler(client)) as handler:
            handler.emit(_record("one"))
            handler.flush()
            client.recover()
            handler.flush()

            expected = (1, 0) if is_kept else (0, 1)  # (delivered, dropped)
            assert (len(client.documents), handler.dropped) == expected, (
                f"{status} {error_type}"
            )


def test_a_send_that_raises_is_contained_and_reported() -> None:
    """Records are counted as dropped; the thread lives on; close does not raise.

    Each failed send is reported through logging, with its traceback.
    """
    client = FakeClient(bulk_error=RuntimeError("boom"))
    handler = _handler(client)
    reports = logging.handlers.BufferingHandler(capacity=100)
    handler_logger = logging.getLogger("opensearch_client.log_handler")
    handler_logger.addHandler(reports)
    try:
        with _running(handler):
            handler.emit(_record("one"))
            handler.flush()
            assert handler.dropped == 1

            handler.emit(_record("two"))
            handler.flush()
            assert handler.dropped == 2  # the flush thread survived the first failure

            handler.emit(_record("three"))
        # Leaving the block closed the handler. Its final send raised, and that
        # must not have escaped.
    finally:
        handler_logger.removeHandler(reports)

    assert handler.dropped == 3
    assert len(reports.buffer) == 3
    assert all(report.exc_info for report in reports.buffer)


def test_emit_lets_recursion_error_through() -> None:
    """Like standard library handlers, emit re-raises RecursionError."""

    class RecursiveStr:
        def __str__(self) -> str:
            raise RecursionError

    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "%s", (RecursiveStr(),), None
    )

    with _running(_handler(FakeClient())) as handler:
        with pytest.raises(RecursionError):
            handler.emit(record)


def test_close_sends_the_remaining_records_and_drops_later_ones() -> None:
    """Closing sends what is queued and stops the flush thread.

    A record emitted later is counted as dropped, and a later flush returns at
    once, as logging.shutdown needs.
    """
    client = FakeClient()
    handler = _handler(client)
    handler.emit(_record("last"))

    handler.close()

    assert _messages(client) == ["last"]
    assert not handler._flush_thread.is_alive()

    handler.emit(_record("late"))
    started = time.monotonic()
    handler.flush()  # logging.shutdown flushes handlers that were already closed
    assert time.monotonic() - started < _WAIT_SECONDS
    assert handler.dropped == 1
    assert _messages(client) == ["last"]
