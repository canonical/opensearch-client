# Library

## Return types

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

`OpensearchClient(transport, default_index="*")` runs over a transport. Every
method returns an `OpensearchResult`.

- `request(method, path, body=None, timeout=30)`: the core primitive the helpers
  build on.
- Reads: `get(path)`, `search(query)`, `search_raw(query)` (full response, with
  aggregations), `count(query)`, `sql(q)`, `sql_raw(q)` (raw jdbc), `ppl(q)`,
  `explain(q)`, `get_mapping()`, `field_mapping(field)`, `opensearch_version()`,
  `plugin_versions()`, `get_pipeline(name=None)`, `get_index_template(name=None)`,
  `get_legacy_template(name=None)`, `simulate_template(name, body)`,
  `simulate_index(index, timeout=120)`, `get_component_template(name=None)`.
- Writes / admin: `index_document(document)`, `bulk(documents)`,
  `create_index(body)`, `put_mapping(mapping)`, `put_pipeline(name, body)`,
  `put_index_template(name, body)`, `delete_index_template(name)`,
  `put_component_template(name, body)`, `delete_component_template(name)`,
  `rollover(alias, settings=None, dry_run=False, timeout=120)`,
  `reindex(source, dest)`, `update_by_query(query)`, `get_task(task_id)`.

`bulk(documents, index=None, *, action="index", max_bytes=..., max_retries=3)`
writes many documents through the `_bulk` API. It serializes them to
newline-delimited JSON, sends them in batches bounded by `max_bytes`, halves a
batch that comes back `413`, and inspects each item's own result rather than
trusting the bulk request's `200`. Any failed document, whether from a failed
batch or a per-item error, is retried up to `max_retries` times (`0` disables
retries). The run is summarized as `indexed` / `failed` document counts, the
number of `batches` sent, and a `failures` list (each with the offending
`document` and its error). The result is a `Success` only if every document was
indexed; if any failed, it is a `Failure`. (`data` still contains the summary.)

Index-scoped helpers (`search`, `count`, `create_index`, `update_by_query`,
`get_mapping`, `field_mapping`, ...) target `default_index` unless you pass an
explicit `index=`:

```python
client.search(query)                  # uses default_index
client.search(query, index="logs-*")  # overrides it for this call
```

Helpers whose target index is carried elsewhere take no `index` argument:
`sql`/`ppl`/`explain` embed it in the query text, and `reindex` names source and
destination in the body.

## Index templates

An index template fixes the settings and mappings of indices created after it is
put; existing indices keep their mappings, and a field's type cannot be changed
once set. Without one, OpenSearch types each new field from the first document
that carries it, so one source writing a field as a string can cause every later
document writing it as an object to be rejected.

Composable templates (`_index_template`) replace legacy ones (`_template`): when
any composable template matches a new index, every legacy template is ignored for
it, not merged. A composable template built to extend a legacy one must therefore
carry its full mappings. The client reads legacy templates but does not write
them.

Component templates hold reusable settings and mappings that index templates
list in `composed_of`. They are merged in that order, then the index template's
own `template` on top: a field defined twice takes the later definition, and
dynamic templates are concatenated, so the first match in `composed_of` order
wins. A component must exist before an index template composes it, and cannot be
deleted while one still does.

Preview before and after applying:

```python
candidate = {"index_patterns": ["logs-*"], "priority": 500, "template": {...}}
client.simulate_template("logs", candidate)  # what a matching index would get
client.put_index_template("logs", candidate)
client.simulate_index("logs-2026.41")        # what this index gets, all templates applied
```

## Transports

The client reaches an endpoint one of two ways:

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

## Building from the environment

`client_from_env()` assembles a client from the `OPENSEARCH_*` variables:

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
