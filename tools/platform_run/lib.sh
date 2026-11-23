# Shared by windows-vm.sh and macos-vm.sh (sourced, not run).
# shellcheck shell=bash disable=SC2034  # the settings are used by the scripts that source it
#
# make_payload VERSION DIR fills DIR with what a VM needs to test a published pysacd release:
#   tree.tar   the repository at tag vVERSION without src/, so every test, and every subprocess
#              the tests start, imports pysacd from the installed wheel, not from a checkout
#   qs.tar     this checkout's README.md and tools/readme_quickstart.py (the README's Quick start
#              as the release test plan runs it; tags up to 0.5.5 predate the tool)
#   album.iso  the disc the Quick start reads, made by `readme_quickstart.py make-disc` in a
#              Fedora 44 container (it needs numpy and a C compiler, which the VMs don't have)
# Needs git, tar, and rootless podman with network access on the machine that runs it.

# The Python the VMs install when they have none of 3.10 or later: python.org's 3.13.17, the
# files checked against python.org's sigstore bundles (identity thomas@python.org).
PYTHON_VERSION=3.13.17
PYTHON_MAC_PKG_SHA256=3053592af66128269efab37fe7851a4894920b27c442aeec99ce4d34d17c07e3
PYTHON_WIN_EXE_SHA256=5da012101ba2e17113a9a1cd901e9ce1441086f2b7ecb5de06ec94163899a190

FEDORA_IMAGE=registry.fedoraproject.org/fedora:44

make_payload() {
    local version=$1 dir=$2 repo stage
    repo=$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)
    git -C "$repo" fetch -q --tags origin 2>/dev/null || true
    git -C "$repo" rev-parse -q --verify "refs/tags/v$version" >/dev/null \
        || { echo "no tag v$version in $repo" >&2; return 1; }
    mkdir -p "$dir"
    stage=$(mktemp -d "$dir/stage.XXXXXX")
    mkdir "$stage/tree" "$stage/qs" "$stage/qs/tools"
    git -C "$repo" archive "v$version" | tar -x -C "$stage/tree"
    rm -rf "$stage/tree/src"
    tar -C "$stage/tree" -cf "$dir/tree.tar" .
    cp "$repo/README.md" "$stage/qs/"
    cp "$repo/tools/readme_quickstart.py" "$stage/qs/tools/"
    tar -C "$stage/qs" -cf "$dir/qs.tar" .
    # The disc, from this checkout's generators, as the release test plan's Quick start step.
    git -C "$repo" archive HEAD Makefile tests tools | tar -x -C "$stage" --one-top-level=disc-src
    podman run --rm --pull=newer --memory 4g --cpus 2 -v "$stage/disc-src:/src:ro,Z" \
        -v "$dir:/out:Z" "$FEDORA_IMAGE" bash -euc '
        dnf -y -q install python3 python3-numpy gcc make findutils >/dev/null
        cp -r /src /qs-src && make -s -C /qs-src test-tools
        python3 -I /qs-src/tools/readme_quickstart.py make-disc /out/album.iso'
    rm -rf "$stage"
    ls -l "$dir"
}
