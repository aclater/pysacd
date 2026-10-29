#!/usr/bin/env bash
# Fail if tracked files or commit identities contain private details: names in
# the home.arpa special-use domain, private (RFC 1918) IPv4 addresses, local
# mount and home paths, plus the regex in $PRIVATE_NAMES (kept outside the
# repo). Documentation addresses (192.0.2.0/24 and friends) are fine.
# Commits are checked from $BASE_SHA (exclusive) to HEAD, or all of history.
set -euo pipefail

pattern='\.home\.arpa\b|\b192\.168\.[0-9]{1,3}\.[0-9]{1,3}\b|\b10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\b'
pattern="$pattern"'|\b172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}\b|/mnt/[A-Za-z]|/home/[a-z][a-z0-9_-]*/'
if [ -n "${PRIVATE_NAMES:-}" ]; then
    pattern="$pattern|$PRIVATE_NAMES"
fi
status=0

# This script itself spells the patterns out
if git ls-files -z | grep -zv '^scripts/check-private-names\.sh$' | xargs -0 grep -nIiE "$pattern" -- 2>/dev/null; then
    echo "::error::private details found in tracked files (matches above)"
    status=1
fi

range=HEAD
if [ -n "${BASE_SHA:-}" ] && [ "${BASE_SHA}" != "0000000000000000000000000000000000000000" ] \
        && git cat-file -e "${BASE_SHA}^{commit}" 2>/dev/null; then
    range="${BASE_SHA}..HEAD"
fi
if git rev-parse --verify -q HEAD >/dev/null && git log --format='%h %an <%ae> %cn <%ce>' "$range" | grep -iE "$pattern"; then
    echo "::error::private details found in commit identities (matches above)"
    status=1
fi

[ "$status" -eq 0 ] && echo "no private details found"
exit "$status"
