# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""HTTP transports that carry a request to an OpenSearch cluster.

Two ways to reach a cluster, behind one ``request`` interface:

- :class:`DirectTransport`: talk to the OpenSearch REST API directly
  (``https://host:9200/<path>``). This is the primary transport.
- :class:`ProxyTransport`: tunnel through an OpenSearch Dashboards console proxy
  (``POST /api/console/proxy?path=&method=``), for clusters only reachable that
  way. This is the fallback transport.

:class:`FailoverTransport` composes the two: it tries the primary and, only when
the primary is unreachable (a transport error, not an HTTP error status from a
reachable server), retries the same request through the fallback.

Every ``request`` returns an :class:`~opensearch_client.result.OpensearchResult` rather
than raising: a transport error, a non-ok status, or an unparsable body all come
back as ``ok=False`` with a reason.
"""

import logging
from typing import Any, Protocol

import requests

from opensearch_client.result import Failure, OpensearchResult, Success

PROXY_PATH = "/api/console/proxy"
REQUEST_TIMEOUT = 30


class Transport(Protocol):
    """Carry an already-encoded request body to a cluster and return a result.

    The body is bytes (or None); ``content_type`` is the header to send with it.
    A transport does no serialization: the caller (the client) encodes JSON or
    NDJSON and names the content type. A transport's job is purely to reach the
    cluster (topology, auth, TLS, failover) and normalize the outcome.
    """

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = ...,
        content_type: str = ...,
        timeout: int = ...,
    ) -> OpensearchResult[Any]: ...


def _result_from_response(
    response: requests.Response, method: str, path: str
) -> OpensearchResult[Any]:
    """Turn a received HTTP response into a result (non-ok status is a value)."""
    if not response.ok:
        try:
            body = response.json()
        except ValueError:
            body = None  # a non-JSON error body (e.g. an HTML proxy page)
        return Failure(
            f"{response.status_code} from {method} {path}: {response.text}",
            data=body,
            status=response.status_code,
        )
    try:
        return Success(response.json())
    except ValueError as e:
        return Failure(f"invalid JSON in response: {e}")


def _headers(body: bytes | None, content_type: str) -> dict[str, str]:
    """The Content-Type header for a request, only when it carries a body."""
    return {"Content-Type": content_type} if body is not None else {}


class _HttpTransport:
    """Shared session setup and the send/request split the failover logic needs.

    ``send`` performs the HTTP call and may raise ``requests.RequestException`` on
    a transport error (an unreachable endpoint); ``request`` wraps that into a
    result. Keeping ``send`` raise-capable is what lets FailoverTransport tell a
    connection failure (retry elsewhere) from an HTTP error status (a real answer).
    """

    def __init__(
        self, base_url: str, auth: tuple[str, str], verify: bool | str
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.verify = verify
        self.session = requests.Session()
        self.session.auth = auth

    def send(
        self,
        method: str,
        path: str,
        body: bytes | None,
        content_type: str,
        timeout: int,
    ) -> requests.Response:
        raise NotImplementedError

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: str = "application/json",
        timeout: int = REQUEST_TIMEOUT,
    ) -> OpensearchResult[Any]:
        try:
            response = self.send(method, path, body, content_type, timeout)
        except requests.RequestException as e:
            return Failure(str(e))
        return _result_from_response(response, method, path)


class DirectTransport(_HttpTransport):
    """Talk to the OpenSearch REST API directly at ``base_url`` (e.g. port 9200)."""

    def send(
        self,
        method: str,
        path: str,
        body: bytes | None,
        content_type: str,
        timeout: int,
    ) -> requests.Response:
        return self.session.request(
            method,
            f"{self.base_url}/{path}",
            data=body,
            headers=_headers(body, content_type),
            verify=self.verify,
            timeout=timeout,
        )


class ProxyTransport(_HttpTransport):
    """Tunnel through an OpenSearch Dashboards console proxy at ``base_url``.

    The proxy always receives a POST from us; the real method and path are passed
    as query parameters. The ``osd-xsrf`` header is required by the dashboard.
    """

    def __init__(
        self, base_url: str, auth: tuple[str, str], verify: bool | str
    ) -> None:
        super().__init__(base_url, auth, verify)
        self.session.headers.update({"osd-xsrf": "true"})

    def send(
        self,
        method: str,
        path: str,
        body: bytes | None,
        content_type: str,
        timeout: int,
    ) -> requests.Response:
        return self.session.post(
            f"{self.base_url}{PROXY_PATH}",
            params={"path": path, "method": method},
            data=body,
            headers=_headers(body, content_type),
            verify=self.verify,
            timeout=timeout,
        )


class FailoverTransport:
    """Try the primary transport, fall back to the secondary on a transport error.

    For when two distinct endpoints are configured (a direct cluster and a
    separate dashboard proxy). Only an unreachable primary
    (``requests.RequestException``) triggers the fallback; an HTTP error status is
    a real answer from a reachable server, so it is returned as-is and never
    retried elsewhere.
    """

    def __init__(self, primary: _HttpTransport, fallback: _HttpTransport) -> None:
        self.primary = primary
        self.fallback = fallback

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: str = "application/json",
        timeout: int = REQUEST_TIMEOUT,
    ) -> OpensearchResult[Any]:
        try:
            response = self.primary.send(method, path, body, content_type, timeout)
        except requests.RequestException as primary_error:
            logging.warning(
                "primary transport failed (%s); falling back to the proxy",
                primary_error,
            )
            try:
                response = self.fallback.send(method, path, body, content_type, timeout)
            except requests.RequestException as fallback_error:
                return Failure(
                    f"primary transport failed ({primary_error}); "
                    f"fallback transport failed ({fallback_error})"
                )
        return _result_from_response(response, method, path)


class ProbeTransport:
    """A single endpoint of unknown type: detect direct vs proxy, then remember it.

    For when only one endpoint is configured and the caller does not know whether
    it is a direct OpenSearch cluster or a dashboard console proxy. On the first
    request it tries the direct transport; if that request fails for any reason (a
    transport error, or an HTTP error such as the 404 a dashboard returns to a
    direct-style path), it retries through the proxy transport against the same
    endpoint. Whichever answers is cached and used for every later request, so the
    probe happens at most once.

    Both transports point at the same base URL. A first request that fails on both
    is returned as a failure and nothing is cached, so a later request re-probes.
    """

    def __init__(self, direct: _HttpTransport, proxy: _HttpTransport) -> None:
        self.direct = direct
        self.proxy = proxy
        self._chosen: _HttpTransport | None = None

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: str = "application/json",
        timeout: int = REQUEST_TIMEOUT,
    ) -> OpensearchResult[Any]:
        if self._chosen is not None:
            return self._chosen.request(method, path, body, content_type, timeout)

        direct_result = self.direct.request(method, path, body, content_type, timeout)
        if direct_result.ok:
            self._chosen = self.direct
            return direct_result

        logging.warning(
            "direct connection did not answer (%s); trying the dashboard proxy",
            direct_result.reason,
        )
        proxy_result = self.proxy.request(method, path, body, content_type, timeout)
        if proxy_result.ok:
            self._chosen = self.proxy
            return proxy_result

        return Failure(
            f"direct connection failed ({direct_result.reason}); "
            f"proxy connection failed ({proxy_result.reason})"
        )
