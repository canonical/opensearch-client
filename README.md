# opensearch_client

OpenSearch client library

## Install

Install the package as you would any non-PyPi Python package. For example:

```
python3 -m pip install -e .
```

or

```
pipx install git+https://github.com/canonical/opensearch_client.git
```

Runtime dependencies: `requests`, `PyYAML`.

## Overview

`opensearch_client` provides:

- a Python library for querying an OpenSearch cluster (directly, or through a
  dashboard console proxy as a fallback), where every call returns an
  `OpensearchResult` (`ok` / `data` / `reason`) rather than raising; and
- an `os-cli` command-line tool

## Documentation

- [Library](docs/library.md): the client API and connection helpers.
- [CLI](docs/cli.md): the CLI subcommands, including `query`, `search`, `index`,
  `cluster`, and `triage`.
- [SQL `_explain`](docs/sql-explain.md): the `_explain` output the triage
  predicate translation parses.

## Roadmap

The client currently covers single-document and bulk indexing, search, SQL and
PPL, counts, mappings, versions, index creation and lifecycle (refresh, delete,
exists, list, rollover), index templates, reindex, update-by-query, and task polling, over
a direct or dashboard-proxy transport with server-certificate verification. The
following capabilities are planned but not yet implemented:

### Mutual TLS

Client-certificate authentication in the transports: a certificate and private
key supplied alongside the existing verify / CA-bundle options, for clusters
that require clients to present a certificate.

### Ingest pipelines

An optional pipeline name on the indexing operations (single and bulk), so
documents are routed through a named ingest pipeline as they are written.

### ISM management

Get and set index state management (ISM) policies.
