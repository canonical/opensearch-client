# Triage

_See also: [Run a triage hunt](../how-to/triage-hunt.md),
[SQL `_explain` and predicate push-down](sql-explain.md)_

## What triage does

`triage` supports a layered-elimination threat hunt: instead of searching for a
needle in a haystack, eliminate the haystack.

The process:

- identify a set of documents you can explain (routine noise, known-good
  activity, an expected job, etc.)
- set them aside
- repeat until the remaining dataset can be reviewed manually

Each dataset you remove is labeled with a 'layer' number. Every document records
the layer that removed it, the query that selected it, and a short rationale.
That makes the hunt auditable: afterwards anyone can see exactly which logs were
eliminated, by what reasoning, and in what order.

## How it maps to OpenSearch

Each step runs through the client as an OpenSearch operation, all on a scratch
copy so the source data is never touched: the logs of interest are first copied
into a fresh, writable index with a server-side `_reindex`, and every copied
document is tagged `triage.layer: -1` (a sentinel meaning "untriaged").

The operations, mirroring the process above:

- identify a set by writing a SQL `WHERE` predicate, which the SQL engine's
  `_explain` turns into the query DSL the tool actually runs (see
  [SQL `_explain`](sql-explain.md))
- set it aside with a single `_update_by_query` that stamps the layer number,
  the predicate, and the rationale onto the matches, touching only documents
  still tagged `-1`
- repeat, reading back each pass's progress with a `size: 0` aggregation over
  `triage.layer`: how many documents are still `-1` versus how many sit in each
  eliminated layer

Because the layer, predicate, and rationale are written onto every eliminated
document, the audit trail the process promises lives in the data itself.

## How the tool performs those actions

The triage process is driven by subcommands:

- `init` creates the scratch index (with an explicit triage-field mapping) and
  runs the reindex, polling the async task to completion so a large copy does
  not hit a request timeout.
- `eliminate` translates your predicate, cross-checks the translation (see
  below), and, on `--apply`, runs the tagging `_update_by_query`, again polling
  to completion.
- `restore` undoes an elimination, returning the matching documents to untriaged
  and recording the undo on `triage.history`.
- `status` runs the aggregation and prints the per-layer breakdown.

Before tagging anything, `eliminate` cross-checks its work: it confirms the
translated DSL matches exactly as many documents as `SELECT COUNT(*)` for the
same predicate, and refuses if the two disagree, rather than risk tagging a set
the predicate did not mean. Without `--apply` it is a dry run that only reports
counts, the translated DSL, and a sample.

The triage index is always named explicitly with `--index` / `--dest`; it is
never taken from `OPENSEARCH_INDEX`, so a wildcard or the wrong index can never
be tagged by accident.
