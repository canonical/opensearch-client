# SQL `_explain` and predicate push-down

`os-cli triage eliminate` turns a SQL `WHERE` predicate into the OpenSearch
query DSL it compiles to, so the same filter can be counted and then applied by
a single server-side `_update_by_query`. It recovers that DSL by asking the SQL
engine to *explain* the query and pulling the pushed-down search body out of the
plan. This note documents the `_explain` output that the parsing in
`opensearch_client.triage` (`where_to_dsl`, `_find_pushed_down_request`,
`_extract_json_object`) relies on.

## What `_explain` returns

`POST _plugins/_sql/_explain` with a body like:

```json
{ "query": "SELECT * FROM logs-* WHERE rule.level < 3" }
```

returns the query's execution plan, a tree of operators. When the predicate can
be pushed down to the search layer, one node in the tree is an index scan whose
`description.request` is the Java `toString()` of an `OpenSearchQueryRequest`.
That string is not itself JSON, but it embeds the real search body as JSON after
`sourceBuilder=`:

```json
{
  "root": {
    "name": "ProjectOperator",
    "description": {
      "fields": "[rule.level, @timestamp, message]"
    },
    "children": [
      {
        "name": "OpenSearchIndexScan",
        "description": {
          "request": "OpenSearchQueryRequest(indexName=logs-*, sourceBuilder={\"from\":0,\"size\":200,\"timeout\":\"1m\",\"query\":{\"range\":{\"rule.level\":{\"from\":null,\"to\":3,\"include_lower\":true,\"include_upper\":false,\"boost\":1.0}}},\"_source\":{\"includes\":[\"rule.level\",\"@timestamp\",\"message\"],\"excludes\":[]}}, searchDone=false)"
        },
        "children": []
      }
    ]
  }
}
```

The DSL equivalent of `rule.level < 3` is the `query` object inside
`sourceBuilder`:

```json
{ "range": { "rule.level": { "from": null, "to": 3, "include_lower": true, "include_upper": false } } }
```

## How the parsing extracts it

Three functions cooperate to pull that `query` object out:

- `_find_pushed_down_request` walks the plan tree (the shape varies by query, so
  it recurses through every dict and list) and returns the first
  `description.request` string that contains `sourceBuilder=`.
- `_extract_json_object` returns the balanced `{...}` that begins right after
  `sourceBuilder=`. It counts braces while respecting JSON string literals,
  because two things would otherwise break a naive "find the next `}`" scan: the
  embedded body can contain `}` inside string values, and the whole
  `OpenSearchQueryRequest(...)` wrapper around it is not JSON.
- `where_to_dsl` runs `SELECT * FROM <index> WHERE <predicate>` through
  `_explain`, applies the two helpers above, JSON-parses the extracted object,
  and returns its `query` field.

## When a predicate does not push down

Some predicates are computed in the SQL engine rather than translated into a
filter (for example, expressions over unmapped fields or functions the search
layer cannot evaluate). Their plans contain no `OpenSearchQueryRequest` /
`sourceBuilder`, so `where_to_dsl` raises. `eliminate` then refuses rather than
risk tagging the wrong set of documents; simplify the predicate to a plain
boolean expression over mapped fields and try again.
