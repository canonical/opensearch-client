# Index templates

The `os-cli` tool and `opensearch_client` library support the management of
index templates.

An index template fixes the settings and mappings of indices created after it is
put; existing indices keep their mappings, and a field's type cannot be changed
once set. Without one, OpenSearch types each new field from the first document
that carries it, so one source writing a field as a string can cause every later
document writing it as an object to be rejected.

Composable templates (`_index_template`) replace legacy ones (`_template`): when
any composable template matches a new index, every legacy template is ignored
for it, not merged. A composable template built to extend a legacy one must
therefore carry its full mappings. The client reads legacy templates but does
not write them.

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
