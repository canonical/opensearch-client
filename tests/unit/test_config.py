# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for opensearch_client.config.client_from_env.

Transport selection is driven by which OPENSEARCH_* variables are set. Each test
sets an explicit, isolated environment and restores it afterward.
"""

import contextlib
import os
from typing import Iterator

from opensearch_client.client import OpensearchClient
from opensearch_client.config import client_from_env
from opensearch_client.transport import (
    FailoverTransport,
    ProbeTransport,
    ProxyTransport,
)

_OS_KEYS = [
    "OPENSEARCH_URL",
    "OPENSEARCH_HOST",
    "OPENSEARCH_PORT",
    "OPENSEARCH_DASHBOARD_URL",
    "OPENSEARCH_DASHBOARD_HOST",
    "OPENSEARCH_DASHBOARD_PORT",
    "OPENSEARCH_USER",
    "OPENSEARCH_PASSWORD",
    "OPENSEARCH_CA_CERT",
    "OPENSEARCH_INSECURE",
    "OPENSEARCH_INDEX",
]

_CREDS = {"OPENSEARCH_USER": "u", "OPENSEARCH_PASSWORD": "p"}


@contextlib.contextmanager
def env(**values: str) -> Iterator[None]:
    """Run with exactly the given OPENSEARCH_* vars set, restoring the prior ones."""
    saved = {key: os.environ.get(key) for key in _OS_KEYS}
    for key in _OS_KEYS:
        os.environ.pop(key, None)
    os.environ.update(values)
    try:
        yield
    finally:
        for key in _OS_KEYS:
            os.environ.pop(key, None)
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value


def _built(**env_vars: str) -> OpensearchClient:
    """Build a client under the given vars plus credentials; assert it exists."""
    with env(**_CREDS, **env_vars):
        client = client_from_env()
    assert client is not None
    return client


def test_transport_is_chosen_by_which_variables_are_set() -> None:
    # (endpoint variables, expected transport)
    cases = [
        ({"OPENSEARCH_URL": "https://h:9200"}, ProbeTransport),
        ({"OPENSEARCH_HOST": "node1"}, ProbeTransport),
        ({"OPENSEARCH_DASHBOARD_URL": "https://dash"}, ProxyTransport),
        ({"OPENSEARCH_DASHBOARD_HOST": "dash"}, ProxyTransport),
        (
            {
                "OPENSEARCH_URL": "https://h:9200",
                "OPENSEARCH_DASHBOARD_URL": "https://dash",
            },
            FailoverTransport,
        ),
    ]
    for endpoint, expected in cases:
        assert isinstance(_built(OPENSEARCH_INDEX="x", **endpoint)._transport, expected)


def test_host_endpoints_use_default_ports() -> None:
    direct = _built(OPENSEARCH_HOST="node1", OPENSEARCH_INDEX="x")._transport
    assert isinstance(direct, ProbeTransport)
    assert direct.direct.base_url == "https://node1:9200"
    proxy = _built(OPENSEARCH_DASHBOARD_HOST="dash", OPENSEARCH_INDEX="x")._transport
    assert isinstance(proxy, ProxyTransport)
    assert proxy.base_url == "https://dash:5601"


def test_verify_reflects_insecure_and_ca_cert() -> None:
    insecure = _built(
        OPENSEARCH_DASHBOARD_URL="https://d", OPENSEARCH_INSECURE="1"
    )._transport
    assert isinstance(insecure, ProxyTransport)
    assert insecure.verify is False
    ca = _built(
        OPENSEARCH_DASHBOARD_URL="https://d", OPENSEARCH_CA_CERT="/ca.pem"
    )._transport
    assert isinstance(ca, ProxyTransport)
    assert ca.verify == "/ca.pem"
    # The insecure=True argument (the CLI --insecure flag) forces it off too.
    with env(**_CREDS, OPENSEARCH_DASHBOARD_URL="https://d"):
        by_arg = client_from_env(insecure=True)
    assert by_arg is not None
    assert isinstance(by_arg._transport, ProxyTransport)
    assert by_arg._transport.verify is False


def test_returns_none_without_credentials_or_endpoint() -> None:
    with env(OPENSEARCH_URL="https://h:9200"):  # endpoint but no credentials
        assert client_from_env() is None
    with env(**_CREDS):  # credentials but no endpoint
        assert client_from_env() is None


def test_index_defaults_to_star_when_unset() -> None:
    assert _built(OPENSEARCH_URL="https://h:9200").default_index == "*"
