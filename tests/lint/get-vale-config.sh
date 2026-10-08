#!/usr/bin/env bash
#
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Download the Canonical documentation style guide's Vale styles, vocabulary,
# dictionaries and vale.ini into .vale/vale/ (gitignored), unmodified.
#
# Vale reads that directory as its global configuration when XDG_CONFIG_HOME is
# set to .vale, which the vale tox env does. The tracked .vale.ini at the
# repository root holds only this project's overrides, and Vale layers it on top
# of the upstream vale.ini.
#
# The upstream commit is pinned, so results only change when STYLE_GUIDE_REF
# is changed on purpose.
#
# Usage: get-vale-config.sh

set -euo pipefail

STYLE_GUIDE_REF="705da792f4baf51dfeebb70990f43cc04359cdcd"
STYLE_GUIDE_URL="https://github.com/canonical/documentation-style-guide"
DEST=".vale/vale"

work_dir=$(mktemp -d)
trap 'rm -rf "${work_dir}"' EXIT

curl --fail --silent --show-error --location --retry 3 \
    "${STYLE_GUIDE_URL}/archive/${STYLE_GUIDE_REF}.tar.gz" |
    tar -xz -C "${work_dir}" --strip-components=1

rm -rf .vale
mkdir -p "${DEST}/styles/config/vocabularies"
cp -r "${work_dir}/styles/Canonical" "${DEST}/styles/Canonical"
cp -r "${work_dir}/styles/config/vocabularies/Canonical" "${DEST}/styles/config/vocabularies/Canonical"
cp -r "${work_dir}/styles/config/dictionaries" "${DEST}/styles/config/dictionaries"
cp "${work_dir}/vale.ini" "${DEST}/.vale.ini"
