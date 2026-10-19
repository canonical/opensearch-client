# CLI

The `opensearch_client` package offers a CLI entry point, `os-cli`, with
subcommands.

## Usage

Every invocation has the form:

```sh
os-cli <subcommand> [options]
```

The subcommands are:

- `query`: run a SQL, PPL, or raw-DSL query
- `search`: a term-lookup convenience
- `index`: index-level operations
- `cluster`: cluster-level inspection and resource management
- `triage`: the guided threat-hunt workflow

By default, results are printed as YAML on stdout; failures are reported on
stderr with a non-zero exit status.

The connection is resolved from the `OPENSEARCH_*` environment variables (see
[Library](../explanation/library.md)), so no credentials ever appear on the
command line.

The global `--insecure` flag skips TLS certificate verification. It is the
command-line equivalent of `OPENSEARCH_INSECURE=1` and goes before the
subcommand:

```sh
os-cli --insecure query sql "SELECT rule.level FROM logs-* LIMIT 5"
```

## Output format

Every command that prints data takes `--format`, one of `yaml` (the default),
`json`, `csv`, or `tsv`:

```sh
os-cli query sql "SELECT client_ip FROM logs-*" --format json | jq '.[].client_ip'
os-cli query sql "SELECT client_ip FROM logs-*" --format csv > stack.csv
os-cli query sql "SELECT client_ip FROM logs-*" --format tsv | column -t
```

The tabular formats (`csv`, `tsv`) render one record per row. Nested objects
become dotted column names (`source.ip`), and the header is the union of every
record's keys, with a blank cell where a record lacks one. A list value is
written into its cell as JSON. A result that is a single object (such as
`cluster versions` or a triage summary) is rendered as a one-row table.

## Time scoping

`query sql`, `query ppl`, `query dsl`, and `search` take `--since` (inclusive
lower bound) and `--until` (exclusive upper bound), a half-open window so
back-to-back ranges do not double-count the boundary. `--time-field` sets the
timestamp field the bounds apply to (defaults to `@timestamp`).

A bound is either an absolute time or a relative offset:

- absolute: `2026-03-14` or `2026-03-14T00:00:00`
- relative: `-24h`, `-7d` (units `s`/`m`/`h`/`d`/`w`), resolved against the
  current time

A relative offset starts with `-`, which the argument parser would read as a
flag, so pass it with `=`:

```sh
os-cli query sql "SELECT ..." --since=-24h
os-cli search source.ip=10.0.0.5 --since=-7d --count 20
os-cli query sql "SELECT ..." --since 2026-03-14T00:00:00 --until 2026-03-15T00:00:00
os-cli query dsl @rule.json --since=-3d --time-field event.created
```

How the bound is applied depends on the command:

- `search` and `query dsl` add a `range` filter to the query
- `query sql` sends the range as the SQL API's `filter` field, which OpenSearch
  ANDs with the query (the query text is left untouched)
- `query ppl` appends a `| where` stage bounding the time field

`--since` / `--until` cannot be combined with `--explain`, which shows the
query's plan and is not affected by a separate filter.

## `os-cli query`

`query` runs a query in a chosen language and prints the result as YAML. Each
`<query>` may be given literally, as `-` to read it from stdin, or as `@PATH` to
read it from a file.

Run a SQL query. `--explain` prints its execution plan (the pushed-down DSL)
instead of running it:

```sh
os-cli query sql "SELECT rule.level FROM logs-* LIMIT 5"
os-cli query sql --explain "SELECT * FROM logs-* WHERE rule.level < 3"
os-cli query sql @failed-logins.sql
os-cli query sql - <<'EOF'
SELECT client_ip, COUNT(*) FROM logs-*
WHERE `event.outcome` = 'failure'
GROUP BY client_ip
EOF
```

Run a PPL query; `--explain` works here too:

```sh
os-cli query ppl "source=logs-* | head 5"
os-cli query ppl --explain "source=logs-* | where rule.level > 10"
```

Run a raw query DSL: a bare query object, or a full `_search` body (detected by
a top-level `query` key). Add `--count-only` to return just the number of
matching documents, routed to `_count` (so `size`, `sort`, and any aggregations
in the body are ignored):

```sh
os-cli query dsl '{"query": {"bool": {"must": [{"term": {"event.action": "logon"}}]}}}'
os-cli query dsl @rules/compiled/lateral-movement.json
os-cli query dsl @rule.json --count-only
```

## `os-cli search`

`search` finds the newest documents matching a set of exact `field=value` terms,
ANDed together, most recent first. `--count` controls how many are returned;
`--count-only` prints just the number of matches (via `_count`). `--index`
overrides the configured `OPENSEARCH_INDEX` for the search.

```sh
os-cli search rule.id=5710 agent.name=web01 --count 3
os-cli search source.ip=10.0.0.5 --count-only
os-cli search source.ip=10.0.0.5 --index logs-2026.07.14
```

## `os-cli index`

