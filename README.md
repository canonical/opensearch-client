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

## Development

All development, test and CI dependencies are locked in `uv.lock`, including
transitive dependencies. `uv sync` and tox install the exact versions and file
hashes in the lock, so local runs and CI get the same packages.

You need `uv` 0.13.4 or newer.

Set up a local environment with the package and all development tools:

```sh
uv sync
```

By default, `uv sync` installs the `dev` group, which includes every other group
in `pyproject.toml`.

To install a single dependency group instead, for example only the lint tools:

```sh
uv sync --only-group lint
```

To install the package with one group, for example to run the unit tests:

```sh
uv sync --no-default-groups --group unit
```

Without `--no-default-groups`, uv also installs the `dev` group.

`uv sync` creates the virtual environment in `.venv`. Activate it with
`source .venv/bin/activate`, or `source .venv/bin/activate.fish` in fish. Run
`deactivate` to leave it. You can also run a command in it without activating,
for example `uv run pytest`.

Run the checks the same way CI does:

```sh
tox
```

### Changing dependencies

1. Edit `pyproject.toml`, or use `uv add`.
1. Run `uv lock`.
1. Commit `pyproject.toml` and `uv.lock` together.

CI fails if `uv.lock` does not match `pyproject.toml`. Do not edit `uv.lock` by
hand. Renovate opens a pull request each week to refresh the lock.

### New releases

`uv` does not lock any release that is less than seven days old (`exclude-newer`
in `pyproject.toml`). This gives time for a bad or malicious release to be found
and pulled before we use it.

To take an urgent fix sooner:

1. Add `exclude-newer-package = { <package> = false }` under `[tool.uv]`.
1. Run `uv lock --upgrade-package <package>`.
1. Once the release is more than seven days old, remove the exemption and run
   `uv lock`. Only the exemption should change in `uv.lock`, not any package
   versions.
