# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Manage Security plugin accounts: roles, internal users and role mappings.

This module calls the Security REST API through
:meth:`~opensearch_client.client.OpensearchClient.request`; the client has no methods of its
own for it, and the API paths live here. Two parts:

- The classes model the request bodies this module sends, and only the fields a
  service account needs; the API accepts more. Their field names are the JSON
  keys. ``to_dict`` builds a request body. Responses are not parsed into these
  classes: the ``get_*`` and ``list_*`` functions return the response JSON.
- The functions work with those classes and return
  :class:`~opensearch_client.result.OpensearchResult`; an expected failure is a value, not
  an exception.

Documentation of the API, which these docstrings were checked against:

- Roles: https://docs.opensearch.org/latest/security/api/roles/create-role/
- Internal users: https://docs.opensearch.org/latest/security/api/users/create-user/
- Role mappings:
  https://docs.opensearch.org/latest/security/api/role-mappings/create-role-mapping/
- Action groups such as ``index`` and ``read``:
  https://docs.opensearch.org/latest/security/access-control/default-action-groups/
- Permission names such as ``indices:data/write/bulk``:
  https://docs.opensearch.org/latest/security/access-control/permissions/

Rules that apply throughout:

- A class refuses invalid values when it is created: its ``__post_init__`` raises
  ``ValueError``, so an invalid object never exists. A function that takes a bare
  name instead returns a failure.
- Creation is create-only. A role, user or mapping that already exists is never
  overwritten: ``create_*`` returns a failure instead. Replacing means deleting
  and creating again. (The API itself creates or replaces.)
- A password is write-only. It is accepted when a user is created, is never
  returned by the API, and is kept out of ``repr``. Nothing here generates a
  password. Only a plain-text ``password`` is supported, not a ``hash``.
- Secrets are never printed or logged. Output built from a raw API response goes
  through :func:`redact_secrets` first, and a request body is never logged: logs
  may be shipped to the cluster, and a logged password would then be stored in an
  index.

The Security API is served by the cluster's REST port (9200 by default), so
OPENSEARCH_URL should point there. Whether it can also be reached through a
Dashboards console proxy (the OPENSEARCH_DASHBOARD_URL route) has not been
verified.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from opensearch_client.client import OpensearchClient
from opensearch_client.result import Failure, OpensearchResult, Success

# The API reads these in a role mapping's ``users`` as wildcards, so a user name
# containing one would map the role to every user it matches.
_WILDCARDS = "*?"


def _invalid_name(kind: str, name: str) -> Failure | None:
    """Refuse a name that would not address one object.

    A blank name makes the path the collection's, so a lookup would list every
    object (the API documents this: omit the name to get them all). ``.`` and
    ``..`` are dot segments, which an HTTP client may resolve to another endpoint
    even though they are not encoded.

    Args:
        kind: what the name is of (``role``, ``user``), for the failure message.
        name: the name to check.

    Returns:
        A failure to return as it is, or None if the name is fine.
    """
    if not name.strip() or name in (".", ".."):
        return Failure(
            f"{kind} name {name!r} is not valid: it must not be blank, '.' or '..'"
        )
    return None


