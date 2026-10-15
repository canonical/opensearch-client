# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for opensearch_client.security.

The cluster is a FakeSecurityApi, so these tests check what ends up stored and
what is reported. That the permissions actually let the account write logs, and
nothing else, is covered by the functional tests.
"""

from helpers import FakeSecurityApi

from opensearch_client import security
from opensearch_client.client import OpensearchClient

ROLE = "_plugins/_security/api/roles/collector-logging-writer"
USER = "_plugins/_security/api/internalusers/collector-logging"
MAPPING = "_plugins/_security/api/rolesmapping/collector-logging-writer"


def test_create_stores_the_role_the_user_and_the_mapping() -> None:
    api = FakeSecurityApi()

    result = security.create_log_writer_account(
        OpensearchClient(api), "collector-logging", "a-secret-value", "collector-logs"
    )

    assert result
    assert result.data == {
        "role": "collector-logging-writer",
        "user": "collector-logging",
        "role_mapping": "collector-logging-writer",
    }
    assert api.objects == {
        ROLE: {
            "cluster_permissions": ["indices:data/write/bulk"],
            "index_permissions": [
                {
                    "index_patterns": ["collector-logs"],
                    "allowed_actions": ["indices:admin/get", "create_index", "index"],
                }
            ],
        },
        USER: {"password": "a-secret-value"},
        MAPPING: {"users": ["collector-logging"]},
    }
    # Access is granted last, so a failure part-way leaves no working account.
    puts = [path for method, path, _ in api.calls if method == "PUT"]
    assert puts == [ROLE, USER, MAPPING]


def test_create_never_overwrites_and_reports_what_it_created_first() -> None:
    api = FakeSecurityApi()
    api.objects[USER] = {"password": "the-original"}

    result = security.create_log_writer_account(
        OpensearchClient(api), "collector-logging", "a-replacement", "collector-logs"
    )

    assert not result
    assert result.status == 409
    assert result.data == {"created": ["role"]}
    assert api.objects[USER] == {"password": "the-original"}
    assert MAPPING not in api.objects
    assert "a-replacement" not in result.reason


def test_create_returns_a_failed_lookup_without_writing() -> None:
    api = FakeSecurityApi()
    api.denied = True

    result = security.create_log_writer_account(
        OpensearchClient(api), "collector-logging", "a-secret-value", "collector-logs"
    )

    assert not result
    assert result.status == 403
    assert result.data == {"created": []}
    assert [method for method, _, _ in api.calls] == ["GET"]


def test_a_name_that_would_not_address_one_object_is_refused_without_a_request() -> (
    None
):
    bad_users = ["", "   ", ".", "..", "collector-*", "collector-?"]
    bad_indexes = ["", ".", "..", "_all", "_cat", "logs-*", "logs-?", "a,b"]

    for user in bad_users:
        api = FakeSecurityApi()
        client = OpensearchClient(api)
        assert not security.create_log_writer_account(client, user, "pw", "logs")
        assert not security.check_log_writer_account(client, user, "logs")
        assert not security.delete_log_writer_account(client, user, apply=True)
        assert api.calls == []

    for index in bad_indexes:
        api = FakeSecurityApi()
        client = OpensearchClient(api)
        assert not security.create_log_writer_account(client, "user", "pw", index)
        assert not security.check_log_writer_account(client, "user", index)
        assert api.calls == []

    api = FakeSecurityApi()
    refused = security.create_log_writer_account(
        OpensearchClient(api), "user", "", "logs"
    )
    assert not refused
    assert "needs a password" in refused.reason
    assert api.calls == []


def test_a_name_is_encoded_so_it_cannot_change_the_endpoint() -> None:
    api = FakeSecurityApi()

    security.delete_log_writer_account(OpensearchClient(api), "a/b c", apply=False)

    assert api.calls[1] == (
        "GET",
        "_plugins/_security/api/internalusers/a%2Fb%20c",
        None,
    )


def test_check_reports_each_part_and_the_index() -> None:
    api = FakeSecurityApi(indices=("collector-logs",))
    api.objects[USER] = {"password": "a-secret-value"}
    api.objects[ROLE] = {}

    result = security.check_log_writer_account(
        OpensearchClient(api), "collector-logging", "collector-logs"
    )

    assert result
    assert result.data == {
        "user": True,
        "role": True,
        "role_mapping": False,
        "index": True,
    }


def test_check_returns_the_first_failure_and_stops() -> None:
    api = FakeSecurityApi()
    api.denied = True

    result = security.check_log_writer_account(
        OpensearchClient(api), "collector-logging", "collector-logs"
    )

    assert not result
    assert result.status == 403
    assert len(api.calls) == 1


def test_delete_without_apply_only_reports_what_exists() -> None:
    api = FakeSecurityApi()
    api.objects[USER] = {"password": "a-secret-value"}
    api.objects[ROLE] = {}
    stored_before = dict(api.objects)

    result = security.delete_log_writer_account(
        OpensearchClient(api), "collector-logging", apply=False
    )

    assert result
    assert result.data == {"role_mapping": False, "user": True, "role": True}
    assert api.objects == stored_before


def test_delete_removes_the_mapping_first_and_skips_what_is_gone() -> None:
    api = FakeSecurityApi()
    api.objects[MAPPING] = {"users": ["collector-logging"]}
    api.objects[ROLE] = {}

    result = security.delete_log_writer_account(
        OpensearchClient(api), "collector-logging", apply=True
    )

    assert result
    assert result.data == {"role_mapping": True, "user": False, "role": True}
    assert api.objects == {}
    assert [path for _, path, _ in api.calls] == [MAPPING, USER, ROLE]
