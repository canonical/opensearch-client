# Functional tests

These run opensearch_client against a real OpenSearch cluster; the unit tests
(`tests/unit/`) use fakes and need nothing running.

`run.sh` starts an ephemeral single-node OpenSearch via Docker Compose, waits
for it, runs the tests through `tox`, and tears everything down:

```sh
tests/functional/run.sh
```

The tests read the connection from `OPENSEARCH_URL` / `OPENSEARCH_USER` /
`OPENSEARCH_PASSWORD` and skip when those are unset, so `tox` without a cluster
stays green.

Requires: docker (with the compose plugin), curl, openssl, and tox.
