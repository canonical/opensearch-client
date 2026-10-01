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
import os
import platform
import socket
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

# How long to watch for activity that should not happen.
_QUIET_SECONDS = 0.3


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
        documents (list[dict[str, Any]]): the documents of every bulk request the
            cluster accepted, in order.
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
        accepted = isinstance(self.bulk_result, Success) and not (
            self.bulk_result.data.get("errors")
        )
        if accepted:
            self.documents.extend(_parse_bulk_documents(body))
        self.bulk_received.set()
        return self.bulk_result

    def recover(self) -> None:
        """Start accepting bulk requests."""
        self.bulk_result = Success({"items": []})


class BlockingCluster(FakeCluster):
    """A FakeCluster that holds the first bulk request open until released."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
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


def _item_failure(status: int, error_type: str) -> OpensearchResult[Any]:
    """A bulk response in which the only document failed with this error."""
    error = {"type": error_type}
    item = {"index": {"status": status, "error": error}}
    return Success({"errors": True, "items": [item]})


def _record(message: str, extra: dict[str, Any] | None = None) -> logging.LogRecord:
    """A record as a logger makes it, with ``extra=`` attributes if given."""
    logger = logging.getLogger("test")
    return logger.makeRecord(
        "test", logging.INFO, __file__, 1, message, (), None, None, extra
    )


def _emit_all_then_signal(
    handler: OpensearchHandler,
    records: list[logging.LogRecord],
    emitted: threading.Event,
) -> None:
    for record in records:
        handler.emit(record)
    emitted.set()


def test_emit_queues_ecs_documents_and_contains_bad_records() -> None:
    """An exception record becomes an ECS document; a bad one is reported."""
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
        assert document["log"]["level"] == "error"
        assert document["log"]["logger"] == "test"
        assert document["event"]["severity"] == logging.ERROR
        assert document["error"]["type"] == "ValueError"
        assert document["error"]["message"] == "bad value"
        assert "ValueError: bad value" in document["error"]["stack_trace"]
        assert "args" not in document
        assert "exc_info" not in document


def test_documents_carry_identity_labels_and_merged_extra_fields() -> None:
    """Shared identity, extras and extra_fields end up in every document."""
    cluster = FakeCluster()
    extra_fields = {"service": {"version": "1.2"}, "labels": {"env": "test"}}
    handler = _handler(cluster, service_name="superset", extra_fields=extra_fields)
    extra = {"collector": "superset", "batch.size": 3, "skipped": None}
    first = _record("one", extra)
    first.created = 1_700_000_000.123456
    second = _record("two")
    second.stack_info = "Stack (most recent call last):\n  File x, line 1"

    with _running(handler):
        handler.emit(first)
        handler.emit(second)
        handler.flush()

        first_document, second_document = cluster.documents
        assert first_document["@timestamp"] == "2023-11-14T22:13:20.123Z"
        assert first_document["ecs"] == {"version": "9.0"}
        assert first_document["host"] == {"name": socket.gethostname()}
        assert first_document["agent"]["type"] == "osclient"
        # extra_fields are merged into service, not a replacement for it.
        service = first_document["service"]
        assert service["name"] == "superset"
        assert service["version"] == "1.2"
        assert service["ephemeral_id"] == second_document["service"]["ephemeral_id"]
        assert first_document["event"] == {
            "dataset": "superset",
            "severity": logging.INFO,
            "sequence": 1,
        }
        assert second_document["event"]["sequence"] == 2
        assert first_document["labels"] == {
            "env": "test",
            "collector": "superset",
            "batch_size": "3",
        }
        assert first_document["python"] == {"msg": "one", "pathname": __file__}
        assert "error" not in first_document
        # stack_info alone is reported as a stack trace, with no exception type.
        assert second_document["error"] == {"stack_trace": second.stack_info}
        # Nothing from the first record carries over into the second.
        assert second_document["labels"] == {"env": "test"}


def test_system_details_are_opt_in_and_service_name_has_a_default() -> None:
    """OS and process details are added only on request."""
    plain_cluster = FakeCluster()
    detailed_cluster = FakeCluster()
    plain = _handler(plain_cluster)
    detailed = _handler(detailed_cluster, include_system_details=True)

    with _running(plain), _running(detailed):
        plain.emit(_record("one"))
        plain.flush()
        detailed.emit(_record("one"))
        detailed.flush()

    plain_document = plain_cluster.documents[0]
    detailed_document = detailed_cluster.documents[0]
    # With no service_name the name comes from the running program.
    assert plain_document["service"]["name"]
    assert plain_document["event"]["dataset"] == plain_document["service"]["name"]
    assert set(plain_document["process"]) == {"pid", "name", "thread"}
    assert set(plain_document["host"]) == {"name"}
    assert detailed_document["host"]["architecture"] == platform.machine()
    assert detailed_document["host"]["os"]["name"]
    assert detailed_document["process"]["executable"] == sys.executable
    assert detailed_document["process"]["parent"] == {"pid": os.getppid()}
    assert detailed_document["process"]["working_directory"] == os.getcwd()


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
        # The dropped record "b" was number 2, so the gap shows the loss.
        assert [d["event"]["sequence"] for d in cluster.documents] == [1, 3, 4]
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


def test_failed_sends_are_kept_and_resent_until_the_handler_closes() -> None:
    """An outage loses nothing until close, and nothing retries in a tight loop."""
    cluster = FakeCluster(bulk_result=Failure("down", status=None))
    with _running(_handler(cluster, buffer_size=2)) as handler:
        handler.emit(_record("a"))
        handler.flush()

        # The first attempt and bulk's two retries.
        assert cluster.calls.count(("POST", "_bulk")) == 3
        assert handler.dropped == 0

        cluster.bulk_received.clear()
        handler.emit(_record("b"))  # fills the buffer, but the handler backs off
        assert not cluster.bulk_received.wait(_QUIET_SECONDS), "retrying in a loop"

        cluster.recover()
        handler.flush()
        assert [document["message"] for document in cluster.documents] == ["a", "b"]
        assert handler.dropped == 0

        cluster.bulk_result = Failure("down", status=None)
        handler.emit(_record("c"))
        handler.close()
        assert handler.dropped == 1  # closing gives up on what could not be sent


def test_records_kept_after_a_failed_send_respect_max_queue_oldest_first() -> None:
    """Kept records go back in front, and the oldest are dropped past max_queue."""
    cluster = BlockingCluster(bulk_result=Failure("down", status=None))
    with _running(_handler(cluster, buffer_size=3, max_queue=3)) as handler:
        for name in ("a", "b", "c"):
            handler.emit(_record(name))  # the third fills the buffer; its send blocks
        assert cluster.send_started.wait(_WAIT_SECONDS), "the send never started"
        handler.emit(_record("d"))
        handler.emit(_record("e"))

        cluster.release_send.set()  # the blocked send now fails
        handler.flush()
        assert handler.dropped == 2

        cluster.recover()
        handler.flush()
        assert [document["message"] for document in cluster.documents] == [
            "c",
            "d",
            "e",
        ]
        assert handler.dropped == 2


def test_temporary_failures_are_kept_and_rejected_documents_are_dropped() -> None:
    """Only a failure that can clear up later is worth sending again."""
    cases = (
        (503, "unavailable_shards_exception", True),
        (429, "es_rejected_execution_exception", True),
        (403, "cluster_block_exception", True),
        (404, "index_not_found_exception", True),
        (400, "mapper_parsing_exception", False),
        (403, "security_exception", False),
    )

    for status, error_type, is_kept in cases:
        cluster = FakeCluster(bulk_result=_item_failure(status, error_type))
        with _running(_handler(cluster)) as handler:
            handler.emit(_record("one"))
            handler.flush()
            cluster.recover()
            handler.flush()

            expected = (1, 0) if is_kept else (0, 1)  # (delivered, dropped)
            assert (len(cluster.documents), handler.dropped) == expected, (
                f"{status} {error_type}"
            )


def test_index_deleted_while_running_is_recreated() -> None:
    """After a missing-index failure the index is created before the next send."""
    cluster = FakeCluster()
    with _running(_handler(cluster)) as handler:
        handler.emit(_record("one"))
        handler.flush()

        cluster.index_exists = False  # the index is deleted
        cluster.bulk_result = _item_failure(404, "index_not_found_exception")
        cluster.calls.clear()
        handler.emit(_record("two"))
        handler.flush()
        cluster.recover()
        handler.flush()

        assert cluster.calls[-3:] == [
            ("GET", "logs"),
            ("PUT", "logs"),
            ("POST", "_bulk"),
        ]
        assert [document["message"] for document in cluster.documents] == [
            "one",
            "two",
        ]


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
