# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""The ``security`` subcommand: roles, internal users and role mappings.

SKELETON: the functions below are declarations with no behaviour, and this
module is not registered in ``main.py`` yet. Thin CLI over :mod:`osclient.security`.

The planned grammar (``SOURCE`` is JSON given as ``@PATH``, ``-`` for stdin, or
literal text, as in the other subcommands)::

    security role [NAME]              show roles (all, or one)
    security user [NAME]              show internal users (all, or one)
    security mapping [ROLE]           show role mappings (all, or one)

    security create role NAME SOURCE          create a role from JSON
    security create user NAME [SOURCE]        create a user; SOURCE is optional JSON
                                              (roles, attributes), and the
                                              password comes from --password-file
    security create mapping ROLE SOURCE       create a role mapping from JSON
    security create account USER              create a role, a user and the mapping
        --index PATTERN ...                   for a log-shipping account: the
        [--role ROLE]                         indexes it may write to, and the role
                                              name (default: derived from USER)

    security delete role|user|mapping NAME    delete one; dry run unless --apply

    security check USER --role ROLE --index INDEX
                                      report which of the user, role, mapping and
                                      index exist

Creation is create-only: an existing role, user or mapping is reported and left
unchanged. A password is only ever read from a file or from stdin
(``--password-file PATH``, or ``-`` for stdin), never taken from an argument and
never generated.

The show verbs print the server's raw JSON (``client.get`` on the API path), after
passing it through :func:`osclient.security.redact_secrets`, so a password or hash
is never displayed. This keeps fields the typed classes do not model. Decision to
revisit: print the typed classes instead. The create verbs print only the API's
acknowledgement and never echo the request body, which holds a password.

The administrator's login comes from the OPENSEARCH_* environment variables, as in
the other subcommands.
"""

from argparse import Namespace, _SubParsersAction

from opensearch_client.client import OpensearchClient

NAME = "security"

# Open question, to revisit: should these commands insist on a direct connection,
# with no fallback to a Dashboards proxy? The leaning is yes.


def add_subparser(subparsers: _SubParsersAction) -> None:
    """Register ``security`` and its verbs.

    Args:
        subparsers: the top-level subparsers action to register under.
    """
    raise NotImplementedError


def run(args: Namespace, client: OpensearchClient) -> None:
    """Run the chosen security verb against the client.

    Args:
        args: the parsed command-line arguments.
        client: the client to run the verb with.
    """
    raise NotImplementedError


def _read_password(source: str) -> str | None:
    """Read a password from a file, or from stdin when ``source`` is ``-``.

    Args:
        source: a file path, or ``-`` for stdin.

    Returns:
        The password with its trailing newline removed, or None (after printing
        an error to stderr) if it could not be read or was empty.
    """
    raise NotImplementedError
