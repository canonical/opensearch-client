# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""OpenSearch client library.

The public surface:

    from opensearch_client import OpensearchClient, OpensearchResult, client_from_env

and, to ship log records to an index, ``OpensearchLogHandler``, optionally with
``ECSLogFormatter`` and ``ECSLogContextFilter`` for ECS documents.
"""

from opensearch_client.client import OpensearchClient
from opensearch_client.config import client_from_env
from opensearch_client.log_handler import (
    ECSLogContextFilter,
    ECSLogFormatter,
    OpensearchLogHandler,
)
from opensearch_client.result import Failure, OpensearchResult, Success
from opensearch_client.transport import (
    DirectTransport,
    FailoverTransport,
    ProbeTransport,
    ProxyTransport,
)

__all__ = [
    "OpensearchClient",
    "ECSLogContextFilter",
    "ECSLogFormatter",
    "OpensearchLogHandler",
    "OpensearchResult",
    "Success",
    "Failure",
    "client_from_env",
    "DirectTransport",
    "ProxyTransport",
    "FailoverTransport",
    "ProbeTransport",
]
