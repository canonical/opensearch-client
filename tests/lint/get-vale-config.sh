#!/usr/bin/env bash
#
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Download the Vale configuration of the Canonical documentation style guide
# into .vale/ (gitignored), unmodified: the rules, severities, vocabulary,
# dictionaries and vale.ini all come from upstream.
#
# The upstream commit is pinned, so results only change when STYLE_GUIDE_REF
# is changed on purpose.
#
# Usage: get-vale-config.sh

set -euo pipefail

STYLE_GUIDE_REF="705da792f4baf51dfeebb70990f43cc04359cdcd"
STYLE_GUIDE_URL="https://github.com/canonical/documentation-style-guide"
DEST=".vale"

work_dir=$(mktemp -d)
trap 'rm -rf "${work_dir}"' EXIT

curl --fail --silent --show-error --location --retry 3 \
    "${STYLE_GUIDE_URL}/archive/${STYLE_GUIDE_REF}.tar.gz" |
    tar -xz -C "${work_dir}" --strip-components=1

rm -rf "${DEST}"
mkdir -p "${DEST}/styles/config/vocabularies"
cp -r "${work_dir}/styles/Canonical" "${DEST}/styles/Canonical"
cp -r "${work_dir}/styles/config/vocabularies/Canonical" "${DEST}/styles/config/vocabularies/Canonical"
cp -r "${work_dir}/styles/config/dictionaries" "${DEST}/styles/config/dictionaries"
cp "${work_dir}/vale.ini" "${DEST}/vale.ini"
