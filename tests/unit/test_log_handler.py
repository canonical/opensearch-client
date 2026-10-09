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
from unittest.mock import MagicMock

import pytest
from helpers import RecordingTransport

from opensearch_client.client import BULK_MAX_BYTES, OpensearchClient
from opensearch_client.log_handler import (
    ECSLogContextFilter,
    ECSLogFormatter,
    OpensearchLogHandler,
    _Waitlist,
)
from opensearch_client.result import Failure, OpensearchResult, Success
from opensearch_client.transport import REQUEST_TIMEOUT

# How long the tests wait on another thread before deciding it is stuck. Only
# reached when the code under test is broken.
_WAIT_SECONDS = 5

_CLUSTER_DOWN = {"status": None, "reason": "connection refused"}


class FakeClient(OpensearchClient):
    """An OpensearchClient whose two calls the handler makes are answered here.

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
        super().__init__(RecordingTransport())
        self.index_present = index_present
        self.failure = failure
        self.bulk_error = bulk_error
        self.chatter = chatter
        self.send_delay = send_delay
        self.calls: list[tuple[str, str | None]] = []
        self.documents: list[dict[str, Any]] = []
        self.send_started = threading.Event()
        self.bulk_received = threading.Event()

    def ensure_index(
        self, index: str | None = None, body: dict[str, Any] | None = None
    ) -> OpensearchResult[bool]:
        del body  # the handler creates the index with default settings
        self.calls.append(("ensure_index", index))
        created = not self.index_present
        self.index_present = True
        return Success(created)

    def bulk(
        self,
        documents: Iterable[dict[str, Any]],
        index: str | None = None,
        *,
        action: str = "index",
        max_bytes: int = BULK_MAX_BYTES,
        max_retries: int = 3,
    ) -> OpensearchResult[dict[str, Any]]:
        # Batching and retrying are the real client's job; see test_client.py.
        del action, max_bytes, max_retries
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


def test_extra_fields_is_copied() -> None:
    """The filter keeps its own copy of extra_fields."""
    extra_fields = {"labels": {"env": "prod"}}
    context_filter = ECSLogContextFilter(extra_fields=extra_fields)
    extra_fields["labels"]["env"] = "changed"
    record = _record("one")

    context_filter.filter(record)

    document = ECSLogFormatter().format_document(record)
    assert document["labels"] == {"env": "prod"}


def test_shipped_records_carry_the_context_and_a_bad_record_is_reported() -> None:
    """With the ECS formatter and filter set, documents name the service.

    A record that cannot be formatted goes to handleError, and is not shipped.
    """
    client = FakeClient()
    handler = ErrorRecordingHandler(client, "logs", flush_interval=3600.0)
    handler.setFormatter(ECSLogFormatter())
    handler.addFilter(ECSLogContextFilter())
    bad = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "%d", ("not a number",), None
    )

    with _running(handler):
        # handle, unlike emit, runs the handler's filters, as logging does.
        handler.handle(bad)
        handler.handle(_record("good"))
        handler.flush()

        assert handler.errored_records == [bad]
        [document] = client.documents
        assert document["service"]["name"]
        assert ("bulk", "logs") in client.calls


def test_the_document_comes_from_format_document_then_json_then_raw_text() -> None:
    """A formatter's format_document is used if it has one.

    Otherwise the formatted text is the document if it is a JSON object, and
    failing that it is wrapped under ``message``.
    """
    document_formatter = MagicMock(
        spec=["format_document"],
        format_document=MagicMock(return_value={"message": "from a dict"}),
    )
    json_formatter = MagicMock(
        spec=["format"], format=MagicMock(return_value='{"message": "from JSON"}')
    )
    text_formatter = MagicMock(
        spec=["format"], format=MagicMock(return_value="plain text")
    )
    client = FakeClient()

    with _running(_handler(client)) as handler:
        handler.setFormatter(document_formatter)
        handler.emit(_record("one"))
        handler.setFormatter(json_formatter)
        handler.emit(_record("two"))
        handler.setFormatter(text_formatter)
        handler.emit(_record("three"))
        handler.flush()

    from_dict, from_json, from_text = client.documents
    assert from_dict == {"message": "from a dict"}
    assert from_json == {"message": "from JSON"}
    assert from_text["message"] == "plain text"
    assert "@timestamp" in from_text


def test_a_format_document_that_returns_no_dict_falls_back_to_the_text() -> None:
    """A non-dict from format_document is not shipped."""
    formatter = MagicMock(
        spec=["format_document", "format"],
        format_document=MagicMock(return_value=["not", "a", "dict"]),
        format=MagicMock(return_value="plain text"),
    )
    client = FakeClient()

    with _running(_handler(client)) as handler:
        handler.setFormatter(formatter)
        handler.emit(_record("one"))
        handler.flush()

    [document] = client.documents
    assert document["message"] == "plain text"
    assert "@timestamp" in document


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


def test_a_failed_index_check_is_not_repeated_on_every_send() -> None:
    """An account that may write but not read index metadata is checked once."""
    client = MagicMock(
        ensure_index=MagicMock(return_value=Failure("forbidden", status=403)),
        bulk=MagicMock(return_value=Success({"failures": []})),
    )
    handler = OpensearchLogHandler(client, "logs", flush_interval=3600.0)

    with _running(handler):
        handler.emit(_record("one"))
        handler.flush()
        handler.emit(_record("two"))
        handler.flush()

    assert client.ensure_index.call_count == 1
    assert client.bulk.call_count == 2


def test_records_survive_an_outage_without_hammering_the_cluster() -> None:
    """While the cluster is down, records are kept and resent once it is back.

    Records kept after a failed send leave the buffer full, and new records must
    not each trigger a resend to the cluster that just failed: the next attempt
    waits for the interval or a flush.
    """
    client = FakeClient(failure=_CLUSTER_DOWN)
    with _running(_handler(client, flush_threshold=2)) as handler:
        handler.emit(_record("a"))
        handler.emit(_record("b"))  # reaches flush_threshold
        handler.flush()  # the cluster is down: "a" and "b" are kept

        client.send_started.clear()
        for name in ("c", "d", "e"):
            handler.emit(_record(name))
        assert not client.send_started.wait(0.3), "resent at once while down"

        client.recover()
        handler.flush()
        assert _messages(client) == ["a", "b", "c", "d", "e"]


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

        client.recover()
        handler.flush()
        assert _messages(client) == ["c", "d", "e"]


def test_temporary_failures_are_kept_and_rejected_documents_are_dropped() -> None:
    """Only a failure that can clear up later is worth sending again.

    The status alone does not decide: a 403 can be a disk-full block that clears,
    or a permission the account will never have.
    """
    cases = (
        (503, "unavailable_shards_exception", True),
        (429, "es_rejected_execution_exception", True),
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

        delivered = 1 if is_kept else 0
        assert len(client.documents) == delivered, f"{status} {error_type}"


def test_a_send_that_raises_is_contained() -> None:
    """Its records are lost, but the thread lives on and close does not raise."""
    client = FakeClient(bulk_error=RuntimeError("boom"))

    with _running(_handler(client)) as handler:
        handler.emit(_record("one"))
        handler.flush()

        client.bulk_error = None
        handler.emit(_record("two"))
        handler.flush()
        assert _messages(client) == ["two"]  # the thread survived the failure

        client.bulk_error = RuntimeError("boom")
        handler.emit(_record("three"))
    # Leaving the block closed the handler. Its final send raised, and that must
    # not have escaped.


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

    A record emitted later is not queued, and a later flush returns at once, as
    logging.shutdown needs.
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
    assert _messages(client) == ["last"]


def test_closing_a_waitlist_releases_its_signals() -> None:
    """After close, nobody waiting on the waitlist can be left waiting."""
    waitlist = _Waitlist()
    registered_before = waitlist.register()

    waitlist.close()
    registered_after = waitlist.register()

    assert registered_before.is_set()
    assert registered_after.is_set()
    assert waitlist.take_all() == []
