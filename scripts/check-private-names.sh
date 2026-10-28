#!/usr/bin/env bash
# Fail if tracked files or commit identities contain private names: anything
# under .home.arpa, plus the regex in $PRIVATE_NAMES (kept outside the repo).
# Commits are checked from $BASE_SHA (exclusive) to HEAD, or all of history.
set -euo pipefail

pattern='\.home\.arpa\b'
if [ -n "${PRIVATE_NAMES:-}" ]; then
    pattern="$pattern|$PRIVATE_NAMES"
fi
status=0

if git ls-files -z | xargs -0 grep -nIiE "$pattern" -- 2>/dev/null; then
    echo "::error::private names found in tracked files (matches above)"
    status=1
fi

range=HEAD
if [ -n "${BASE_SHA:-}" ] && [ "${BASE_SHA}" != "0000000000000000000000000000000000000000" ] \
        && git cat-file -e "${BASE_SHA}^{commit}" 2>/dev/null; then
    range="${BASE_SHA}..HEAD"
fi
if git log --format='%h %an <%ae> %cn <%ce>' "$range" | grep -iE "$pattern"; then
    echo "::error::private names found in commit identities (matches above)"
    status=1
fi

[ "$status" -eq 0 ] && echo "no private names found"
exit "$status"
