# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""``os-cli`` entrypoint: dispatches to its subcommands."""

import logging
import sys
from argparse import ArgumentParser

from opensearch_client.cli import cluster, index, query, search, triage_cmd
from opensearch_client.config import client_from_env

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# Each command module exposes NAME, add_subparser(subparsers) and run(args, client).
_COMMANDS = (query, search, index, cluster, triage_cmd)


def main() -> None:
    parser = ArgumentParser(prog="os-cli", description="Query an OpenSearch cluster.")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="skip TLS certificate verification, for a dev cluster with a "
        "self-signed cert; the equivalent of OPENSEARCH_INSECURE=1. Give it before "
        "the subcommand: os-cli --insecure query sql ...",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)
    for command in _COMMANDS:
        command.add_subparser(subparsers)

    args = parser.parse_args()
    client = client_from_env(insecure=args.insecure)
    if client is None:
        sys.exit(2)
    for command in _COMMANDS:
        if args.subcommand == command.NAME:
            command.run(args, client)
            return


if __name__ == "__main__":
    main()
