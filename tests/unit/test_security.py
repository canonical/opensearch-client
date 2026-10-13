# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for opensearch_client.security."""

import copy
import re
from collections.abc import Callable
from typing import Any

import pytest
from helpers import RecordingTransport, ScriptedTransport

from opensearch_client import security
from opensearch_client.client import OpensearchClient
from opensearch_client.result import Failure, OpensearchResult, Success
from opensearch_client.security import Role, RoleMapping, User


def test_the_request_bodies_hold_only_what_the_api_needs() -> None:
    role = Role(
        name="example-role",
        cluster_permissions=("cluster_monitor",),
        index_patterns=("example-index-*",),
        index_actions=("read",),
    )
    assert role.to_dict() == {
        "cluster_permissions": ["cluster_monitor"],
        "index_permissions": [
            {"index_patterns": ["example-index-*"], "allowed_actions": ["read"]}
        ],
    }

    # No index patterns means no index permissions entry at all.
    assert Role(name="example-role").to_dict() == {
        "cluster_permissions": [],
        "index_permissions": [],
    }

    mapping = RoleMapping(role="example-role", users=("example-user", "other-user"))
    assert mapping.to_dict() == {"users": ["example-user", "other-user"]}

    user = User(name="example-user", password="a-secret-value")
    assert user.to_dict() == {"password": "a-secret-value"}
    assert "a-secret-value" not in repr(user)


def test_an_invalid_object_cannot_be_created() -> None:
    refused: list[tuple[Callable[[], object], str]] = [
        (lambda: Role(name=""), "role name '' is not valid"),
        (lambda: Role(name=".."), "role name '..' is not valid"),
        (
            lambda: Role(name="r", index_patterns=("a",)),
            "index_patterns and index_actions must be given together",
        ),
        (
            lambda: Role(name="r", index_actions=("read",)),
            "index_patterns and index_actions must be given together",
        ),
        (
            lambda: Role(name="r", index_patterns=("  ",), index_actions=("read",)),
            "is blank",
        ),
        (lambda: Role(name="r", cluster_permissions=("",)), "is blank"),
        (lambda: RoleMapping(role=" ", users=("u",)), "role name ' ' is not valid"),
        (lambda: RoleMapping(role="r", users=()), "needs at least one user"),
        (lambda: RoleMapping(role="r", users=("",)), "user name '' is not valid"),
        (lambda: RoleMapping(role="r", users=("a*",)), "must not contain"),
        (lambda: User(name="", password="a-secret-value"), "user name '' is not valid"),
        (lambda: User(name="a?", password="a-secret-value"), "must not contain"),
        (lambda: User(name="example-user", password=""), "needs a password"),
    ]

    for create, expected in refused:
        with pytest.raises(ValueError, match=re.escape(expected)) as caught:
            create()
        assert "a-secret-value" not in str(caught.value)

    # A role with no permissions at all is valid, and so is a pattern with a dot.
    assert Role(name=".hidden-role")


def test_get_and_list_send_one_get_and_return_the_response_unchanged() -> None:
    response = {"example": {"description": "as the API returned it"}}
    transport = RecordingTransport(response)
    client = OpensearchClient(transport)

    fetched = [
        security.get_role(client, "example-role"),
        security.get_user(client, "example-user"),
        security.get_role_mapping(client, "example-role"),
        security.list_roles(client),
        security.list_users(client),
        security.list_role_mappings(client),
    ]

    for result in fetched:
        assert result
        assert result.data == response
    assert transport.calls == [
        ("GET", "_plugins/_security/api/roles/example-role", None),
        ("GET", "_plugins/_security/api/internalusers/example-user", None),
        ("GET", "_plugins/_security/api/rolesmapping/example-role", None),
        ("GET", "_plugins/_security/api/roles", None),
        ("GET", "_plugins/_security/api/internalusers", None),
        ("GET", "_plugins/_security/api/rolesmapping", None),
    ]


def test_a_name_is_encoded_so_it_cannot_change_the_endpoint() -> None:
    transport = RecordingTransport()

    security.get_role(OpensearchClient(transport), "a/b?c d")

    assert transport.calls == [
        ("GET", "_plugins/_security/api/roles/a%2Fb%3Fc%20d", None)
    ]


def test_a_name_that_would_not_address_one_object_is_refused_without_a_request() -> (
    None
):
    calls: list[Callable[[OpensearchClient, str], OpensearchResult[Any]]] = [
        security.get_role,
        security.get_user,
        security.get_role_mapping,
        security.role_exists,
        security.user_exists,
        security.role_mapping_exists,
        security.delete_role,
        security.delete_user,
        security.delete_role_mapping,
    ]

    for blank_or_dot in ("", "   ", ".", ".."):
        for call in calls:
            transport = RecordingTransport()
            result = call(OpensearchClient(transport), blank_or_dot)
            assert not result
            assert "is not valid" in result.reason
            assert transport.calls == []

    # A name that merely starts with a dot is fine.
    transport = RecordingTransport()
    assert security.get_role(OpensearchClient(transport), ".hidden-role")
    assert len(transport.calls) == 1


