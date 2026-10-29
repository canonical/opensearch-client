# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared helpers for the unit tests."""

import contextlib
import io
import json
from argparse import ArgumentParser
from types import ModuleType
from typing import Any

from opensearch_client.client import OpensearchClient
from opensearch_client.result import OpensearchResult, Success
from opensearch_client.transport import Transport


class RecordingTransport:
    """Records each request and answers every one with the same payload."""

    def __init__(self, payload: Any = None) -> None:
        self.payload = {} if payload is None else payload
        self.calls: list[tuple[str, str, Any]] = []

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: str = "application/json",
        timeout: int = 30,
    ) -> OpensearchResult[Any]:
        decoded = None if body is None else json.loads(body)
        self.calls.append((method, path, decoded))
        return Success(self.payload)


def run_cli(
    module: ModuleType, command: str, argv: list[str], transport: Transport
) -> str:
    """Parse argv as an ``os-cli <command>`` command, run it, return stdout."""
    parser = ArgumentParser()
    module.add_subparser(parser.add_subparsers(dest="subcommand"))
    args = parser.parse_args([command, *argv])
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        module.run(args, OpensearchClient(transport))
    return stdout.getvalue()
