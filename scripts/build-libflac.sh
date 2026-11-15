#!/bin/sh
# Build a static libFLAC from its release source, SHA-256 checked, and install it into PREFIX
# (#153). The Linux wheels link it into the engine, as the macOS wheels do (#142), instead of
# bundling the build image's distro libFLAC and libogg. No Ogg, no programs or C++ library;
# position-independent code, for linking into the shared engine.
#
#   FLAC_VERSION=1.5.0 FLAC_SHA256=... scripts/build-libflac.sh PREFIX
#
# The workflow (.github/workflows/workflow.yml) sets FLAC_VERSION and FLAC_SHA256 and runs this
# inside the cibuildwheel containers (manylinux and musllinux). Needs curl, sha256sum, tar with xz,
# make and a C compiler. Then PKG_CONFIG_PATH=PREFIX/lib/pkgconfig makes the Makefile find it.
set -eu

prefix=${1:?usage: build-libflac.sh PREFIX}
: "${FLAC_VERSION:?FLAC_VERSION is not set}" "${FLAC_SHA256:?FLAC_SHA256 is not set}"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

curl -fsSL --retry 3 -o "$work/flac.tar.xz" \
    "https://github.com/xiph/flac/releases/download/$FLAC_VERSION/flac-$FLAC_VERSION.tar.xz"
echo "$FLAC_SHA256  $work/flac.tar.xz" | sha256sum -c -
tar -xJf "$work/flac.tar.xz" -C "$work"

# FLAC's configure adds -O3 -funroll-loops, _FORTIFY_SOURCE and the stack protector itself. Add the
# engine's other hardening flags (Makefile, HARDEN): without -fcf-protection, libFLAC's objects
# would drop the CET shadow-stack mark from the x86-64 engine (#94; tools/check_cet.py).
cflags="-fstack-clash-protection"
case $(uname -m) in
    x86_64 | i?86) cflags="$cflags -fcf-protection" ;;
esac

cd "$work/flac-$FLAC_VERSION"
CFLAGS="$cflags" ./configure --prefix="$prefix" --libdir="$prefix/lib" \
    --disable-shared --enable-static --with-pic \
    --disable-ogg --disable-cpplibs --disable-programs --disable-examples \
    --disable-doxygen-docs --disable-oss-fuzzers --disable-version-from-git
make -j "$(nproc)"
make install
