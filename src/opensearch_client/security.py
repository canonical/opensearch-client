# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Create, check and delete an account that may only write logs to one index.

The account is three Security plugin objects named after its user: the role
``<user>-writer``, the internal user, and the role mapping between them.
"""

from typing import Any
from urllib.parse import quote

from opensearch_client.client import OpensearchClient
from opensearch_client.result import Failure, OpensearchResult, Success

_ROLES_PATH = "_plugins/_security/api/roles"
_USERS_PATH = "_plugins/_security/api/internalusers"
_ROLE_MAPPINGS_PATH = "_plugins/_security/api/rolesmapping"


def _build_path(base: str, name: str) -> str:
    """Build the API path of one named object, encoding the name.

    Encoding keeps a ``/`` or ``?`` in a name from changing which endpoint is hit.
    """
    return f"{base}/{quote(name, safe='')}"


def _check_user_name(user: str) -> Failure | None:
    """Refuse a user name that would not address exactly one user.

    A blank name, ``.`` or ``..`` would address the whole collection or another
    endpoint. ``*`` and ``?`` are wildcards in a role mapping, where they would
    give the role to every user they match.
    """
    if not user.strip() or user in (".", "..") or "*" in user or "?" in user:
        return Failure(
            f"user name {user!r} is not valid: it must not be blank, '.' or '..', "
            "or contain '*' or '?'"
        )
    return None


def _check_index_name(index: str) -> Failure | None:
    """Refuse an index name that would not address exactly one index.

    Besides the cases of :func:`_check_user_name`, a name starting with ``_`` is
    an API endpoint (or ``_all``), and ``,`` separates several indexes.
    """
    if (
        not index.strip()
        or index in (".", "..")
        or index.startswith("_")
        or "*" in index
        or "?" in index
        or "," in index
    ):
        return Failure(
            f"index name {index!r} is not valid: it must name one index, so not "
            "blank, '.', '..', starting with '_', or containing '*', '?' or ','"
        )
    return None


def _create_if_absent(
    client: OpensearchClient, path: str, body: dict[str, Any]
) -> OpensearchResult[Any]:
    """PUT ``body`` to ``path`` unless an object is already there.

    The lookup and the PUT are two requests, so another writer could create the
    object in between; the Security API has no conditional PUT.
    """
    lookup = client.request("GET", path)
    if lookup:
        return Failure("it already exists", status=409)
    if lookup.status != 404:
        return lookup
    return client.request("PUT", path, body)


def create_log_writer_account(
    client: OpensearchClient, user: str, password: str, index: str
) -> OpensearchResult[dict[str, str]]:
    """Create the role, the user and the mapping of a log-writing account.

    The parts are created in that order, so the user gets access only once the
    other two exist. A part that already exists is not changed: the call stops
    there, and nothing created before it is undone.

    Args:
        client: the client used to reach the cluster, as an administrator.
        user: the user name.
        password: the user's plain-text password.
        index: the exact name of the one index the account writes to.

    Returns:
        The names of the role, user and role mapping created; or a failure whose
        ``data`` is ``{"created": [...]}``, the parts created before it. A part
        that already exists fails with status 409. A bad name or an empty
        password fails before any request.
    """
    for invalid in (_check_user_name(user), _check_index_name(index)):
        if invalid is not None:
            return Failure(invalid.reason, data={"created": []})
    if not password:
        return Failure(f"user {user!r} needs a password", data={"created": []})
    role = f"{user}-writer"

    result = _create_if_absent(
        client,
        _build_path(_ROLES_PATH, role),
        {
            # Bulk requests are checked at the cluster level as well as on the index.
            "cluster_permissions": ["indices:data/write/bulk"],
            "index_permissions": [
                {
                    "index_patterns": [index],
                    "allowed_actions": [
                        # Look the index up before writing to it.
                        "indices:admin/get",
                        # Create the index when it is missing or was deleted.
                        "create_index",
                        # Write documents, and map the fields they add.
                        "index",
                    ],
                }
            ],
        },
    )
    if not result:
        return Failure(
            f"creating the role {role!r} failed: {result.reason}",
            data={"created": []},
            status=result.status,
        )

    result = _create_if_absent(
        client, _build_path(_USERS_PATH, user), {"password": password}
    )
    if not result:
        return Failure(
            f"creating the user {user!r} failed: {result.reason}",
            data={"created": ["role"]},
            status=result.status,
        )

    result = _create_if_absent(
        client, _build_path(_ROLE_MAPPINGS_PATH, role), {"users": [user]}
    )
    if not result:
        return Failure(
            f"creating the mapping of role {role!r} failed: {result.reason}",
            data={"created": ["role", "user"]},
            status=result.status,
        )

    return Success({"role": role, "user": user, "role_mapping": role})


def check_log_writer_account(
    client: OpensearchClient, user: str, index: str
) -> OpensearchResult[dict[str, bool]]:
    """Report which parts of a log-writing account exist, and whether its index does.

    Args:
        client: the client used to reach the cluster, as an administrator.
        user: the user name.
        index: the exact name of the index the account writes to.

    Returns:
        ``user``, ``role``, ``role_mapping`` and ``index``, each True or False; or
        the first failure other than "not found", after which nothing more is
        looked up. A bad name fails before any request.
    """
    for invalid in (_check_user_name(user), _check_index_name(index)):
        if invalid is not None:
            return invalid
    role = f"{user}-writer"
    paths = {
        "user": _build_path(_USERS_PATH, user),
        "role": _build_path(_ROLES_PATH, role),
        "role_mapping": _build_path(_ROLE_MAPPINGS_PATH, role),
        "index": quote(index, safe=""),
    }

    found: dict[str, bool] = {}
    for part, path in paths.items():
        result = client.request("GET", path)
        if result:
            found[part] = True
        elif result.status == 404:
            found[part] = False
        else:
            return result
    return Success(found)


def delete_log_writer_account(
    client: OpensearchClient, user: str, apply: bool
) -> OpensearchResult[dict[str, bool]]:
    """Delete the role mapping, the user and the role of a log-writing account.

    They are deleted in that order, so the user loses access first. The index and
    its documents are kept.

    Args:
        client: the client used to reach the cluster, as an administrator.
        user: the user name.
        apply: if False, delete nothing and only look each part up.

    Returns:
        ``role_mapping``, ``user`` and ``role``, each True if the part was there
        (and, with ``apply``, has been deleted) or False if it was not; or the
        first failure other than "not found", after which the later parts are
        left as they are. A bad name fails before any request.
    """
    invalid = _check_user_name(user)
    if invalid is not None:
        return invalid
    role = f"{user}-writer"
    paths = {
        "role_mapping": _build_path(_ROLE_MAPPINGS_PATH, role),
        "user": _build_path(_USERS_PATH, user),
        "role": _build_path(_ROLES_PATH, role),
    }

    method = "DELETE" if apply else "GET"
    found: dict[str, bool] = {}
    for part, path in paths.items():
        result = client.request(method, path)
        if result:
            found[part] = True
        elif result.status == 404:
            found[part] = False
        else:
            return result
    return Success(found)