`index` groups index-level operations. `mapping` shows the mapping for one or
more fields (comma-separated, wildcards allowed). An empty result means the
field is unmapped, and therefore cannot be queried by SQL or a term filter even
when it appears in a document's `_source`. `--index` overrides the configured
`OPENSEARCH_INDEX`:

```sh
os-cli index mapping "data.event.*"
os-cli index mapping source.ip --index logs-2026.07.14
```

`create` makes a new index from a settings/mappings body, and `set mapping` adds
or updates field mappings on an existing one (OpenSearch can add new fields but
not change an existing field's type). Both read the JSON body from `SOURCE`
(`@PATH`, `-`, or literal).

```sh
os-cli index create hunt-001 @index-body.json
os-cli index set mapping @mapping.json --index hunt-001
```

`bulk` writes many documents into `--index` in one pass. `SOURCE` is the
documents, given as `@PATH` (a file), `-` (stdin), or literally, and
`--input-format` says how to read it: `json` (an array of objects), `jsonl` (one
object per line), or `yaml` (a sequence of mappings). It prints the run summary
(`indexed` / `failed` counts and a `failures` list) and exits non-zero if any
document failed:

```sh
os-cli index bulk @events.jsonl --index hunt-001 --input-format jsonl
os-cli index bulk @events.yaml  --index hunt-001 --input-format yaml
os-cli index bulk - --index hunt-001 --input-format json < events.json
```

The lifecycle verbs cover the common index-management operations:

- `refresh` makes recent writes searchable (a write followed immediately by a
  query otherwise returns nothing).
- `exists` reports whether an index exists.
- `list` lists indices matching `--pattern` (default: all).
- `alias` shows the indices holding an alias (wildcards allowed) and each one's
  alias properties, such as `is_write_index`. It exits non-zero when no index
  holds the alias.
- `delete` removes indices, and is a dry run unless `--apply`. `--index` deletes
  one concrete index, while `--pattern` deletes every matching index. The
  pattern dry run lists exactly which indices would be deleted.
- `rollover` rolls a write alias over to a new index, and is a dry run unless
  `--apply`: the dry run reports the new index's name without creating it.
  `--settings SOURCE` (`@PATH`, `-`, or literal JSON) sets index settings for
  the new index only, overriding its template, for example to give one index a
  different shard count. Creating the index can take over 30 s on a slow
  cluster, so the call waits up to 120 s; if it still times out, check
  `os-cli index list` before retrying, because the rollover may have happened.

```sh
os-cli index refresh --index hunt-001
os-cli index exists  --index hunt-001
os-cli index list --pattern "triage-*"
os-cli index alias logs-write                       # which index holds the write alias
os-cli index delete  --index hunt-001 --apply
os-cli index delete  --pattern "scratch-*"          # dry run: lists the matches
os-cli index delete  --pattern "scratch-*" --apply
os-cli index rollover logs-write                    # dry run: names the new index
os-cli index rollover logs-write --apply --settings '{"index.number_of_shards": 1}'
```

## `os-cli cluster`

`cluster` groups cluster-level inspection and configuration. `versions` reports
the OpenSearch and installed-plugin versions:

```sh
os-cli cluster versions
```

`pipeline` shows ingest pipelines (all, or one by name), and `set pipeline`
creates or replaces one from a JSON definition read from `SOURCE` (`@PATH`, `-`,
or literal).

```sh
os-cli cluster pipeline
os-cli cluster pipeline web-logs
os-cli cluster set pipeline web-logs @pipeline.json
```

`template` shows composable index templates (all, or those matching a name;
`--legacy` shows `_template` ones instead). `set template` creates or replaces
one from `SOURCE`, and `delete template` removes one: a dry run by default,
reporting the index patterns it covers, until `--apply` is given. Both `delete`
commands take exactly one name; a name with a wildcard or a comma is refused,
because either could delete several templates. `simulate template` resolves what
putting a definition under a name would give a new index, without installing it,
and `simulate index` resolves what the installed templates would give a new
index. See [Index templates](../explanation/index-templates.md) for how
templates interact.

`component-template` shows component templates, `set component-template` creates
or replaces one from `SOURCE`, and `delete component-template` removes one: a
dry run by default, reporting the index templates that compose it, until
`--apply`. Put components before the index templates that compose them, and
delete them after.

```sh
os-cli cluster template
os-cli cluster template wazuh --legacy
os-cli cluster simulate template web-logs @template.json   # preview before applying
os-cli cluster set template web-logs @template.json
os-cli cluster simulate index web-logs-2026.41    # verify after applying
os-cli cluster delete template web-logs           # dry run
os-cli cluster delete template web-logs --apply
os-cli cluster component-template
os-cli cluster set component-template web-fields @web-fields.json
os-cli cluster delete component-template web-fields   # dry run: lists its users
```

## `os-cli triage`

The `triage` subcommand is covered in [Triage](../explanation/triage.md) and
[Run a triage hunt](../how-to/triage-hunt.md).
