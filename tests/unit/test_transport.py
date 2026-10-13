# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for opensearch_client.transport.

Each transport builds a request and turns the response into a result. Tests
replace the transport's ``session`` with an explicit recorder (no magic fixtures).
The failover/probe branches and error mapping are not reachable via the functional
tests, which use a single working direct connection.
"""

from typing import Any

import requests

from opensearch_client.transport import (
    DirectTransport,
    FailoverTransport,
    ProbeTransport,
    ProxyTransport,
)


class FakeResponse:
    """A minimal stand-in for requests.Response."""

    def __init__(self, status_code: int, payload: Any, text: str = "") -> None:
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.text = text
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    """A fake requests.Session: records the call and returns (or raises) a preset."""

    def __init__(self, response: Any) -> None:
        self.auth: Any = None
        self.headers: dict[str, str] = {}
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kw: Any) -> Any:
        self.calls.append({"method": method, "url": url, **kw})
        return self._fire()

    def post(self, url: str, **kw: Any) -> Any:
        self.calls.append({"url": url, **kw})
        return self._fire()

    def _fire(self) -> Any:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _stub(transport: Any, response: Any) -> FakeSession:
    transport.session = FakeSession(response)
    return transport.session


def test_direct_transport_builds_the_rest_request() -> None:
    transport = DirectTransport("https://host:9200/", ("u", "p"), True)
    session = _stub(transport, FakeResponse(200, {"ok": 1}))
    got = transport.request("GET", "logs-*/_search", b'{"q": 1}', timeout=10)
    assert got.data == {"ok": 1}
    assert session.calls[0] == {
        "method": "GET",
        "url": "https://host:9200/logs-*/_search",
        "data": b'{"q": 1}',
        "headers": {"Content-Type": "application/json"},
        "verify": True,
        "timeout": 10,
    }


def test_proxy_transport_tunnels_and_sets_xsrf() -> None:
    transport = ProxyTransport("https://dash/", ("u", "p"), True)
    assert transport.session.headers["osd-xsrf"] == "true"  # set at construction
    session = _stub(transport, FakeResponse(200, {"ok": 1}))
    transport.request("PUT", "my-index", b'{"m": {}}')
    call = session.calls[0]
    assert call["url"] == "https://dash/api/console/proxy"
    assert call["params"] == {"path": "my-index", "method": "PUT"}
    assert call["data"] == b'{"m": {}}'
    assert call["headers"] == {"Content-Type": "application/json"}


def test_request_turns_failures_into_reasons() -> None:
    # (session response, fragments the reason must contain)
    cases = [
        (FakeResponse(404, None, text="nope"), ["404", "GET missing", "nope"]),
        (requests.ConnectionError("refused"), ["refused"]),
        (FakeResponse(200, ValueError("bad")), ["invalid JSON"]),
    ]
    for response, fragments in cases:
        transport = DirectTransport("https://h:9200", ("u", "p"), True)
        _stub(transport, response)
        res = transport.request("GET", "missing")
        assert not res
        assert all(f in res.reason for f in fragments)


def test_transport_sends_the_body_with_its_content_type() -> None:
    transport = DirectTransport("https://h:9200", ("u", "p"), True)
    session = _stub(transport, FakeResponse(200, {"ok": 1}))
    transport.request(
        "POST", "_bulk", b"line1\nline2\n", content_type="application/x-ndjson"
    )
    assert session.calls[0]["data"] == b"line1\nline2\n"
    assert session.calls[0]["headers"] == {"Content-Type": "application/x-ndjson"}


def test_a_bodyless_request_sends_no_content_type() -> None:
    transport = DirectTransport("https://h:9200", ("u", "p"), True)
    session = _stub(transport, FakeResponse(200, {}))
    transport.request("GET", "logs-*/_search")
    assert session.calls[0]["data"] is None
    assert session.calls[0]["headers"] == {}


def test_http_error_failure_carries_status_and_error_body() -> None:
    transport = DirectTransport("https://h:9200", ("u", "p"), True)
    body = {"error": {"type": "index_not_found_exception"}, "status": 404}
    _stub(transport, FakeResponse(404, body, text="nope"))
    res = transport.request("GET", "missing")
    assert res.status == 404
    assert res.data == body  # the parsed error response body
    # A transport error carries neither a status nor a body.
    _stub(transport, requests.ConnectionError("down"))
    down = transport.request("GET", "x")
    assert down.status is None and down.data is None


def _failover(primary: Any, fallback: Any) -> tuple:
    p = DirectTransport("https://direct:9200", ("u", "p"), True)
    f = ProxyTransport("https://dash", ("u", "p"), True)
    _stub(p, primary)
    return FailoverTransport(p, f), _stub(f, fallback)


def test_failover_retries_only_on_a_transport_error() -> None:
    # Unreachable primary -> fall back to the proxy.
    ft, fs = _failover(
        requests.ConnectionError("down"), FakeResponse(200, {"via": "proxy"})
    )
    assert ft.request("GET", "x").data == {"via": "proxy"}
    assert fs.calls
    # An HTTP error is a real answer -> returned as-is, proxy untouched.
    ft, fs = _failover(FakeResponse(500, None, text="boom"), FakeResponse(200, {}))
    res = ft.request("GET", "x")
    assert not res and "500" in res.reason and fs.calls == []
    # Both unreachable -> both reasons reported.
    ft, _ = _failover(requests.ConnectionError("d"), requests.ConnectionError("p"))
    res = ft.request("GET", "x")
    assert not res and "d" in res.reason and "p" in res.reason


def _probe(direct: Any, proxy: Any) -> tuple:
    d = DirectTransport("https://e:9200", ("u", "p"), True)
    p = ProxyTransport("https://e:9200", ("u", "p"), True)
    return ProbeTransport(d, p), _stub(d, direct), _stub(p, proxy)


def test_probe_caches_the_answering_transport() -> None:
    # Direct answers -> used for both requests, proxy never tried.
    pt, ds, ps = _probe(
        FakeResponse(200, {"via": "direct"}), FakeResponse(200, {"via": "proxy"})
    )
    assert (
        pt.request("GET", "x").data == pt.request("GET", "x").data == {"via": "direct"}
    )
    assert len(ds.calls) == 2 and ps.calls == []
    # Direct fails (dashboard 404) -> proxy used and cached (direct tried once).
    pt, ds, ps = _probe(
        FakeResponse(404, None, text="no"), FakeResponse(200, {"via": "proxy"})
    )
    assert (
        pt.request("GET", "x").data == pt.request("GET", "x").data == {"via": "proxy"}
    )
    assert len(ds.calls) == 1 and len(ps.calls) == 2
    # Both fail -> nothing cached, so a second request probes again.
    pt, ds, ps = _probe(
        FakeResponse(500, None, text="d"), FakeResponse(502, None, text="p")
    )
    res = pt.request("GET", "x")
    assert not res and "d" in res.reason and "p" in res.reason
    pt.request("GET", "x")
    assert len(ds.calls) == 2 and len(ps.calls) == 2
