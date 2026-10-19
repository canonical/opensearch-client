# Log handler

_See also: [Library](library.md),
[Log handler concurrency](/docs/log-handler-concurrency.md)_

`OpensearchLogHandler` is a `logging.Handler` that sends emitted records to an
OpenSearch index.

The `emit` function submits records to a background thread, which periodically
ships records via the `bulk` endpoint, so logging never waits on the cluster.

The module also includes `ECSLogFormatter` (a `logging.Formatter`) and
`ECSLogContextFilter` (a `logging.Filter`). The formatter converts log records
to the Elastic Common Schema (ECS), and the filter adds host and service-based
enrichment.

These utilities can be attached to the log handler at creation-time by using the
`ecs_formatter=True` and `ecs_context=True` parameters.

```python
handler = OpensearchLogHandler(client, "app-logs", ecs_formatter=True, ecs_context=True)
logging.getLogger().addHandler(handler)
```