@dataclass(frozen=True)
class Role:
    """A Security plugin role: what an account may do on the cluster and on indexes.

    A role has cluster permissions and, optionally, one set of index permissions: a
    list of index patterns and the actions allowed on them. The API also supports
    several such sets, document- and field-level security, and tenant permissions;
    none is needed for the service accounts this module manages.

    API: https://docs.opensearch.org/latest/security/api/roles/create-role/

    Example:
        The request body ``to_dict`` builds. In the API, only the entries of
        ``index_permissions`` have required fields (``index_patterns`` and
        ``allowed_actions``)::

            {
              "cluster_permissions": ["cluster_monitor"],
              "index_permissions": [
                {
                  "index_patterns": ["example-index-*"],
                  "allowed_actions": ["read"]
                }
              ]
            }

        The API returns a role as an object keyed by its name, and also carries
        ``reserved``, ``hidden`` and ``static`` (not modelled here).

    Attributes:
        name: the role name. In the API it is part of the URL, not a body field.
        cluster_permissions: cluster-level actions or action groups.
        index_patterns: the indexes the index permissions apply to; wildcards
            allowed. Empty for a role with no index permissions.
        index_actions: individual actions or action groups allowed on
            ``index_patterns``, e.g. ``read``. Required when there are patterns.

    Raises:
        ValueError: on creation, if the name is blank, ``.`` or ``..``; if
            patterns are given without actions or the reverse; or if any entry of
            a list is blank.
    """

    name: str
    cluster_permissions: tuple[str, ...] = ()
    index_patterns: tuple[str, ...] = ()
    index_actions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        invalid = _invalid_name("role", self.name)
        if invalid is not None:
            raise ValueError(invalid.reason)
        if bool(self.index_patterns) != bool(self.index_actions):
            raise ValueError(
                f"role {self.name!r}: index_patterns and index_actions must be given "
                "together"
            )
        entries = (*self.cluster_permissions, *self.index_patterns, *self.index_actions)
        if any(not entry.strip() for entry in entries):
            raise ValueError(f"role {self.name!r}: a permission or pattern is blank")

    def to_dict(self) -> dict[str, Any]:
        """Build the request body for the roles API.

        Returns:
            ``cluster_permissions`` and ``index_permissions`` (one entry, or none
            if there are no index patterns). The name is left out: it goes in the
            URL.
        """
        index_permissions: list[dict[str, Any]] = []
        if self.index_patterns:
            index_permissions.append(
                {
                    "index_patterns": list(self.index_patterns),
                    "allowed_actions": list(self.index_actions),
                }
            )
        return {
            "cluster_permissions": list(self.cluster_permissions),
            "index_permissions": index_permissions,
        }


@dataclass(frozen=True)
class RoleMapping:
    """Gives a role to named users.

    The API also maps backend roles and hosts (and requires all of a set of backend
    roles with ``and_backend_roles``); this module maps only users, by exact name.

    API: https://docs.opensearch.org/latest/security/api/role-mappings/create-role-mapping/

    Example:
        The request body ``to_dict`` builds::

            {"users": ["example-user"]}

        The API returns a mapping as an object keyed by the role name, and also
        carries ``backend_roles``, ``and_backend_roles``, ``hosts``, ``reserved``,
        ``hidden`` and ``static`` (not modelled here).

    Attributes:
        role: the role being mapped. In the API it is part of the URL, not a body
            field.
        users: the names of the users who receive the role.

    Raises:
        ValueError: on creation, if the role name is blank, ``.`` or ``..``; if
            there are no users; or if a user name is invalid or contains ``*`` or
            ``?``, which the API reads as wildcards and which would give the role
            to every user the name matches.
    """

    role: str
    users: tuple[str, ...]

    def __post_init__(self) -> None:
        invalid = _invalid_name("role", self.role)
        if invalid is not None:
            raise ValueError(invalid.reason)
        if not self.users:
            raise ValueError(f"mapping of role {self.role!r}: needs at least one user")
        for user in self.users:
            invalid = _invalid_name("user", user)
            if invalid is not None:
                raise ValueError(f"mapping of role {self.role!r}: {invalid.reason}")
            if any(wildcard in user for wildcard in _WILDCARDS):
                raise ValueError(
                    f"mapping of role {self.role!r}: user name {user!r} must not "
                    "contain '*' or '?'"
                )

    def to_dict(self) -> dict[str, Any]:
        """Build the request body for the role mappings API.

        Returns:
            ``users``. The role name is left out: it goes in the URL.
        """
        return {"users": list(self.users)}


