# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""The typed result every opensearch_client call returns.

A call returns either a :class:`Success` carrying ``data`` or a :class:`Failure`
carrying a ``reason``; :data:`OpensearchResult` is the union of the two, and both
expose ``ok`` / ``data`` / ``reason`` / ``status`` so a result reads as a single
type (each field's meaning is documented on the two classes). A result is truthy
when it succeeded, so ``if res:`` (or ``if not res:``, ``assert res``) both
branches and narrows ``data`` to be present; you can also pattern-match on
``Success`` / ``Failure``. An expected failure (HTTP error, transport error, bad
JSON) is a value here, not a raised exception.
"""

from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar, Union

T = TypeVar("T")


@dataclass(frozen=True)
class Success(Generic[T]):
    """A call that succeeded; ``data`` holds the result payload of type ``T``.

    For a single request, ``data`` contains response data (a processed response
    body); a multi-request operation such as ``bulk`` may store a summary here
    instead.

    Attributes:
        data: the result payload.
        ok: always True.
        reason: always empty.
        status: the HTTP status code, if the call recorded one.
    """

    data: T
    ok: Literal[True] = True
    reason: str = ""
    status: int | None = None

    def __bool__(self) -> Literal[True]:
        """Report success, so ``if res:`` takes the success branch.

        Returns:
            True.
        """
        return True


@dataclass(frozen=True)
class Failure:
    """A call that failed; ``reason`` explains why.

    ``data`` carries a payload when the failed operation has one: the parsed
    error response body when the server returned one (e.g. ``{"error":
    {"type": "index_not_found_exception", ...}}``), or a composed operation's
    partial summary (e.g. ``bulk``'s). ``status`` is the HTTP status code for
    an HTTP-error failure (e.g. 413, 503), or None for a non-HTTP failure such
    as a transport error or bad JSON.

    Attributes:
        reason: why the call failed.
        ok: always False.
        data: the failed operation's payload, if it has one.
        status: the HTTP status code, or None for a non-HTTP failure.
    """

    reason: str
    ok: Literal[False] = False
    data: Any = None
    status: int | None = None

    def __bool__(self) -> Literal[False]:
        """Report failure, so ``if res:`` takes the failure branch.

        Returns:
            False.
        """
        return False


# The one type callers name. Written with Union (not ``|``) so the generic alias
# stays subscriptable at runtime on Python 3.10.
OpensearchResult = Union[Success[T], Failure]
