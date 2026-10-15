# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for opensearch_client.cli.security_cmd.

What each account operation does is tested in test_security.py; these tests cover
what the CLI adds: reading the password, keeping it out of the output, and the
exit statuses.
"""

import io
import os
import sys
import tempfile

import pytest
import yaml
from helpers import FakeSecurityApi, run_cli

from opensearch_client.cli import main, security_cmd

USER = "_plugins/_security/api/internalusers/collector-logging"


def _write(directory: str, name: str, content: bytes) -> str:
    """Write a file in the directory and return its path."""
    path = os.path.join(directory, name)
    with open(path, "wb") as handle:
        handle.write(content)
    return path


def test_create_account_reads_the_password_from_a_file_and_never_prints_it() -> None:
    api = FakeSecurityApi()
    with tempfile.TemporaryDirectory() as directory:
        password_file = _write(directory, "password", b"a-secret-value\n")
        output = run_cli(
            security_cmd,
            "security",
            ["create-account", "collector-logging", "--index", "collector-logs"]
            + ["--password-file", password_file],
            api,
        )

    assert api.objects[USER] == {"password": "a-secret-value"}
    assert yaml.safe_load(output) == {
        "role": "collector-logging-writer",
        "user": "collector-logging",
        "role_mapping": "collector-logging-writer",
    }
    assert "a-secret-value" not in output


def test_create_account_reads_the_password_from_stdin() -> None:
    api = FakeSecurityApi()
    saved_stdin = sys.stdin
    sys.stdin = io.StringIO("from-stdin\n")
    try:
        run_cli(
            security_cmd,
            "security",
            ["create-account", "collector-logging", "--index", "collector-logs"]
            + ["--password-file", "-"],
            api,
        )
    finally:
        sys.stdin = saved_stdin

    assert api.objects[USER] == {"password": "from-stdin"}


def test_create_account_refuses_bad_input_before_any_request() -> None:
    with tempfile.TemporaryDirectory() as directory:
        good = _write(directory, "good", b"a-secret-value\n")
        empty = _write(directory, "empty", b"\n")
        binary = _write(directory, "binary", b"\xff\xfe")
        missing = os.path.join(directory, "missing")
        # (user, index, password file, expected exit status)
        refused = [
            ("collector-logging", "collector-logs", empty, 2),
            ("collector-logging", "collector-logs", binary, 2),
            ("collector-logging", "collector-logs", missing, 2),
            ("collector-logging", "logs-*", good, 1),
            ("..", "collector-logs", good, 1),
        ]

        for user, index, password_file, expected_status in refused:
            api = FakeSecurityApi()
            with pytest.raises(SystemExit) as caught:
                run_cli(
                    security_cmd,
                    "security",
                    ["create-account", user, "--index", index]
                    + ["--password-file", password_file],
                    api,
                )
            assert caught.value.code == expected_status
            assert api.calls == []


def test_create_account_that_fails_part_way_exits_with_status_1() -> None:
    api = FakeSecurityApi()
    api.objects[USER] = {"password": "the-original"}
    with tempfile.TemporaryDirectory() as directory:
        password_file = _write(directory, "password", b"a-secret-value\n")
        with pytest.raises(SystemExit) as caught:
            run_cli(
                security_cmd,
                "security",
                ["create-account", "collector-logging", "--index", "collector-logs"]
                + ["--password-file", password_file],
                api,
            )

    assert caught.value.code == 1
    assert api.objects[USER] == {"password": "the-original"}


def test_delete_account_is_a_dry_run_unless_applied() -> None:
    api = FakeSecurityApi()
    api.objects[USER] = {"password": "a-secret-value"}

    output = run_cli(
        security_cmd, "security", ["delete-account", "collector-logging"], api
    )
    assert yaml.safe_load(output) == {
        "role_mapping": False,
        "user": True,
        "role": False,
    }
    assert USER in api.objects

    run_cli(
        security_cmd,
        "security",
        ["delete-account", "collector-logging", "--apply"],
        api,
    )
    assert api.objects == {}


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
    sys.argv = ["os-cli", "security", "check-account", "u", "--index", "logs"]
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
