# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Functional tests for opensearch_client against a live OpenSearch cluster.

These require a running OpenSearch reachable via the ``OPENSEARCH_*`` environment
variables (``OPENSEARCH_URL``, ``OPENSEARCH_USER``, ``OPENSEARCH_PASSWORD``); when
any is unset the whole module is skipped, so a plain ``tox`` run without a cluster
stays green. ``tests/functional/run.sh`` starts an ephemeral Docker cluster, sets
the variables, runs these tests, and tears everything down.
"""

import contextlib
import os
import secrets
import uuid
from typing import Iterator

import pytest

from opensearch_client import OpensearchClient, security, triage
from opensearch_client.config import client_from_env
from opensearch_client.transport import DirectTransport

if not (
    os.environ.get("OPENSEARCH_URL")
    and os.environ.get("OPENSEARCH_USER")
    and os.environ.get("OPENSEARCH_PASSWORD")
):
    pytest.skip(
        "OPENSEARCH_URL, OPENSEARCH_USER, and OPENSEARCH_PASSWORD must be set; "
        "no OpenSearch cluster available",
        allow_module_level=True,
    )


def _build_client() -> OpensearchClient:
    """Build the client, asserting it is configured (the skip guard ran above).

    The non-optional return type narrows ``_client`` to ``OpensearchClient`` for
    every test function, unlike a module-level assert which narrows only here.
    """
    client = client_from_env()
    assert client is not None, "OPENSEARCH_* is set but client_from_env returned None"
    return client


_client = _build_client()


@contextlib.contextmanager
def _temporary_indices(*names: str) -> Iterator[None]:
    """Delete the named indices on exit, whether the test passes or fails."""
    try:
        yield
    finally:
        for name in names:
            _client.request("DELETE", name)


@contextlib.contextmanager
def _temporary_template(name: str) -> Iterator[None]:
    """Delete the named index template on exit, whether the test passes or fails."""
    try:
        yield
    finally:
        _client.request("DELETE", f"_index_template/{name}")


@contextlib.contextmanager
def _temporary_component_template(name: str) -> Iterator[None]:
    """Delete the named component template on exit, whether the test passes or fails."""
    try:
        yield
    finally:
        _client.request("DELETE", f"_component_template/{name}")


@contextlib.contextmanager
def _temporary_accounts(users: list[str], roles: list[str]) -> Iterator[None]:
    """Delete the named users, roles and role mappings on exit.

    This sends raw requests instead of calling opensearch_client.security, so the cleanup
    still works when the code under test does not.
    """
    try:
        yield
    finally:
        for role in roles:
            _client.request("DELETE", f"_plugins/_security/api/rolesmapping/{role}")
        for user in users:
            _client.request("DELETE", f"_plugins/_security/api/internalusers/{user}")
        for role in roles:
            _client.request("DELETE", f"_plugins/_security/api/roles/{role}")


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _password() -> str:
    """Make a test password that passes the Security plugin's default policy.

    The policy wants upper and lower case, a digit and a special character; the
    fixed prefix guarantees those, and the random part keeps each run distinct.
    """
    return f"Aa1!{secrets.token_urlsafe(24)}"


def _login_as(user: str, password: str) -> OpensearchClient:
    """Build a client that authenticates as the given account, not the admin."""
    return OpensearchClient(
        DirectTransport(
            os.environ["OPENSEARCH_URL"],
            (user, password),
            os.environ.get("OPENSEARCH_CA_CERT") or True,
        )
    )


def _can_log_in(user: str, password: str) -> bool:
    """Whether the cluster accepts these credentials."""
    return bool(_login_as(user, password).get("_plugins/_security/authinfo"))


def test_index_and_read_back() -> None:
    index = _unique("opensearch_client-func")
    with _temporary_indices(index):
        assert _client.create_index(
            {"mappings": {"properties": {"name": {"type": "keyword"}}}}, index=index
        ).ok
        for i, name in enumerate(["a", "b", "c"]):
            assert _client.index_document(
                {"name": name, "n": i}, index=index, refresh=True
            ).ok

        search = _client.search({"query": {"match_all": {}}}, index=index)
        assert search
        assert len(search.data) == 3

        count = _client.count({"match_all": {}}, index=index)
        assert count
        assert count.data == 3

        rows = _client.sql(f"SELECT name FROM {index} ORDER BY n")
        assert rows
        assert [row["name"] for row in rows.data] == ["a", "b", "c"]


def test_triage_workflow_eliminates_a_layer() -> None:
    source = _unique("opensearch_client-func-src")
    dest = _unique("opensearch_client-func-triage")
    with _temporary_indices(source, dest):
        assert _client.create_index(
            {
                "mappings": {
                    "properties": {
                        "rule": {"properties": {"level": {"type": "integer"}}}
                    }
                }
            },
            index=source,
        ).ok
        for level in [1, 1, 1, 10, 10]:
            assert _client.index_document(
                {"rule": {"level": level}}, index=source, refresh=True
            ).ok

        # init copies the source into a fresh triage index, all untriaged.
        summary = triage.init(_client, source, dest, poll_seconds=0.5)
        assert summary["total"] == 5
        assert summary["tagged_untriaged"] == 5
        assert triage.status(_client, dest)["untriaged"] == 5

        # eliminate tags the low-severity docs (SQL predicate pushed down to DSL).
        # layer=None exercises the auto-increment branch; on a fresh index it is 1.
        result = triage.eliminate(
            _client,
            dest,
            "rule.level < 3",
            None,
            "low severity",
            apply=True,
        )
        assert result["updated"] == 3
        assert result["layer"] == 1

        after = triage.status(_client, dest)
        assert after["untriaged"] == 2
        assert after["eliminated_by_layer"] == {1: 3}

        # restore undoes layer 1: the 3 docs return to untriaged, and each records
        # the undone elimination on triage.history.
        undo = triage.restore(_client, dest, 1, None, apply=True)
        assert undo["restored"] == 3
        restored = triage.status(_client, dest)
        assert restored["untriaged"] == 5
        assert restored["eliminated_by_layer"] == {}

        rows = _client.search({"size": 10, "query": {"match_all": {}}}, index=dest)
        assert rows
        with_history = [doc for doc in rows.data if doc["triage"].get("history")]
        assert len(with_history) == 3  # only the restored docs carry history
        entry = with_history[0]["triage"]["history"][0]
        assert entry["layer"] == 1
        assert entry["query"] == "rule.level < 3"


def test_index_lifecycle_helpers() -> None:
    index = _unique("opensearch_client-func-lifecycle")
    with _temporary_indices(index):
        assert _client.create_index({}, index=index).ok
        assert _client.index_exists(index).data is True
        assert _client.index_document({"n": 1}, index=index).ok
        assert _client.refresh(index=index).ok
        listed = _client.list_indices("opensearch_client-func-lifecycle-*")
        assert listed and index in listed.data
        assert _client.delete_index(index).ok
        assert _client.index_exists(index).data is False


def test_bulk_indexes_documents_across_batches() -> None:
    index = _unique("opensearch_client-func-bulk")
    with _temporary_indices(index):
        docs = [{"name": f"host-{i}", "n": i} for i in range(50)]
        # A small byte cap forces the 50 documents across several batches.
        result = _client.bulk(docs, index=index, max_bytes=300)
        assert result, result.reason
        assert result.data["indexed"] == 50
        assert result.data["failed"] == 0
        assert result.data["batches"] > 1

        assert _client.request("POST", f"{index}/_refresh").ok
        count = _client.count({"match_all": {}}, index=index)
        assert count
        assert count.data == 50
        rows = _client.sql(f"SELECT name FROM {index} WHERE n = 7")
        assert rows
        assert [row["name"] for row in rows.data] == ["host-7"]


def test_bulk_reports_a_rejected_document() -> None:
    index = _unique("opensearch_client-func-bulk-reject")
    with _temporary_indices(index):
        assert _client.create_index(
            {"mappings": {"properties": {"n": {"type": "integer"}}}}, index=index
        ).ok
        # The middle document's value cannot coerce to the integer mapping, so the
        # cluster rejects that one item while indexing the others: a per-item error
        # inside an otherwise-200 bulk response.
        result = _client.bulk(
            [{"n": 1}, {"n": "not-an-int"}, {"n": 3}], index=index, max_retries=0
        )
        assert not result
        assert "1 of 3" in result.reason
        assert result.data["indexed"] == 2
        assert result.data["failed"] == 1
        failures = result.data["failures"]
        assert len(failures) == 1
        assert failures[0]["document"] == {"n": "not-an-int"}
        assert failures[0]["status"] >= 400
        assert failures[0]["error"] is not None


def test_index_template_pins_a_field_type_before_the_first_document() -> None:
    name = _unique("opensearch_client-func-template")
    index = f"{name}-2026.41"
    event_mapping = {"properties": {"kind": {"type": "keyword"}}}
    template = {
        "index_patterns": [f"{name}-*"],
        "priority": 500,
        "template": {"mappings": {"properties": {"event": event_mapping}}},
    }

    preview = _client.simulate_template(name, template)
    assert preview
    assert preview.data["template"]["mappings"]["properties"]["event"] == event_mapping
    assert _client.get_legacy_template()

    with _temporary_template(name), _temporary_indices(index):
        assert _client.put_index_template(name, template)
        # Previewing a change to an installed template must not clash with itself.
        assert _client.simulate_template(name, template)
        listed = _client.get_index_template(name)
        assert listed
        assert [entry["name"] for entry in listed.data["index_templates"]] == [name]
        resolved = _client.simulate_index(index)
        assert resolved
        properties = resolved.data["template"]["mappings"]["properties"]
        assert properties["event"] == event_mapping

        # Without the template, this first document would fix `event` as text and
        # every later object-shaped `event` would be rejected instead.
        scalar = _client.index_document({"event": "login:user-1"}, index=index)
        assert not scalar
        assert scalar.status == 400
        assert _client.index_document({"event": {"kind": "event"}}, index=index)

        assert _client.delete_index_template(name)
        assert _client.get_index_template(name).status == 404


def test_component_templates_compose_into_an_index_template() -> None:
    name = _unique("opensearch_client-func-component")
    component = f"{name}-fields"
    index = f"{name}-2026.41"
    field_mapping = {"type": "keyword"}
    template = {
        "index_patterns": [f"{name}-*"],
        "priority": 500,
        "composed_of": [component],
    }

    with _temporary_template(name), _temporary_component_template(component):
        assert _client.put_component_template(
            component,
            {"template": {"mappings": {"properties": {"host": field_mapping}}}},
        )
        listed = _client.get_component_template(component)
        assert listed
        assert [e["name"] for e in listed.data["component_templates"]] == [component]
        assert _client.put_index_template(name, template)

        resolved = _client.simulate_index(index)
        assert resolved
        assert resolved.data["template"]["mappings"]["properties"]["host"] == (
            field_mapping
        )

        # A component still composed by an index template cannot be deleted.
        assert not _client.delete_component_template(component)
        assert _client.delete_index_template(name)
        assert _client.delete_component_template(component)
        assert _client.get_component_template(component).status == 404


def test_rollover_dry_run_then_apply_moves_the_write_index_with_new_settings() -> None:
    prefix = _unique("opensearch_client-func-rollover")
    alias = f"{prefix}-write"
    first, second = f"{prefix}-000001", f"{prefix}-000002"
    assert _client.get_alias(alias).status == 404
    with _temporary_indices(first, second):
        assert _client.create_index(
            {
                "aliases": {alias: {"is_write_index": True}},
                "settings": {"index.number_of_shards": 2},
            },
            index=first,
        )

        preview = _client.rollover(alias, dry_run=True)
        assert preview
        assert preview.data["new_index"] == second
        assert preview.data["rolled_over"] is False
        assert _client.get(second).status == 404

        rolled = _client.rollover(alias, settings={"index.number_of_shards": 1})
        assert rolled
        assert rolled.data["new_index"] == second
        assert rolled.data["rolled_over"] is True
        new_settings = _client.get(f"{second}/_settings")
        assert new_settings
        assert new_settings.data[second]["settings"]["index"]["number_of_shards"] == "1"
        holders = _client.get_alias(alias)
        assert holders
        # The old index keeps the alias but stops being where new writes go.
        assert sorted(holders.data) == [first, second]
        assert holders.data[first]["aliases"][alias]["is_write_index"] is False
        assert holders.data[second]["aliases"][alias]["is_write_index"] is True


def test_a_log_writer_account_may_create_and_write_its_index_and_nothing_else() -> None:
    index = _unique("opensearch_client-func-logs")
    other_index = _unique("opensearch_client-func-other")
    uncreated_index = _unique("opensearch_client-func-uncreated")
    user = _unique("opensearch_client-func-writer")
    role = f"{user}-writer"
    password = _password()

    with (
        _temporary_indices(index, other_index, uncreated_index),
        _temporary_accounts([user], [role]),
    ):
        # Settle the admin client's transport first: as the very first request, a
        # 404 is read as "the cluster did not answer".
        assert _client.get("_cluster/health")
        assert _client.create_index({}, index=other_index)

        created = security.create_log_writer_account(_client, user, password, index)
        assert created, created.reason
        checked = security.check_log_writer_account(_client, user, index)
        assert checked, checked.reason
        assert checked.data == {
            "user": True,
            "role": True,
            "role_mapping": True,
            "index": False,
        }

        # What the log handler does on a fresh environment: look the index up,
        # create it, and ship documents to it.
        writer = _login_as(user, password)
        exists = writer.index_exists(index)
        assert exists, exists.reason
        assert exists.data is False
        made = writer.create_index({}, index=index)
        assert made, made.reason
        shipped = writer.bulk(
            [{"message": f"line {n}", "labels": {"n": n}} for n in range(3)],
            index=index,
        )
        assert shipped, shipped.reason
        assert shipped.data["indexed"] == 3

        # And nothing else: no reading, and no other index.
        assert writer.search({"query": {"match_all": {}}}, index=index).status == 403
        assert writer.index_document({"n": 1}, index=other_index).status == 403
        assert writer.create_index({}, index=uncreated_index).status == 403
        assert _client.refresh(index=index)
        rows = _client.search({"size": 10, "query": {"match_all": {}}}, index=index)
        assert rows, rows.reason
        assert len(rows.data) == 3

        # Creation never overwrites: a second attempt fails and the password stays.
        replacement = _password()
        again = security.create_log_writer_account(_client, user, replacement, index)
        assert not again
        assert again.status == 409
        assert _can_log_in(user, password)
        assert not _can_log_in(user, replacement)

        # Deleting takes every part away; the account can no longer log in.
        deleted = security.delete_log_writer_account(_client, user, apply=True)
        assert deleted, deleted.reason
        assert deleted.data == {"role_mapping": True, "user": True, "role": True}
        assert not _can_log_in(user, password)
