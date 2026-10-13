# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""``os-cli security``: Security plugin roles, internal users and role mappings.

A thin CLI over :mod:`opensearch_client.security`::

    security role [NAME]              show roles (all, or one)
    security user [NAME]              show internal users (all, or one)
    security mapping [ROLE]           show role mappings (all, or one)

    security create account USER      create a role, a user and the mapping for an
        --index PATTERN ...           account that only ships logs: the indexes it
        --password-file PATH|-        may write to, and where the password is read
        [--role ROLE]                 from; the role defaults to ``USER-writer``

    security delete role|user|mapping NAME [--apply]
                                      delete one; a dry run unless --apply

    security check USER --role ROLE --index INDEX
                                      report which of the user, role, mapping and
                                      index exist

Creation is create-only: an existing role, user or mapping is reported and left
unchanged. A password is only ever read from a file or from stdin
(``--password-file PATH``, or ``-`` for stdin), never taken from an argument and
never generated. Nothing printed contains it.

The show verbs print the server's raw JSON after passing it through
:func:`opensearch_client.security.redact_secrets`, so a password or hash is never
displayed. The create verb prints only the names of what it created.

``main.py`` builds the client for this command with a direct connection only
(``OPENSEARCH_URL`` or ``OPENSEARCH_HOST``, the cluster's REST port), so the
Security API is never reached through a Dashboards proxy.

An invalid argument (a name the API cannot address, a blank pattern, a missing or
empty password) exits with status 2 before any request; a failed request exits
with status 1.
"""

import logging
import sys
from argparse import Namespace, _SubParsersAction

from opensearch_client import security
from opensearch_client.cli.diagnostics import diagnose
from opensearch_client.cli.io import add_format_argument, emit, render
from opensearch_client.client import OpensearchClient
from opensearch_client.result import Success

NAME = "security"

# For each kind: how to list all, and how to get one.
_SHOW = {
    "role": (security.list_roles, security.get_role),
    "user": (security.list_users, security.get_user),
    "mapping": (security.list_role_mappings, security.get_role_mapping),
}

# For each kind: how to test that one exists, and how to delete it.
_DELETE = {
    "role": (security.role_exists, security.delete_role),
    "user": (security.user_exists, security.delete_user),
    "mapping": (security.role_mapping_exists, security.delete_role_mapping),
}


def add_subparser(subparsers: _SubParsersAction) -> None:
    """Register ``security`` and its verbs.

    Args:
        subparsers: the top-level subparsers action to register under.
    """
    parser = subparsers.add_parser(
        NAME, help="manage Security plugin roles, users and role mappings"
    )
    operations = parser.add_subparsers(dest="operation", required=True)

    for kind, noun, metavar in (
        ("role", "roles", "NAME"),
        ("user", "internal users", "NAME"),
        ("mapping", "role mappings", "ROLE"),
    ):
        show = operations.add_parser(kind, help=f"show {noun} (all, or one named)")
        show.add_argument(
            "name",
            nargs="?",
            metavar=metavar,
            help=f"the {kind} to show (default: all)",
        )
        add_format_argument(show)

    create = operations.add_parser(
        "create", help="create something; never overwrites an existing object"
    )
    create_targets = create.add_subparsers(dest="create_target", required=True)
    account = create_targets.add_parser(
        "account",
        help="create a role, a user and the mapping for an account that only ships "
        "logs",
        epilog="The password is read from --password-file (a path, or '-' for "
        "stdin), never taken from an argument and never generated.",
    )
    account.add_argument("user", metavar="USER", help="the user name")
    account.add_argument(
        "--index",
        action="append",
        required=True,
        metavar="PATTERN",
        help="an index the account may write to; repeat for several (a bare '*' is "
        "refused)",
    )
    account.add_argument(
        "--role",
        metavar="ROLE",
        help="the role name (default: USER-writer)",
    )
    account.add_argument(
        "--password-file",
        required=True,
        metavar="PATH",
        help="a file holding the user's password, or '-' to read it from stdin",
    )
    add_format_argument(account)

    delete = operations.add_parser(
        "delete", help="delete a role, user or role mapping; dry run unless --apply"
    )
    delete_targets = delete.add_subparsers(dest="delete_target", required=True)
    for kind, metavar in (("role", "NAME"), ("user", "NAME"), ("mapping", "ROLE")):
        target = delete_targets.add_parser(
            kind, help=f"delete one {kind}; dry run unless --apply"
        )
        target.add_argument(
            "name", metavar=metavar, help=f"the {kind} to delete (one name)"
        )
        target.add_argument(
            "--apply",
            action="store_true",
            help="actually delete; without it, only report whether it exists",
        )
        add_format_argument(target)

    check = operations.add_parser(
        "check", help="report which parts of an account exist"
    )
    check.add_argument("user", metavar="USER", help="the user name")
    check.add_argument(
        "--role", required=True, metavar="ROLE", help="the role name (and its mapping)"
    )
    check.add_argument(
        "--index",
        required=True,
        metavar="INDEX",
        help="the one index the account writes to, by exact name",
    )
    add_format_argument(check)


def run(args: Namespace, client: OpensearchClient) -> None:
    """Run the chosen security verb against the client.

    Args:
        args: the parsed command-line arguments.
        client: the client to run the verb with.
    """
    if args.operation in _SHOW:
        list_all, get_one = _SHOW[args.operation]
        result = list_all(client) if args.name is None else get_one(client, args.name)
        if result:
            result = Success(security.redact_secrets(result.data))
        emit(diagnose(result), f"Show {args.operation}", args.format)
    elif args.operation == "create":
        role_name = args.role if args.role is not None else f"{args.user}-writer"
        built = security.build_logging_writer_role(role_name, tuple(args.index))
        if not built:
            logging.error(built.reason)
            sys.exit(2)
        password = _read_password(args.password_file)
        if password is None:
            sys.exit(2)
        try:
            user = security.User(name=args.user, password=password)
        except ValueError as error:
            logging.error(str(error))
            sys.exit(2)
        created = security.create_service_account(client, user, built.data)
        if not created and created.data and created.data.get("created"):
            done = ", ".join(created.data["created"])
            logging.error(f"created before the failure, and not undone: {done}")
        emit(diagnose(created), "Create account", args.format)
    elif args.operation == "delete":
        exists, delete = _DELETE[args.delete_target]
        if args.apply:
            deleted = delete(client, args.name)
            emit(diagnose(deleted), f"Delete {args.delete_target}", args.format)
            return
        found = exists(client, args.name)
        if not found:
            emit(diagnose(found), f"Delete {args.delete_target}", args.format)
            return
        summary = {args.delete_target: args.name, "dry_run": True, "exists": found.data}
        print(render(summary, args.format))
    elif args.operation == "check":
        checked = security.check_service_account(
            client, args.user, args.role, args.index
        )
        emit(diagnose(checked), "Check account", args.format)


def _read_password(source: str) -> str | None:
    """Read a password from a file, or from stdin when ``source`` is ``-``.

    Only the trailing newline is removed; any other whitespace is part of the
    password. No error message contains any part of the password.

    Args:
        source: a file path, or ``-`` for stdin.

    Returns:
        The password, or None (after logging why) if it could not be read or was
        empty.
    """
    if source == "-":
        password = sys.stdin.read()
    else:
        try:
            with open(source, mode="r", encoding="utf-8") as handle:
                password = handle.read()
        except OSError as error:
            logging.error(f"could not read the password file {source!r}: {error}")
            return None
        except UnicodeDecodeError:
            logging.error(f"the password file {source!r} is not valid UTF-8 text")
            return None
    password = password.rstrip("\r\n")
    if not password:
        logging.error("the password is empty")
        return None
    return password