@dataclass(frozen=True)
class User:
    """An internal user of the Security plugin, with a plain-text password.

    Every ``User`` is sent to the API to create an account, and the API requires a
    password (or a hash, which this module does not support), so an empty password
    is invalid. The API also takes backend roles, security roles, attributes and a
    description; none is needed here, since the account gets its role through a
    :class:`RoleMapping`.

    API: https://docs.opensearch.org/latest/security/api/users/create-user/

    Example:
        The request body ``to_dict`` builds. The API never returns the password::

            {"password": "<the plain-text password>"}

        The API returns a user as an object keyed by the user name, with the
        fields ``attributes``, ``backend_roles``, ``opendistro_security_roles``,
        ``description``, an empty ``hash``, ``reserved``, ``hidden`` and
        ``static`` (not modelled here).

    Attributes:
        name: the user name. In the API it is part of the URL, not a body field.
        password: the plain-text password; hidden from ``repr``. The Security
            plugin hashes it before storing it, and a password policy applies.

    Raises:
        ValueError: on creation, if the name is blank, ``.`` or ``..``, or
            contains ``*`` or ``?`` (see :class:`RoleMapping`); or if the password
            is empty. The message never contains the password.
    """

    name: str
    password: str = field(repr=False)

    def __post_init__(self) -> None:
        invalid = _invalid_name("user", self.name)
        if invalid is not None:
            raise ValueError(invalid.reason)
        if any(wildcard in self.name for wildcard in _WILDCARDS):
            raise ValueError(f"user name {self.name!r} must not contain '*' or '?'")
        if not self.password:
            raise ValueError(f"user {self.name!r} needs a password")

    def to_dict(self) -> dict[str, Any]:
        """Build the request body for the internal users API.

        Never print or log the result: it holds the password.

        Returns:
            ``password``. The name is left out: it goes in the URL.
        """
        return {"password": self.password}


_ROLES_PATH = "_plugins/_security/api/roles"
_USERS_PATH = "_plugins/_security/api/internalusers"
_ROLE_MAPPINGS_PATH = "_plugins/_security/api/rolesmapping"


def _path(base: str, name: str) -> str:
    """Build the API path of one named object, encoding the name.

    Encoding keeps a ``/`` or ``?`` in a name from changing which endpoint is hit.
    """
    return f"{base}/{quote(name, safe='')}"


REDACTED = "<redacted>"

# Keys whose values must never be printed or logged, wherever they appear in a
# response (compared in lower case):
# - password: write-only; the API never returns it, but a request body carries it.
# - hash: the stored password hash. The API normally returns it empty, but a
#   response could contain one, and a hash can be attacked offline.
# Not redacted, but worth a decision: a user's ``attributes`` are free-form
# name-value pairs and could hold anything.
SENSITIVE_KEYS = frozenset({"password", "hash"})


# -- output -------------------------------------------------------------------