def test_existence_checks_report_true_false_or_the_failure() -> None:
    checks = (
        (security.role_exists, "_plugins/_security/api/roles/example"),
        (security.user_exists, "_plugins/_security/api/internalusers/example"),
        (security.role_mapping_exists, "_plugins/_security/api/rolesmapping/example"),
    )
    forbidden = Failure("403 from GET", status=403)

    for exists, path in checks:
        found = ScriptedTransport([Success({"example": {}})])
        assert exists(OpensearchClient(found), "example") == Success(True)
        assert found.calls == [("GET", path, None)]

        missing = ScriptedTransport([Failure("404 from GET", status=404)])
        assert exists(OpensearchClient(missing), "example") == Success(False)

        denied = ScriptedTransport([forbidden])
        assert exists(OpensearchClient(denied), "example") == forbidden


def test_create_puts_only_when_the_object_is_absent() -> None:
    role = Role(name="example-role", cluster_permissions=("cluster_monitor",))
    user = User(name="example-user", password="a-secret-value")
    mapping = RoleMapping(role="example-role", users=("example-user",))
    cases: list[
        tuple[
            Callable[[OpensearchClient], OpensearchResult[dict[str, Any]]],
            str,
            dict[str, Any],
        ]
    ] = [
        (
            lambda client: security.create_role(client, role),
            "_plugins/_security/api/roles/example-role",
            role.to_dict(),
        ),
        (
            lambda client: security.create_user(client, user),
            "_plugins/_security/api/internalusers/example-user",
            user.to_dict(),
        ),
        (
            lambda client: security.create_role_mapping(client, mapping),
            "_plugins/_security/api/rolesmapping/example-role",
            mapping.to_dict(),
        ),
    ]
    acknowledgement = {"status": "CREATED"}
    forbidden = Failure("403 from GET", status=403)

    for create, path, body in cases:
        # Absent: the check finds nothing, then the object is sent.
        absent = ScriptedTransport(
            [Failure("404 from GET", status=404), Success(acknowledgement)]
        )
        assert create(OpensearchClient(absent)) == Success(acknowledgement)
        assert absent.calls == [("GET", path, None), ("PUT", path, body)]

        # Present: a 409 failure, and nothing is sent.
        present = ScriptedTransport([Success({"example": {}})])
        refused = create(OpensearchClient(present))
        assert not refused
        assert refused.status == 409
        assert present.calls == [("GET", path, None)]

        # A check that fails for another reason is returned, and nothing is sent.
        unchecked = ScriptedTransport([forbidden])
        assert create(OpensearchClient(unchecked)) == forbidden
        assert unchecked.calls == [("GET", path, None)]


def test_the_logging_writer_role_may_only_check_and_write_its_indexes() -> None:
    built = security.build_logging_writer_role(
        "example-writer", ("example-logs-*", "other-logs")
    )

    assert built
    assert built.data.name == "example-writer"
    assert built.data.to_dict() == {
        "cluster_permissions": ["indices:data/write/bulk"],
        "index_permissions": [
            {
                "index_patterns": ["example-logs-*", "other-logs"],
                "allowed_actions": ["indices:admin/get", "index"],
            }
        ],
    }


def test_the_logging_writer_role_refuses_a_name_or_patterns_that_are_not_safe() -> None:
    refused = [
        ("", ("example-logs",), "is not valid"),
        ("..", ("example-logs",), "is not valid"),
        ("example-role", (), "needs at least one index pattern"),
        ("example-role", ("",), "is blank"),
        ("example-role", ("   ",), "is blank"),
        ("example-role", ("*",), "'*'"),
        ("example-role", ("example-logs", "*"), "'*'"),
    ]

    for name, patterns, expected in refused:
        result = security.build_logging_writer_role(name, patterns)
        assert not result
        assert expected in result.reason

    # A wildcard that is narrower than a bare '*' is fine.
    assert security.build_logging_writer_role("example-role", ("example-*",))


def test_create_service_account_creates_the_role_then_the_user_then_the_mapping() -> (
    None
):
    role = security.build_logging_writer_role("example-role", ("example-logs",)).data
    user = User(name="example-user", password="a-secret-value")
    absent = Failure("404 from GET", status=404)
    acknowledged = Success({"status": "CREATED"})
    transport = ScriptedTransport(
        [absent, acknowledged, absent, acknowledged, absent, acknowledged]
    )

    result = security.create_service_account(OpensearchClient(transport), user, role)

    assert result == Success(
        {"role": "example-role", "user": "example-user", "role_mapping": "example-role"}
    )
    roles = "_plugins/_security/api/roles/example-role"
    users = "_plugins/_security/api/internalusers/example-user"
    mappings = "_plugins/_security/api/rolesmapping/example-role"
    assert transport.calls == [
        ("GET", roles, None),
        ("PUT", roles, role.to_dict()),
        ("GET", users, None),
        ("PUT", users, user.to_dict()),
        ("GET", mappings, None),
        ("PUT", mappings, {"users": ["example-user"]}),
    ]


