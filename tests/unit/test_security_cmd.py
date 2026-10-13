# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for opensearch_client.cli.security_cmd."""

import io
import os
import sys
import tempfile

import pytest
import yaml
from helpers import RecordingTransport, ScriptedTransport, run_cli

from opensearch_client.cli import main, security_cmd
from opensearch_client.result import Failure, Success

ROLES = "_plugins/_security/api/roles"
USERS = "_plugins/_security/api/internalusers"
MAPPINGS = "_plugins/_security/api/rolesmapping"
ABSENT = Failure("404 from GET", status=404)
ACKNOWLEDGED = Success({"status": "CREATED"})


def _write(directory: str, name: str, content: bytes) -> str:
    """Write a file in the directory and return its path."""
    path = os.path.join(directory, name)
    with open(path, "wb") as handle:
        handle.write(content)
    return path


def test_show_fetches_one_or_all_and_never_prints_a_secret() -> None:
    response = {"example": {"hash": "$2y$12$not-a-real-hash", "backend_roles": []}}
    cases = [
        (["role"], ROLES),
        (["role", "example-role"], f"{ROLES}/example-role"),
        (["user"], USERS),
        (["user", "example-user"], f"{USERS}/example-user"),
        (["mapping"], MAPPINGS),
        (["mapping", "example-role"], f"{MAPPINGS}/example-role"),
    ]

    for argv, path in cases:
        transport = RecordingTransport(response)
        output = run_cli(security_cmd, "security", argv, transport)

        assert transport.calls == [("GET", path, None)]
        assert "not-a-real-hash" not in output
        assert yaml.safe_load(output)["example"]["hash"] == "<redacted>"


def test_create_account_reads_the_password_from_a_file_and_never_prints_it() -> None:
    with tempfile.TemporaryDirectory() as directory:
        password_file = _write(directory, "password", b"a-secret-value\n")
        transport = ScriptedTransport([ABSENT, ACKNOWLEDGED] * 3)

        output = run_cli(
            security_cmd,
            "security",
            ["create", "account", "example-user", "--index", "example-logs"]
            + ["--password-file", password_file],
            transport,
        )

    role = f"{ROLES}/example-user-writer"
    user = f"{USERS}/example-user"
    mapping = f"{MAPPINGS}/example-user-writer"
    assert [(method, path) for method, path, _ in transport.calls] == [
        ("GET", role),
        ("PUT", role),
        ("GET", user),
        ("PUT", user),
        ("GET", mapping),
        ("PUT", mapping),
    ]
    assert transport.calls[3][2] == {"password": "a-secret-value"}
    assert transport.calls[5][2] == {"users": ["example-user"]}
    assert yaml.safe_load(output) == {
        "role": "example-user-writer",
        "user": "example-user",
        "role_mapping": "example-user-writer",
    }
    assert "a-secret-value" not in output


def test_create_account_takes_a_role_name_several_indexes_and_stdin() -> None:
    transport = ScriptedTransport([ABSENT, ACKNOWLEDGED] * 3)
    saved_stdin = sys.stdin
    sys.stdin = io.StringIO("from-stdin\n")
    try:
        run_cli(
            security_cmd,
            "security",
            ["create", "account", "example-user", "--role", "custom-role"]
            + ["--index", "first-*", "--index", "second", "--password-file", "-"],
            transport,
        )
    finally:
        sys.stdin = saved_stdin

    assert transport.calls[1] == (
        "PUT",
        f"{ROLES}/custom-role",
        {
            "cluster_permissions": ["indices:data/write/bulk"],
            "index_permissions": [
                {
                    "index_patterns": ["first-*", "second"],
                    "allowed_actions": ["indices:admin/get", "index"],
                }
            ],
        },
    )
    assert transport.calls[3][2] == {"password": "from-stdin"}


def test_create_account_refuses_bad_input_before_any_request() -> None:
    with tempfile.TemporaryDirectory() as directory:
        good = _write(directory, "good", b"a-secret-value\n")
        empty = _write(directory, "empty", b"\n")
        binary = _write(directory, "binary", b"\xff\xfe")
        missing = os.path.join(directory, "missing")
        refused = [
            ("example-user", "*", good),
            ("example-user", "", good),
            ("..", "example-logs", good),
            ("example-user", "example-logs", empty),
            ("example-user", "example-logs", binary),
            ("example-user", "example-logs", missing),
        ]

        for user, index, password_file in refused:
            transport = RecordingTransport()
            with pytest.raises(SystemExit) as caught:
                run_cli(
                    security_cmd,
                    "security",
                    ["create", "account", user, "--index", index]
                    + ["--password-file", password_file],
                    transport,
                )
            assert caught.value.code == 2
            assert transport.calls == []


