# Run a triage hunt

_See also: [Triage](../explanation/triage.md),
[SQL `_explain` and predicate push-down](../explanation/sql-explain.md)_

## The workflow

Copy the logs of interest into a fresh index, tagging every document untriaged.
The source index is only read, never modified:

```sh
os-cli triage init --source logs-2026.07.14 --dest triage-hunt-001
```

Review the copied index (for example in a dashboard), pick a set you can
explain, and dry-run its elimination. `--where` takes a SQL `WHERE` predicate;
nothing is written without `--apply`. The dry run reports how many documents
match, how many are still untriaged (and so would be tagged), the translated
DSL, and a sample:

```sh
os-cli triage eliminate --index triage-hunt-001 --layer 1 --where "rule.level < 3" --explanation "informational, below alert threshold"
```

When the counts look right, re-run the same command with `--apply` to write the
tags:

```sh
os-cli triage eliminate --index triage-hunt-001 --layer 1 --where "rule.level < 3" --explanation "informational, below alert threshold" --apply
```

Check what remains, then repeat the review-and-eliminate step until the
untriaged count reaches zero. Omit `--layer` and each elimination
auto-increments to the next layer (2, 3, ...):

```sh
os-cli triage status --index triage-hunt-001
```

If needed, `restore` undoes an elimination: it resets the matching documents to
untriaged and records the undo on each document's `triage.history`. `--layer`
resets exactly one layer; `--from-layer` resets that layer _and_ every
subsequent one . The command is a dry run unless `--apply` is given:

```sh
os-cli triage restore --index triage-hunt-001 --layer 3               # dry run
os-cli triage restore --index triage-hunt-001 --from-layer 3 --apply
```

## Predicate quoting

String literals in a predicate use single quotes and field names use backticks,
for example `` `event.action` = 'delivery' ``. Because backticks trigger command
substitution inside a double-quoted shell argument, either wrap the whole
predicate in single quotes, or pass `--where -` to read the predicate from
`stdin`, or `--where @PATH` to read it from a file:

```sh
os-cli triage eliminate --index triage-hunt-001 --layer 1 --explanation "received emails" --where - <<'EOF'
`event.action` = 'delivery' AND `message` = 'Message received'
EOF
```
