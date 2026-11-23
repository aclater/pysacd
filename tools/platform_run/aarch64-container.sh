#!/bin/bash
# aarch64-container.sh wheel VERSION | source [REF]: pysacd's whole suite on aarch64 Linux, in a
# Fedora 44 arm64 container.
#
#   wheel VERSION   the published manylinux aarch64 wheel of VERSION from PyPI, against the
#                   suite from the tag vVERSION (src/ left out, so the wheel is what's tested)
#   source [REF]    REF (default HEAD) built from source in the container as CI builds it
#                   (`make`, `make test-tools`), the suite, then tools/check_skips.py --job tests
#
# Needs: bash, git, GNU tar, python3, rootless podman with network, and this checkout. On an
# x86-64 machine the container runs under qemu-user emulation, which needs qemu-user-static
# (Fedora: qemu-user-static-aarch64) installed by an administrator; the script checks for it
# first and stops if it's missing. On an aarch64 machine it runs natively. Emulation is about 10
# to 30 times slower: the run is untimed. The arm64 image is used by digest, so the host's
# fedora:44 tag (usually x86-64, shared with other users) is never retagged.
#
# Resources: CPUS (default 8) and MEMORY (default 8g) for the container; the suite runs with
# PYSACD_TEST_MEMORY_MB=4096 as in CI. Run it niced, and inside a memory-capped scope when the
# machine is shared, e.g.
#   systemd-run --user --scope -p MemoryMax=24G -p MemorySwapMax=0 nice -n 19 \
#     tools/platform_run/aarch64-container.sh wheel 0.5.5
# Output: OUT (default ./platform-run-aarch64-MODE), with log.txt, junit.xml and report.json.
# ARCH=amd64 runs the same steps on x86-64, to check the script itself.
set -euo pipefail
MODE=${1:?usage: aarch64-container.sh wheel VERSION | source [REF]}
ARCH=${ARCH:-arm64}
CPUS=${CPUS:-8}
MEMORY=${MEMORY:-8g}
IMAGE=registry.fedoraproject.org/fedora:44
here=$(cd "$(dirname "$0")" && pwd)
repo=$(git -C "$here" rev-parse --show-toplevel)
case $MODE in
wheel) VERSION=${2:?usage: aarch64-container.sh wheel VERSION}; REF=v$VERSION ;;
source) REF=${2:-HEAD}; VERSION= ;;
*) echo "usage: aarch64-container.sh wheel VERSION | source [REF]" >&2; exit 2 ;;
esac
OUT=$(realpath -m "${OUT:-platform-run-$ARCH-$MODE}")
mkdir -p "$OUT"
exec > >(tee "$OUT/log.txt") 2>&1
echo "== $ARCH $MODE run of pysacd $REF ($(git -C "$repo" rev-parse --short "$REF^{commit}")), $(date -u +%FT%TZ)"

digest=$(podman manifest inspect "$IMAGE" | python3 -I -c '
import json, sys
arch = sys.argv[1]
m = [x["digest"] for x in json.load(sys.stdin)["manifests"] if x["platform"]["architecture"] == arch]
print(m[0])' "$ARCH")
image=${IMAGE%:*}@$digest
echo "== image $image"
if ! podman run --rm --arch "$ARCH" "$image" true 2>/dev/null; then
    echo "cannot run $ARCH containers here: install qemu-user-static for $ARCH (an administrator's job)" >&2
    exit 3
fi

src=$(mktemp -d "$OUT/src.XXXXXX")
trap 'rm -rf "$src"' EXIT
git -C "$repo" archive "$REF" | tar -x -C "$src"
[ "$MODE" = wheel ] && rm -rf "$src/src"

start=$(date +%s)
status=0
podman run --rm --arch "$ARCH" --memory "$MEMORY" --memory-swap "$MEMORY" --cpus "$CPUS" \
    --cap-add SYS_PTRACE -e MODE="$MODE" -e VERSION="$VERSION" -e CPUS="$CPUS" \
    -v "$src:/src:ro,Z" -v "$OUT:/out:Z" "$image" bash -euc '
echo "== $(uname -m), $(nproc) CPUs"
if [ "$MODE" = source ]; then
    pkgs="clang compiler-rt llvm libasan libubsan liblsan libtsan valgrind cmake libxml2-devel glibc-gconv-extra flac-devel"
else
    pkgs=
fi
dnf -y -q install shadow-utils util-linux git-core gcc make pkgconf-pkg-config findutils binutils \
    diffutils file which python3.13 python3.13-devel $pkgs >/dev/null
useradd -m -d /build ci && cp -a /src /build/pysacd && chown -R ci /build
runuser -u ci -- env MODE="$MODE" VERSION="$VERSION" CPUS="$CPUS" HOME=/build bash -euc "
cd ~/pysacd && mkdir -p ~/tmp && export TMPDIR=~/tmp LANG=C.UTF-8 MAKEFLAGS=-j\$CPUS
python3.13 -m venv ~/venv && . ~/venv/bin/activate
if [ \"\$MODE\" = source ]; then
    pip -q install --require-hashes -r requirements-dev.txt
    pip -q install --no-deps --no-build-isolation -e .
    make && make test-tools
    pp=src; desel=
else
    pip -q install --only-binary :all: pysacd==\$VERSION
    pip -q install --require-hashes -r requirements-test.txt
    make test-tools
    pp=; desel=--deselect=tests/test_check_release_tag.py::CheckReleaseTag::test_this_repository_agrees_with_itself
fi
pip list 2>/dev/null | grep -i pysacd
fails=0
PYSACD_TEST_MEMORY_MB=4096 PYSACD_TEST_REPORT=report.json PYTHONPATH=\$pp \
    python -m pytest -q -p no:cacheprovider --junitxml=junit.xml -rfEs \$desel tests || fails=\$((fails + 1))
cp report.json junit.xml /out/ || true
if [ \"\$MODE\" = source ]; then
    python3 -I tools/check_skips.py --job tests report.json || fails=\$((fails + 1))
fi
python -I tests/wheel_smoke.py >/dev/null && echo wheel_smoke: passed || fails=\$((fails + 1))
exit \$fails
"' || status=$?
echo "== run time $(( $(date +%s) - start )) s"
echo "== status $status (0: every step passed)"
exit "$status"
