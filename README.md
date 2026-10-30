# opensearch_client

OpenSearch client library, logging utilities, and CLI.

## Install

Install the package as you would any non-PyPi Python package. For example:

```sh
python3 -m pip install -e .
```

or

```sh
pipx install git+https://github.com/canonical/opensearch_client.git
```

Runtime dependencies: `requests`, `PyYAML`.

## Overview

`opensearch_client` provides:

- a Python library for interacting with an OpenSearch cluster (directly, or
  through a dashboard console proxy as a fallback);
- `OpensearchLogHandler`, a `logging` handler that indexes records; and
- the `os-cli` command-line tool

See [docs/](docs/) for additional information.

## Roadmap

The client covers document indexing, querying (including SQL and PPL), and
cluster/index administration. It connects either directly or through an
OpenSearch Dashboards proxy, with server-certificate verification in both modes.

For the full list of supported operations, see the API reference.

The following capabilities are planned but not yet implemented:

### Mutual TLS

Client-certificate authentication in the transports: a certificate and private
key supplied alongside the existing verify / CA-bundle options, for clusters
that require clients to present a certificate.

### Ingest pipelines

An optional pipeline name on the indexing operations (single and bulk), so
documents are routed through a named ingest pipeline as they are written.
