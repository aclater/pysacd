#!/usr/bin/env bash
# Fail if tracked files or commit identities contain private details: names in
# the home.arpa special-use domain, private (RFC 1918) IPv4 addresses, local
# mount and home paths, plus the regex in $PRIVATE_NAMES (kept outside the
# repo). Documentation addresses (192.0.2.0/24 and friends) are fine.
# Commits are checked from $BASE_SHA (exclusive) to HEAD, or all of history.
#
# Exit status: 0 = clean, 1 = private details found, 2 = the check could not
# run (bad $PRIVATE_NAMES, git or grep error). It never passes when it could
# not look, and it never prints $PRIVATE_NAMES itself.
set -euo pipefail

self=scripts/check-private-names.sh

fail() {
    echo "::error::private-names check could not run: $*"
    exit 2
}

top=$(git rev-parse --show-toplevel) || fail "not in a git work tree"
cd "$top"

pattern='\.home\.arpa\b|\b192\.168\.[0-9]{1,3}\.[0-9]{1,3}\b|\b10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\b'
pattern="$pattern"'|\b172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}\b|/mnt/[A-Za-z]|/home/[a-z][a-z0-9_-]*/'
if [ -n "${PRIVATE_NAMES:-}" ]; then
    # A blank value or one that matches an empty or blank line (an empty
    # alternative such as a leading or trailing "|") would match every line.
    if [[ $PRIVATE_NAMES =~ ^[[:space:]]*$ ]]; then
        fail "PRIVATE_NAMES is blank"
    fi
    rc=0
    printf '\n \n' | grep -qE -e "$PRIVATE_NAMES" >/dev/null 2>&1 || rc=$?
    case $rc in
        0) fail "PRIVATE_NAMES matches an empty line" ;;
        1) ;;
        *) fail "PRIVATE_NAMES is not a valid extended regex (grep exit $rc)" ;;
    esac
    pattern="$pattern|$PRIVATE_NAMES"
fi
status=0

# Tracked files. git grep: 0 = match, 1 = none, anything else = error.
# This script itself spells the patterns out.
rc=0
git --no-pager grep -nIiE -e "$pattern" -- . ":(exclude)$self" || rc=$?
case $rc in
    0)
        echo "::error::private details found in tracked files (matches above)"
        status=1
        ;;
    1) ;;
    *) fail "git grep exited $rc" ;;
esac

# Commit identities (none to check before the first commit).
if git rev-parse --verify -q HEAD >/dev/null; then
    range=HEAD
    if [ -n "${BASE_SHA:-}" ] && [ "${BASE_SHA}" != "0000000000000000000000000000000000000000" ] \
            && git cat-file -e "${BASE_SHA}^{commit}" 2>/dev/null; then
        range="${BASE_SHA}..HEAD"
    fi
    log=$(git log --format='%h %an <%ae> %cn <%ce>' "$range") || fail "git log exited $?"
    rc=0
    printf '%s\n' "$log" | grep -iE -e "$pattern" || rc=$?
    case $rc in
        0)
            echo "::error::private details found in commit identities (matches above)"
            status=1
            ;;
        1) ;;
        *) fail "grep exited $rc" ;;
    esac
fi

[ "$status" -eq 0 ] && echo "no private details found"
exit "$status"
