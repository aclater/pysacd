#!/bin/bash
# shellcheck disable=SC2029,SC2329  # remote commands expand here by design; cleanup runs from trap
# macos-vm.sh VERSION: the published pysacd VERSION on macOS (arm64), in a throwaway tart VM.
#
# Runs on a Linux machine (the controller) that reaches, by ssh, a Mac that runs tart. On the
# Mac it only drives tart and the VM: it clones BASE_VM, boots the clone without graphics, audio,
# USB accessories or clipboard, runs everything inside the clone, then stops and deletes it.
#
# Needs:
#   controller   bash, git, GNU tar, rootless podman with network (lib.sh), this checkout with
#                the tag vVERSION
#   TART_HOST    ssh destination of the Mac (key login, no prompts)
#   SSH          the ssh command to reach TART_HOST (default: ssh), e.g. with -F CONFIG
#   TART         command that runs tart there (default: tart), e.g. with TART_HOME set
#   BASE_VM      a tart macOS VM with Remote Login on, never changed: the run uses a clone
#   VM_KEY       path, on the Mac, of the private key the VM's user accepts
#   VM_USER      the VM's admin user (default: admin); VM_PASSWORD its sudo password (default:
#                admin, as in tart's published images), used only to install Python
#   the VM       network access to pypi.org and python.org; about 2 GB free
# Python: the VM's own 3.10 or later, else python.org's (version and SHA-256 in lib.sh).
#
# Does, in the VM: pip install pysacd==VERSION (the wheel from PyPI, never a source build), the
# test requirements from the tag with --require-hashes, then the tag's whole suite against the
# installed wheel, tests/wheel_smoke.py, and the README's Quick start on a generated disc.
# Output: OUT (default ./platform-run-macos-VERSION), with log.txt, junit.xml and report.json.
# Exit status 0 only when every step passed. Untimed: run times are reported, not compared.
set -euo pipefail
VERSION=${1:?usage: macos-vm.sh VERSION}
: "${TART_HOST:?ssh destination of the Mac that runs tart}"
: "${BASE_VM:?the tart VM to clone}"
: "${VM_KEY:?path on the Mac of the key the VM accepts}"
TART=${TART:-tart}
VM_USER=${VM_USER:-admin}
VM_PASSWORD=${VM_PASSWORD:-admin}
OUT=$(realpath -m "${OUT:-platform-run-macos-$VERSION}")
CLONE=pysacd-run-$$
SSH=${SSH:-ssh}
here=$(cd "$(dirname "$0")" && pwd)
. "$here/lib.sh"

mkdir -p "$OUT"
exec > >(tee "$OUT/log.txt") 2>&1
echo "== macOS run of pysacd $VERSION, $(date -u +%FT%TZ)"
make_payload "$VERSION" "$OUT/payload"

tart() { $SSH "$TART_HOST" "$TART $*"; }
SSH_OPTS="-i $VM_KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=20"
# The guest command is quoted for the Mac's shell, so all of it runs in the VM.
sq() { local s=${1//\'/\'\\\'\'}; printf "'%s'" "$s"; }
guest() { $SSH "$TART_HOST" "ssh $SSH_OPTS $VM_USER@$ip $(sq "$*")"; }
cleanup() {
    tart stop "$CLONE" >/dev/null 2>&1 || true
    tart delete "$CLONE" >/dev/null 2>&1 || true
    echo "== clone $CLONE deleted"
}
trap cleanup EXIT

tart clone "$BASE_VM" "$CLONE"
$SSH "$TART_HOST" "($TART run $CLONE --no-graphics --no-audio --no-usb-accessories --no-clipboard) </dev/null >/dev/null 2>&1 &"
ip=$(tart ip "$CLONE" --wait 300)
for _ in $(seq 60); do guest true 2>/dev/null && break; sleep 5; done

tar -C "$OUT/payload" -cf - tree.tar qs.tar album.iso | guest 'rm -rf run && mkdir run && tar -xf - -C run'

start=$(date +%s)
guest_script=$(cat <<'GUEST'
set -uo pipefail
cd ~/run
echo "== $(sw_vers -productName) $(sw_vers -productVersion) $(uname -m), $(sysctl -n hw.ncpu) CPUs"
py=
for p in /Library/Frameworks/Python.framework/Versions/3.*/bin/python3 /opt/homebrew/bin/python3 /usr/bin/python3; do
    [ -x "$p" ] && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null && { py=$p; break; }
done
if [ -z "$py" ]; then
    echo "== no Python 3.10 or later: installing python.org's $PYV"
    curl -fsSLO "https://www.python.org/ftp/python/$PYV/python-$PYV-macos11.pkg"
    echo "$SHA  python-$PYV-macos11.pkg" | shasum -a 256 -c - || exit 1
    echo "$PW" | sudo -S -p '' installer -pkg "python-$PYV-macos11.pkg" -target / >/dev/null || exit 1
    py=/Library/Frameworks/Python.framework/Versions/${PYV%.*}/bin/python3
fi
echo "== $("$py" -V)"
mkdir tree qs work && tar -xf tree.tar -C tree && tar -xf qs.tar -C qs && mv album.iso work/
"$py" -m venv venv && . venv/bin/activate
python -m pip -q install --only-binary :all: "pysacd==$VERSION" || exit 1
python -m pip -q install --require-hashes -r tree/requirements-test.txt || exit 1
python -m pip list 2>/dev/null | grep -i pysacd
fails=0
echo "== the $VERSION suite against the installed wheel"
(cd tree && PYSACD_TEST_REPORT=../report.json python -m pytest -q -p no:cacheprovider \
    --junitxml=../junit.xml -rfEs --deselect tests/test_check_release_tag.py::CheckReleaseTag::test_this_repository_agrees_with_itself tests 2>&1 | grep -v '^SKIPPED ') || fails=$((fails + 1))
echo "== tests/wheel_smoke.py"
(cd work && python -I ../tree/tests/wheel_smoke.py) || fails=$((fails + 1))
echo "== README Quick start"
python -m pip -q download --no-deps --only-binary :all: -d wheel "pysacd==$VERSION" || exit 1
deactivate
PATH="$(dirname "$py"):$PATH" python3 -I qs/tools/readme_quickstart.py run --wheel wheel/*.whl \
    --workdir work --tracks 3 || fails=$((fails + 1))
exit $fails
GUEST
)
status=0
{ printf 'VERSION=%q PYV=%q SHA=%q PW=%q\n' "$VERSION" "$PYTHON_VERSION" "$PYTHON_MAC_PKG_SHA256" \
    "$VM_PASSWORD"; printf '%s\n' "$guest_script"; } | guest /bin/bash -s || status=$?
echo "== run time $(( $(date +%s) - start )) s"
for f in junit.xml report.json; do guest "cat run/$f" > "$OUT/$f" 2>/dev/null || true; done
echo "== status $status (0: every step passed)"
exit "$status"
