# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""OpenSearch client library.

The public surface:

    from opensearch_client import OpensearchClient, OpensearchResult, client_from_env

and, to ship log records to an index, ``OpensearchLogHandler`` and its
``ECSLogFormatter``.
"""

from opensearch_client.client import DEFAULT_INDEX, OpensearchClient
from opensearch_client.config import client_from_env
from opensearch_client.log_handler import ECSLogFormatter, OpensearchLogHandler
from opensearch_client.result import Failure, OpensearchResult, Success
from opensearch_client.transport import (
    DirectTransport,
    FailoverTransport,
    ProbeTransport,
    ProxyTransport,
)

__all__ = [
    "OpensearchClient",
    "ECSLogFormatter",
    "OpensearchLogHandler",
    "OpensearchResult",
    "Success",
    "Failure",
    "DEFAULT_INDEX",
    "client_from_env",
    "DirectTransport",
    "ProxyTransport",
    "FailoverTransport",
    "ProbeTransport",
]
