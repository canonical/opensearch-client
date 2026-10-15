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
from opensearch_client.result import Failure, OpensearchResult, Success
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


class FakeSecurityApi:
    """Stands in for a cluster's Security API: holds objects and answers by path.

    A GET returns the object stored at the path, a PUT stores the body there, and
    a DELETE removes it; a path with nothing stored answers 404. Any other path is
    an index, which exists if it is in ``indices``.

    Attributes:
        objects (dict[str, Any]): every stored request body, by API path.
        indices (set[str]): the indexes that exist.
        denied (bool): answer every request with 403, as for a non-admin login.
        calls (list[tuple[str, str, Any]]): every request, in order, with its
            decoded body.
    """

    def __init__(self, indices: tuple[str, ...] = ()) -> None:
        self.objects: dict[str, Any] = {}
        self.indices = set(indices)
        self.denied = False
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
        if self.denied:
            return Failure(f"403 from {method}", status=403)
        if not path.startswith("_plugins/_security/api/"):
            if method == "GET" and path in self.indices:
                return Success({path: {}})
            return Failure(f"404 from {method}", status=404)
        if method == "PUT":
            self.objects[path] = decoded
            return Success({"status": "CREATED"})
        if path not in self.objects:
            return Failure(f"404 from {method}", status=404)
        if method == "DELETE":
            del self.objects[path]
            return Success({"status": "OK"})
        return Success({path.rsplit("/", 1)[1]: self.objects[path]})


def run_cli(
    module: ModuleType, command: str, argv: list[str], transport: Transport
) -> str:
    """Parse argv as an ``opensearch_client <command>`` command, run it, return stdout."""
    parser = ArgumentParser()
    module.add_subparser(parser.add_subparsers(dest="subcommand"))
    args = parser.parse_args([command, *argv])
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        module.run(args, OpensearchClient(transport))
    return stdout.getvalue()