def test_create_service_account_reports_what_was_created_before_a_failure() -> None:
    role = security.build_logging_writer_role("example-role", ("example-logs",)).data
    user = User(name="example-user", password="a-secret-value")
    # The role is new, but the user already exists.
    transport = ScriptedTransport(
        [
            Failure("404 from GET", status=404),
            Success({"status": "CREATED"}),
            Success({"example-user": {}}),
        ]
    )

    result = security.create_service_account(OpensearchClient(transport), user, role)

    assert not result
    assert result.status == 409
    assert "user" in result.reason
    assert result.data == {"created": ["role"]}
    assert len(transport.calls) == 3
    assert "a-secret-value" not in result.reason


def test_delete_sends_one_delete_and_returns_the_api_answer_unchanged() -> None:
    acknowledgement = {"status": "OK", "message": "'example' deleted."}
    transport = RecordingTransport(acknowledgement)
    client = OpensearchClient(transport)

    deleted = [
        security.delete_role(client, "example-role"),
        security.delete_user(client, "example-user"),
        security.delete_role_mapping(client, "example-role"),
    ]

    for result in deleted:
        assert result == Success(acknowledgement)
    assert transport.calls == [
        ("DELETE", "_plugins/_security/api/roles/example-role", None),
        ("DELETE", "_plugins/_security/api/internalusers/example-user", None),
        ("DELETE", "_plugins/_security/api/rolesmapping/example-role", None),
    ]

    # A failure, such as a 404 or a refusal to delete a reserved object, is the
    # API's answer and is returned as it is.
    missing = Failure("404 from DELETE", status=404)
    for delete in (
        security.delete_role,
        security.delete_user,
        security.delete_role_mapping,
    ):
        transport = ScriptedTransport([missing])
        assert delete(OpensearchClient(transport), "example") == missing


def test_check_service_account_reports_each_part() -> None:
    present = Success({"example": {}})
    absent = Failure("404 from GET", status=404)

    everything = ScriptedTransport([present, present, present, present])
    result = security.check_service_account(
        OpensearchClient(everything), "example-user", "example-role", "example-logs"
    )
    assert result == Success(
        {"user": True, "role": True, "role_mapping": True, "index": True}
    )
    assert everything.calls == [
        ("GET", "_plugins/_security/api/internalusers/example-user", None),
        ("GET", "_plugins/_security/api/roles/example-role", None),
        ("GET", "_plugins/_security/api/rolesmapping/example-role", None),
        ("GET", "example-logs", None),
    ]

    # Only the index exists, as before an account is created.
    only_index = ScriptedTransport([absent, absent, absent, present])
    result = security.check_service_account(
        OpensearchClient(only_index), "example-user", "example-role", "example-logs"
    )
    assert result == Success(
        {"user": False, "role": False, "role_mapping": False, "index": True}
    )


def test_check_service_account_returns_the_first_failure_and_stops() -> None:
    forbidden = Failure("403 from GET", status=403)
    transport = ScriptedTransport([Success({"example": {}}), forbidden])

    result = security.check_service_account(
        OpensearchClient(transport), "example-user", "example-role", "example-logs"
    )

    assert result == forbidden
    assert len(transport.calls) == 2


def test_check_service_account_refuses_input_that_would_not_name_one_object() -> None:
    refused = [
        ("", "example-role", "example-logs"),
        ("example-user", "..", "example-logs"),
        ("example-user", "example-role", ""),
        ("example-user", "example-role", ".."),
        ("example-user", "example-role", "_all"),
        ("example-user", "example-role", "_cluster"),
        ("example-user", "example-role", "example-*"),
        ("example-user", "example-role", "example-?"),
        ("example-user", "example-role", "one,two"),
    ]

    for user, role, index in refused:
        transport = RecordingTransport()
        result = security.check_service_account(
            OpensearchClient(transport), user, role, index
        )
        assert not result
        assert "is not valid" in result.reason
        assert transport.calls == []

    # An index name is encoded, so it cannot change the endpoint.
    transport = RecordingTransport()
    security.check_service_account(
        OpensearchClient(transport), "example-user", "example-role", "a/b c"
    )
    assert transport.calls[-1] == ("GET", "a%2Fb%20c", None)


def test_redact_secrets_replaces_every_secret_and_leaves_the_input_alone() -> None:
    response: dict[str, Any] = {
        "example-user": {
            "hash": "$2y$12$not-a-real-hash",
            "password": "a-secret-value",
            "backend_roles": ["example"],
            "attributes": {"Password": "another-secret", "team": "ops"},
        },
        "other-user": [{"hash": ""}, "plain", 5, None],
    }
    original = copy.deepcopy(response)

    redacted = security.redact_secrets(response)

    assert redacted == {
        "example-user": {
            "hash": security.REDACTED,
            "password": security.REDACTED,
            "backend_roles": ["example"],
            "attributes": {"Password": security.REDACTED, "team": "ops"},
        },
        "other-user": [{"hash": security.REDACTED}, "plain", 5, None],
    }
    assert response == original

    # A scalar is returned as it is, and a secret is hidden whatever its type.
    assert security.redact_secrets("text") == "text"
    assert security.redact_secrets({"password": {"nested": "x"}}) == {
        "password": security.REDACTED
    }