def redact_secrets(data: Any) -> Any:
    """Return a copy of an API response with every secret value replaced.

    Every value under a key in ``SENSITIVE_KEYS`` becomes ``REDACTED``, whatever
    its type, at any depth in nested dicts and lists. Keys are compared in lower
    case, so a free-form name such as ``Password`` in a user's attributes is
    redacted too. The input is not changed.

    Args:
        data: a decoded API response: a dict, a list, or a scalar.

    Returns:
        The same structure with the sensitive values replaced; a scalar is
        returned as it is.
    """
    if isinstance(data, dict):
        return {
            key: REDACTED
            if isinstance(key, str) and key.lower() in SENSITIVE_KEYS
            else redact_secrets(value)
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [redact_secrets(item) for item in data]
    return data


# -- build ------------------------------------------------------------------


def build_logging_writer_role(
    name: str, index_patterns: tuple[str, ...]
) -> OpensearchResult[Role]:
    """Build the least-privileged role for an account that only ships logs.

    The role lets an account check that its index exists and write documents to
    it (cluster ``indices:data/write/bulk``; on ``index_patterns``,
    ``indices:admin/get`` and the ``index`` action group), and nothing else. The
    documentation says a bulk permission must be set at both the cluster and the
    index level, and the ``index`` group includes ``indices:data/write/bulk*``. That
    group also includes ``indices:data/write/update*`` and
    ``indices:admin/mapping/put``, which a log writer does not strictly need; a
    narrower list is possible but has not been verified against a real cluster.
    See https://docs.opensearch.org/latest/security/access-control/default-action-groups/

    Args:
        name: the role name.
        index_patterns: the indexes the account may write to.

    Returns:
        The role, or a failure if :class:`Role` would refuse it (a bad name, no
        index patterns, or a blank pattern) or a pattern is a bare ``*``. ``*``
        would let the account write to every index, which is not least privilege; a
        role that needs it can be built as a :class:`Role` directly.
    """
    if "*" in index_patterns:
        return Failure(
            f"role {name!r}: a log-writing role must not allow the pattern '*', "
            "which matches every index"
        )
    try:
        role = Role(
            name=name,
            cluster_permissions=("indices:data/write/bulk",),
            index_patterns=index_patterns,
            index_actions=("indices:admin/get", "index"),
        )
    except ValueError as error:
        if not index_patterns:
            return Failure(f"role {name!r}: needs at least one index pattern")
        return Failure(str(error))
    return Success(role)


# -- existence ----------------------------------------------------------------


def _exists(lookup: OpensearchResult[dict[str, Any]]) -> OpensearchResult[bool]:
    """Turn the result of a ``get_*`` call into whether the object exists."""
    if lookup:
        return Success(True)
    if lookup.status == 404:
        return Success(False)
    return lookup


def role_exists(client: OpensearchClient, name: str) -> OpensearchResult[bool]:
    """Report whether a role exists.

    Args:
        client: the client used to reach the cluster.
        name: the role name.

    Returns:
        True or False; a failure other than "not found" (such as an
        authorization error, or a name refused as in :func:`get_role`) is returned
        as a failure, not as False.
    """
    return _exists(get_role(client, name))


def user_exists(client: OpensearchClient, name: str) -> OpensearchResult[bool]:
    """Report whether an internal user exists.

    Args:
        client: the client used to reach the cluster.
        name: the user name.

    Returns:
        True or False; any other failure is returned as a failure.
    """
    return _exists(get_user(client, name))


def role_mapping_exists(client: OpensearchClient, role: str) -> OpensearchResult[bool]:
    """Report whether a role has a mapping.

    Args:
        client: the client used to reach the cluster.
        role: the role name.

    Returns:
        True or False; any other failure is returned as a failure.
    """
    return _exists(get_role_mapping(client, role))


def check_service_account(
    client: OpensearchClient, user: str, role: str, index: str
) -> OpensearchResult[dict[str, bool]]:
    """Report which parts of a service account already exist.

    Args:
        client: the client used to reach the cluster.
        user: the user name.
        role: the role name; its mapping is checked too.
        index: the one index the account writes to, by exact name.

    Returns:
        A mapping with the keys ``user``, ``role``, ``role_mapping`` and
        ``index``, each True or False; or the first failure met (an authorization
        error, say), in which case the later parts are not looked up. A user or
        role name refused as in :func:`get_role`, or an index name that could match
        more than one index, fails without a request. For the index that means a
        name that is blank, ``.``, ``..`` or starts with ``_`` (an API endpoint, or
        ``_all``), or contains ``*``, ``?`` or ``,``: a lookup of such a name would
        succeed whenever any index, or any endpoint, matched.
    """
    for invalid in (_invalid_name("user", user), _invalid_name("role", role)):
        if invalid is not None:
            return invalid
    if (
        _invalid_name("index", index) is not None
        or index.startswith("_")
        or any(char in index for char in "*?,")
    ):
        return Failure(
            f"index name {index!r} is not valid: it must name one index, so not "
            "blank, '.', '..', starting with '_', or containing '*', '?' or ','"
        )

    lookups: list[tuple[str, Callable[[], OpensearchResult[bool]]]] = [
        ("user", lambda: user_exists(client, user)),
        ("role", lambda: role_exists(client, role)),
        ("role_mapping", lambda: role_mapping_exists(client, role)),
        ("index", lambda: client.index_exists(quote(index, safe=""))),
    ]
    found: dict[str, bool] = {}
    for part, lookup in lookups:
        outcome = lookup()
        if not outcome:
            return outcome
        found[part] = outcome.data
    return Success(found)


# -- read ---------------------------------------------------------------------


def get_role(client: OpensearchClient, name: str) -> OpensearchResult[dict[str, Any]]:
    """Get one role.

    Args:
        client: the client used to reach the cluster.
        name: the role name.

    Returns:
        The response JSON, an object keyed by the role name, as the API returns it;
        or a failure (status 404 when it does not exist). A blank name, ``.`` or
        ``..`` is refused without a request.

    API: https://docs.opensearch.org/latest/security/api/roles/get-roles/
    """
    invalid = _invalid_name("role", name)
    if invalid is not None:
        return invalid
    return client.request("GET", _path(_ROLES_PATH, name))


def get_user(client: OpensearchClient, name: str) -> OpensearchResult[dict[str, Any]]:
    """Get one internal user.

    Args:
        client: the client used to reach the cluster.
        name: the user name.

    Returns:
        The response JSON, an object keyed by the user name, as the API returns it;
        or a failure (status 404 when it does not exist). A blank name, ``.`` or
        ``..`` is refused without a request. It is not redacted: pass it through
        :func:`redact_secrets` before printing.

    API: https://docs.opensearch.org/latest/security/api/users/get-users/
    """
    invalid = _invalid_name("user", name)
    if invalid is not None:
        return invalid
    return client.request("GET", _path(_USERS_PATH, name))


def get_role_mapping(
    client: OpensearchClient, role: str
) -> OpensearchResult[dict[str, Any]]:
    """Get the mapping of one role.

    Args:
        client: the client used to reach the cluster.
        role: the role name.

    Returns:
        The response JSON, an object keyed by the role name, as the API returns it;
        or a failure (status 404 when the role has none). A blank name, ``.`` or
        ``..`` is refused without a request.

    API: https://docs.opensearch.org/latest/security/api/role-mappings/get-role-mappings/
    """
    invalid = _invalid_name("role", role)
    if invalid is not None:
        return invalid
    return client.request("GET", _path(_ROLE_MAPPINGS_PATH, role))


def list_roles(client: OpensearchClient) -> OpensearchResult[dict[str, Any]]:
    """List every role the API returns.

    Args:
        client: the client used to reach the cluster.

    Returns:
        The response JSON, an object keyed by role name, as the API returns it.

    API: https://docs.opensearch.org/latest/security/api/roles/get-roles/
    """
    return client.request("GET", _ROLES_PATH)


def list_users(client: OpensearchClient) -> OpensearchResult[dict[str, Any]]:
    """List every internal user the API returns.

    Args:
        client: the client used to reach the cluster.

    Returns:
        The response JSON, an object keyed by user name, as the API returns it. It
        is not redacted: pass it through :func:`redact_secrets` before printing.

    API: https://docs.opensearch.org/latest/security/api/users/get-users/
    """
    return client.request("GET", _USERS_PATH)


def list_role_mappings(client: OpensearchClient) -> OpensearchResult[dict[str, Any]]:
    """List every role mapping the API returns.

    Args:
        client: the client used to reach the cluster.

    Returns:
        The response JSON, an object keyed by role name, as the API returns it.

    API: https://docs.opensearch.org/latest/security/api/role-mappings/get-role-mappings/
    """
    return client.request("GET", _ROLE_MAPPINGS_PATH)


# -- create (create-only) -----------------------------------------------------


def _create_only(
    client: OpensearchClient,
    lookup: OpensearchResult[bool],
    description: str,
    path: str,
    body: dict[str, Any],
) -> OpensearchResult[dict[str, Any]]:
    """PUT ``body`` to ``path`` unless the object is already there.

    The check and the PUT are two requests, so another writer could create the
    object in between. The Security API has no conditional PUT.

    Args:
        client: the client used to reach the cluster.
        lookup: the result of the existence check made just before.
        description: what is being created, for the failure message.
        path: the object's API path.
        body: the request body; never logged, since it may hold a password.

    Returns:
        The API's acknowledgement; the failed check if it failed; or a failure with
        status 409 if the object exists.
    """
    if not lookup:
        return lookup
    if lookup.data:
        return Failure(f"{description} already exists", status=409)
    return client.request("PUT", path, body)


def create_role(
    client: OpensearchClient, role: Role
) -> OpensearchResult[dict[str, Any]]:
    """Create a role; never overwrite one.

    Args:
        client: the client used to reach the cluster.
        role: the role to create.

    Returns:
        The API's acknowledgement, or a failure. A role that already exists is a
        failure (status 409) and is left unchanged.

    API: https://docs.opensearch.org/latest/security/api/roles/create-role/
    """
    return _create_only(
        client,
        role_exists(client, role.name),
        f"role {role.name!r}",
        _path(_ROLES_PATH, role.name),
        role.to_dict(),
    )


def create_user(
    client: OpensearchClient, user: User
) -> OpensearchResult[dict[str, Any]]:
    """Create an internal user; never overwrite one.

    Args:
        client: the client used to reach the cluster.
        user: the user to create, with its password set.

    Returns:
        The API's acknowledgement, or a failure. A user that already exists is a
        failure (status 409) and keeps its password.

    API: https://docs.opensearch.org/latest/security/api/users/create-user/
    """
    return _create_only(
        client,
        user_exists(client, user.name),
        f"user {user.name!r}",
        _path(_USERS_PATH, user.name),
        user.to_dict(),
    )


def create_role_mapping(
    client: OpensearchClient, mapping: RoleMapping
) -> OpensearchResult[dict[str, Any]]:
    """Create a role mapping; never replace one.

    Args:
        client: the client used to reach the cluster.
        mapping: the mapping to create.

    Returns:
        The API's acknowledgement, or a failure. A role that already has a mapping
        is a failure (status 409) and keeps it.

    API: https://docs.opensearch.org/latest/security/api/role-mappings/create-role-mapping/
    """
    return _create_only(
        client,
        role_mapping_exists(client, mapping.role),
        f"mapping of role {mapping.role!r}",
        _path(_ROLE_MAPPINGS_PATH, mapping.role),
        mapping.to_dict(),
    )


def create_service_account(
    client: OpensearchClient, user: User, role: Role
) -> OpensearchResult[dict[str, Any]]:
    """Create a role, a user, and the mapping that gives the user the role.

    The steps run in that order, so access is granted last and a failure part-way
    leaves no working account. Each step is create-only.

    Args:
        client: the client used to reach the cluster.
        user: the account's user, with its password set.
        role: the account's role.

    Returns:
        A summary naming the user, role and mapping created, with the keys ``role``,
        ``user`` and ``role_mapping``. On a failure, a failure whose data is
        ``{"created": [...]}``, listing the steps that had already succeeded
        (``role``, ``user``); nothing is undone. An object that already exists fails
        with status 409 at its step; the steps before it are not undone.
    """
    mapping = RoleMapping(role=role.name, users=(user.name,))
    steps: list[tuple[str, Callable[[], OpensearchResult[dict[str, Any]]]]] = [
        ("role", lambda: create_role(client, role)),
        ("user", lambda: create_user(client, user)),
        ("role mapping", lambda: create_role_mapping(client, mapping)),
    ]
    created: list[str] = []
    for step, create in steps:
        outcome = create()
        if not outcome:
            return Failure(
                f"creating the {step} failed: {outcome.reason}",
                data={"created": created},
                status=outcome.status,
            )
        created.append(step)
    return Success({"role": role.name, "user": user.name, "role_mapping": role.name})


# -- delete -------------------------------------------------------------------


def delete_role(
    client: OpensearchClient, name: str
) -> OpensearchResult[dict[str, Any]]:
    """Delete a role.

    Args:
        client: the client used to reach the cluster.
        name: the role name.

    Returns:
        The API's acknowledgement, or a failure (the API's own, such as a 404 for a
        role that does not exist or a refusal for a reserved one, is returned
        unchanged). A blank name, ``.`` or ``..`` is refused without a request.
        The role's mapping is not deleted with it.

    API: https://docs.opensearch.org/latest/security/api/roles/delete-role/
    """
    invalid = _invalid_name("role", name)
    if invalid is not None:
        return invalid
    return client.request("DELETE", _path(_ROLES_PATH, name))


def delete_user(
    client: OpensearchClient, name: str
) -> OpensearchResult[dict[str, Any]]:
    """Delete an internal user.

    Args:
        client: the client used to reach the cluster.
        name: the user name.

    Returns:
        The API's acknowledgement, or a failure (the API's own, such as a 404 for a
        user that does not exist or a refusal for a reserved one, is returned
        unchanged). A blank name, ``.`` or ``..`` is refused without a request.
        Mappings that name the user are not changed.

    API: https://docs.opensearch.org/latest/security/api/users/delete-user/
    """
    invalid = _invalid_name("user", name)
    if invalid is not None:
        return invalid
    return client.request("DELETE", _path(_USERS_PATH, name))


def delete_role_mapping(
    client: OpensearchClient, role: str
) -> OpensearchResult[dict[str, Any]]:
    """Delete the mapping of a role.

    Args:
        client: the client used to reach the cluster.
        role: the role name.

    Returns:
        The API's acknowledgement, or a failure (the API's own, such as a 404 for a
        role with no mapping, is returned unchanged). A blank name, ``.`` or ``..``
        is refused without a request. The role itself is not deleted.

    API: https://docs.opensearch.org/latest/security/api/role-mappings/delete-role-mapping/
    """
    invalid = _invalid_name("role", role)
    if invalid is not None:
        return invalid
    return client.request("DELETE", _path(_ROLE_MAPPINGS_PATH, role))
