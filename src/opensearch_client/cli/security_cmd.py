# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""``os-cli security``: manage an account that may only write logs to one index.

A thin CLI over :mod:`opensearch_client.security`::

    security create-account USER --index INDEX --password-file PATH|-
    security check-account USER --index INDEX
    security delete-account USER [--apply]
"""

import logging
import sys
from argparse import Namespace, _SubParsersAction

from opensearch_client import security
from opensearch_client.cli.diagnostics import diagnose
from opensearch_client.cli.io import add_format_argument, emit
from opensearch_client.client import OpensearchClient

NAME = "security"


def add_subparser(subparsers: _SubParsersAction) -> None:
    """Register ``security`` and its verbs.

    Args:
        subparsers: the top-level subparsers action to register under.
    """
    parser = subparsers.add_parser(
        NAME, help="manage an account that may only write logs to one index"
    )
    operations = parser.add_subparsers(dest="operation", required=True)

    create = operations.add_parser(
        "create-account",
        help="create the role, user and role mapping; never overwrites",
    )
    create.add_argument("user", metavar="USER", help="the user name")
    create.add_argument(
        "--index",
        required=True,
        metavar="INDEX",
        help="the exact name of the one index the account writes to",
    )
    create.add_argument(
        "--password-file",
        required=True,
        metavar="PATH",
        help="a file holding the user's password, or '-' to read it from stdin",
    )
    add_format_argument(create)

    check = operations.add_parser(
        "check-account", help="report which parts of the account exist"
    )
    check.add_argument("user", metavar="USER", help="the user name")
    check.add_argument(
        "--index",
        required=True,
        metavar="INDEX",
        help="the exact name of the index the account writes to",
    )
    add_format_argument(check)

    delete = operations.add_parser(
        "delete-account",
        help="delete the role mapping, user and role; dry run unless --apply",
    )
    delete.add_argument("user", metavar="USER", help="the user name")
    delete.add_argument(
        "--apply",
        action="store_true",
        help="actually delete; without it, only report which parts exist",
    )
    add_format_argument(delete)


def run(args: Namespace, client: OpensearchClient) -> None:
    """Run the chosen security verb against the client.

    Args:
        args: the parsed command-line arguments.
        client: the client to run the verb with.
    """
    if args.operation == "create-account":
        password = _read_password(args.password_file)
        if password is None:
            sys.exit(2)
        result = security.create_log_writer_account(
            client, args.user, password, args.index
        )
        if not result and result.data["created"]:
            done = ", ".join(result.data["created"])
            logging.error(f"created before the failure, and not undone: {done}")
        emit(diagnose(result), "Create account", args.format)
    elif args.operation == "check-account":
        result = security.check_log_writer_account(client, args.user, args.index)
        emit(diagnose(result), "Check account", args.format)
    elif args.operation == "delete-account":
        result = security.delete_log_writer_account(client, args.user, args.apply)
        if not args.apply:
            logging.info("dry run: nothing was deleted; pass --apply to delete")
        emit(diagnose(result), "Delete account", args.format)


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
