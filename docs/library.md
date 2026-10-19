# Library

Every function exposed by `os-cli` is also exported by the `opensearch_client`
library.

In addition to those functions, `opensearch_client` offers a log handler and
associated utilities to be used with Python's `logging` module. For more
information, see [Log handler](log-handler.md).

## Implementation details

### Return types

Every call returns an `OpensearchResult` rather than raising. Each is a
`Success` or a `Failure`, read as one union type with the fields `ok` / `data` /
`reason` / `status`. An HTTP error, transport error, or bad response body comes
back as a `Failure` with a `reason`. For each field's exact meaning, see the
`Success` and `Failure` docstrings in `opensearch_client.result`.

A result is truthy when it succeeded, so `if res:` (or `if not res:`,
`assert res`) both branches and narrows `data` to be present:

```python
from opensearch_client import OpensearchClient, client_from_env

client = client_from_env()   # reads OPENSEARCH_* (or None if unset)
res = client.sql("SELECT rule.level FROM logs-* LIMIT 5")
if res:
    for row in res.data:     # data narrowed to the success type
        print(row)
else:
    print("query failed:", res.reason)
```

You can also pattern-match, which narrows the same way:

```python
from opensearch_client import Success, Failure

match client.sql("SELECT rule.level FROM logs-* LIMIT 5"):
    case Success(rows):
        ...
    case Failure(reason):
        ...
```

(Note: a plain `if res.ok:` works at runtime but does not narrow `data` for a
type checker; use `if res:` or the `match` form when you access `res.data`.)

### Transports

`opensearch_client` reaches an OpenSearch endpoint one of two ways:

- `DirectTransport(base_url, auth, verify)`: the OpenSearch REST API directly
  (`https://host:9200/<path>`).
- `ProxyTransport(base_url, auth, verify)`: an OpenSearch Dashboards console
  proxy (`POST /api/console/proxy`), for clusters only reachable that way.

Two transports compose the two when the endpoint type is uncertain:

- `ProbeTransport(direct, proxy)`: one endpoint of unknown type, where `direct`
  and `proxy` point at the same base URL. On the first request it tries the
  direct transport and, if that request fails for any reason (including the HTTP
  error a dashboard returns to a direct-style path), retries through the proxy.
  Whichever answers is cached and reused for every later request.
- `FailoverTransport(primary, fallback)`: two distinct endpoints. It tries the
  primary and retries through the fallback only when the primary is unreachable
  (a transport error), not on an HTTP error status from a reachable server.

```python
from opensearch_client import OpensearchClient, DirectTransport

transport = DirectTransport("https://host:9200", ("user", "pass"), verify="/etc/ssl/ca.pem")
client = OpensearchClient(transport, default_index="logs-*")
```

## Using the library

### Client construction

`client_from_env` is the most common method of creating an `OpensearchClient`.
It assembles a client from `OPENSEARCH_*` environment variables:

| Variable                    | Purpose                                                          |
| --------------------------- | ---------------------------------------------------------------- |
| `OPENSEARCH_URL`            | endpoint URL of unknown type, e.g. `https://host:9200`           |
| `OPENSEARCH_HOST`           | endpoint host of unknown type (alternative to `OPENSEARCH_URL`)  |
| `OPENSEARCH_PORT`           | port for `OPENSEARCH_HOST` (default `9200`)                      |
| `OPENSEARCH_DASHBOARD_URL`  | dashboard proxy URL                                              |
| `OPENSEARCH_DASHBOARD_HOST` | dashboard proxy host (alternative to `OPENSEARCH_DASHBOARD_URL`) |
| `OPENSEARCH_DASHBOARD_PORT` | port for `OPENSEARCH_DASHBOARD_HOST` (default `5601`)            |
| `OPENSEARCH_USER`           | username (required)                                              |
| `OPENSEARCH_PASSWORD`       | password (required)                                              |
| `OPENSEARCH_CA_CERT`        | path to a CA bundle to verify the server certificate             |
| `OPENSEARCH_INSECURE`       | truthy to skip TLS verification entirely                         |
| `OPENSEARCH_INDEX`          | index pattern the query helpers target (default `*`)             |

At least one endpoint must be configured. The transport is chosen by which
variables are set:

- **only `OPENSEARCH_*`**: the endpoint type is unknown, so it is probed on the
  first request (direct, falling back to the proxy) and the result is
  remembered.
- **only `OPENSEARCH_DASHBOARD_*`**: the endpoint is a dashboard proxy.
- **both**: connect directly, falling back to the dashboard proxy when the
  direct endpoint is unreachable.