def test_create_account_that_fails_part_way_exits_with_status_1() -> None:
    with tempfile.TemporaryDirectory() as directory:
        password_file = _write(directory, "password", b"a-secret-value\n")
        # The role is new, but the user already exists.
        transport = ScriptedTransport(
            [ABSENT, ACKNOWLEDGED, Success({"example-user": {}})]
        )

        with pytest.raises(SystemExit) as caught:
            run_cli(
                security_cmd,
                "security",
                ["create", "account", "example-user", "--index", "example-logs"]
                + ["--password-file", password_file],
                transport,
            )

    assert caught.value.code == 1
    assert len(transport.calls) == 3


def test_delete_is_a_dry_run_unless_applied() -> None:
    kinds = [("role", ROLES), ("user", USERS), ("mapping", MAPPINGS)]

    for kind, base in kinds:
        path = f"{base}/example"

        # A dry run only looks: it reports whether the object exists.
        found = ScriptedTransport([Success({"example": {}})])
        output = run_cli(security_cmd, "security", ["delete", kind, "example"], found)
        assert found.calls == [("GET", path, None)]
        assert yaml.safe_load(output) == {
            kind: "example",
            "dry_run": True,
            "exists": True,
        }

        missing = ScriptedTransport([ABSENT])
        output = run_cli(security_cmd, "security", ["delete", kind, "example"], missing)
        assert yaml.safe_load(output)["exists"] is False

        # --apply deletes, without a lookup first.
        applied = RecordingTransport({"status": "OK"})
        run_cli(
            security_cmd, "security", ["delete", kind, "example", "--apply"], applied
        )
        assert applied.calls == [("DELETE", path, None)]


def test_check_reports_which_parts_of_an_account_exist() -> None:
    present = Success({"example": {}})
    transport = ScriptedTransport([present, present, ABSENT, present])

    output = run_cli(
        security_cmd,
        "security",
        ["check", "example-user", "--role", "example-role", "--index", "example-logs"],
        transport,
    )

    assert yaml.safe_load(output) == {
        "user": True,
        "role": True,
        "role_mapping": False,
        "index": True,
    }


def test_read_password_removes_only_the_trailing_newline() -> None:
    with tempfile.TemporaryDirectory() as directory:
        padded = _write(directory, "padded", b"  a pass word  \r\n")
        empty = _write(directory, "empty", b"")
        binary = _write(directory, "binary", b"\xff\xfe")
        missing = os.path.join(directory, "missing")

        assert security_cmd._read_password(padded) == "  a pass word  "
        assert security_cmd._read_password(empty) is None
        assert security_cmd._read_password(binary) is None
        assert security_cmd._read_password(missing) is None

    saved_stdin = sys.stdin
    sys.stdin = io.StringIO("from-stdin\n")
    try:
        assert security_cmd._read_password("-") == "from-stdin"
    finally:
        sys.stdin = saved_stdin


def test_main_gives_the_security_command_a_direct_connection_only() -> None:
    keys = [
        "OPENSEARCH_URL",
        "OPENSEARCH_HOST",
        "OPENSEARCH_DASHBOARD_URL",
        "OPENSEARCH_DASHBOARD_HOST",
        "OPENSEARCH_USER",
        "OPENSEARCH_PASSWORD",
    ]
    saved_environment = {key: os.environ.get(key) for key in keys}
    saved_argv = sys.argv
    for key in keys:
        os.environ.pop(key, None)
    os.environ["OPENSEARCH_USER"] = "example-admin"
    os.environ["OPENSEARCH_PASSWORD"] = "example-password"
    os.environ["OPENSEARCH_DASHBOARD_URL"] = "https://dashboard.example"
    sys.argv = ["os-cli", "security", "role"]
    try:
        # Only a dashboard proxy is configured, which the Security API must not use,
        # so main stops before any request.
        with pytest.raises(SystemExit) as caught:
            main.main()
    finally:
        sys.argv = saved_argv
        for key in keys:
            os.environ.pop(key, None)
            previous = saved_environment[key]
            if previous is not None:
                os.environ[key] = previous

    assert caught.value.code == 2
