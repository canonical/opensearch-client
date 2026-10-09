# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Build an OpensearchClient from the ``OPENSEARCH_*`` environment variables.

The core :class:`~opensearch_client.client.OpensearchClient` takes an explicit
transport; this helper assembles one from the environment. Two families of
connection variables name an endpoint:

    OPENSEARCH_URL / OPENSEARCH_HOST[:OPENSEARCH_PORT]
        an endpoint whose type (direct cluster or dashboard proxy) is unknown

    OPENSEARCH_DASHBOARD_URL / OPENSEARCH_DASHBOARD_HOST[:OPENSEARCH_DASHBOARD_PORT]
        an endpoint known to be a dashboard console proxy

The transport is chosen by which family is set:

    only OPENSEARCH_*            probe the endpoint on the first request (try a
                                 direct connection, fall back to the proxy) and
                                 remember what answered
    only OPENSEARCH_DASHBOARD_*  use the dashboard proxy
    both                         connect directly, falling back to the dashboard
                                 proxy when the direct endpoint is unreachable

Other variables:

    OPENSEARCH_USER       username (required)
    OPENSEARCH_PASSWORD   password (required)
    OPENSEARCH_CA_CERT    path to a CA bundle to verify the server certificate
    OPENSEARCH_INSECURE   truthy to skip TLS verification entirely
    OPENSEARCH_INDEX      index pattern the query helpers target (default ``*``)
"""

import logging
import os
import sys

import urllib3

from opensearch_client.client import DEFAULT_INDEX, OpensearchClient
from opensearch_client.transport import (
    DirectTransport,
    FailoverTransport,
    ProbeTransport,
    ProxyTransport,
    Transport,
)

_DEFAULT_DIRECT_PORT = "9200"
_DEFAULT_DASHBOARD_PORT = "5601"


def _env_flag(name: str) -> bool:
    """Whether an environment variable is set to a truthy value."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _endpoint(
    url_var: str, host_var: str, port_var: str, default_port: str
) -> str | None:
    """Get a base URL from the environment, or None if no endpoint is set.

    The URL variable wins; otherwise the host variable (with the port variable or
    the default port) is used to build one.

    Args:
        url_var: the name of the variable holding a full URL.
        host_var: the name of the variable holding a host.
        port_var: the name of the variable holding the port for the host.
        default_port: the port to use when ``port_var`` is unset.

    Returns:
        The base URL, or None.
    """
    url = os.environ.get(url_var)
    if url:
        return url
    host = os.environ.get(host_var)
    if host:
        return f"https://{host}:{os.environ.get(port_var, default_port)}"
    return None


def _index() -> str:
    """Get the configured index pattern, or the default ``*`` with a warning.

    Returns:
        The ``OPENSEARCH_INDEX`` value, or ``*`` if it is unset.
    """
    index = os.environ.get("OPENSEARCH_INDEX")
    if index:
        return index
    logging.warning(
        "no OPENSEARCH_INDEX set; defaulting to '%s' (all indices). Prefer an "
        "explicit index pattern.",
        DEFAULT_INDEX,
    )
    return DEFAULT_INDEX


def client_from_env(insecure: bool = False) -> OpensearchClient | None:
    """Build a client from the ``OPENSEARCH_*`` environment variables.

    Args:
        insecure (bool): if True, skip TLS certificate verification, the same as
            setting ``OPENSEARCH_INSECURE``; either turning it on is enough.

    Returns:
        OpensearchClient | None: the configured client, or None (after printing an
        error to stderr) if credentials or a usable connection are missing.
    """
    user = os.environ.get("OPENSEARCH_USER")
    password = os.environ.get("OPENSEARCH_PASSWORD")
    if not (user and password):
        print(
            "error: set OPENSEARCH_USER and OPENSEARCH_PASSWORD in the environment",
            file=sys.stderr,
        )
        return None
    auth = (user, password)

    if insecure or _env_flag("OPENSEARCH_INSECURE"):
        urllib3.disable_warnings()
        verify: bool | str = False
    else:
        verify = os.environ.get("OPENSEARCH_CA_CERT") or True

    unknown_url = _endpoint(
        "OPENSEARCH_URL", "OPENSEARCH_HOST", "OPENSEARCH_PORT", _DEFAULT_DIRECT_PORT
    )
    dashboard_url = _endpoint(
        "OPENSEARCH_DASHBOARD_URL",
        "OPENSEARCH_DASHBOARD_HOST",
        "OPENSEARCH_DASHBOARD_PORT",
        _DEFAULT_DASHBOARD_PORT,
    )

    # The transport is chosen by which endpoints are configured (see module docs).
    if unknown_url and dashboard_url:
        # Two distinct endpoints: connect directly, fall back to the proxy.
        transport: Transport = FailoverTransport(
            DirectTransport(unknown_url, auth, verify),
            ProxyTransport(dashboard_url, auth, verify),
        )
    elif dashboard_url:
        # Only dashboard variables: the endpoint is a proxy.
        transport = ProxyTransport(dashboard_url, auth, verify)
    elif unknown_url:
        # One endpoint of unknown type: probe direct, fall back to proxy, remember.
        transport = ProbeTransport(
            DirectTransport(unknown_url, auth, verify),
            ProxyTransport(unknown_url, auth, verify),
        )
    else:
        print(
            "error: configure a connection: OPENSEARCH_URL or OPENSEARCH_HOST "
            "(or the OPENSEARCH_DASHBOARD_* equivalents)",
            file=sys.stderr,
        )
        return None

    return OpensearchClient(transport, _index())
