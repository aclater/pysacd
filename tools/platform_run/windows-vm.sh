#!/bin/bash
# shellcheck disable=SC2029,SC2329  # remote commands expand here by design; cleanup runs from trap
# windows-vm.sh VERSION: the published pysacd VERSION on Windows 11 (x86-64), in a throwaway VM.
#
# Runs on a Linux machine (the controller) that reaches, by ssh, a Linux host whose user runs a
# Windows 11 VM in libvirt's session mode (qemu:///session). It defines a throwaway copy of that
# VM: a qcow2 overlay on its disk, copies of its UEFI variables and TPM state, its own port
# forward and 8 GiB / 4 vCPUs; runs everything inside the copy, then removes the copy. The base VM
# must stay shut off while the run lasts (its disk is the overlay's backing file).
#
# Needs:
#   controller   bash, git, GNU tar, python3, rootless podman with network (lib.sh), this
#                checkout with the tag vVERSION
#   VM_HOST      ssh destination of the libvirt host, as the user that owns BASE_VM (key login)
#   BASE_VM      that user's Windows 11 domain: UEFI, one qcow2 disk, user-mode networking
#                (passt) with a TCP port forward to the guest's port 22; OpenSSH server with
#                PowerShell as its default shell; network access to pypi.org and python.org
#   VM_KEY       path, on VM_HOST, of the private key VM_USER (an administrator) accepts
#   SSH          the ssh command to reach VM_HOST (default: ssh), e.g. with -F CONFIG
#   VM_USER      that user; SSH_PORT the host port to forward (default: 2232)
#   RUN_DIR      on VM_HOST, on a disk (not tmpfs), for the overlay (default:
#                ~/.local/share/pysacd-platform-run)
# Python: the VM's own 3.10 or later, else python.org's (version and SHA-256 in lib.sh).
#
# Does, in the VM (windows-guest.ps1): pip install pysacd==VERSION (the wheel from PyPI), the test
# requirements from the tag with --require-hashes, then the tag's whole suite against the
# installed wheel, tests/wheel_smoke.py, and the README's Quick start in PowerShell (the README
# writes it for bash; the same steps with Scripts\Activate.ps1) on a generated disc, checking
# each "You should see". windows-guest.ps1 also runs on its own: put it in a folder of any
# Windows VM with tree.tar and album.iso from make_payload, and run it there.
# Output: OUT (default ./platform-run-windows-VERSION), with log.txt, junit.xml and report.json.
# Exit status 0 only when every step passed. Untimed.
set -euo pipefail
VERSION=${1:?usage: windows-vm.sh VERSION}
: "${VM_HOST:?ssh destination of the libvirt host}"
: "${BASE_VM:?the Windows 11 domain to copy}"
: "${VM_KEY:?path on VM_HOST of the key the VM accepts}"
: "${VM_USER:?the VM administrator}"
SSH_PORT=${SSH_PORT:-2232}
RUN_DIR=${RUN_DIR:-'~/.local/share/pysacd-platform-run'}
OUT=$(realpath -m "${OUT:-platform-run-windows-$VERSION}")
NAME=vm-$(od -An -N4 -tx1 /dev/urandom | tr -d " \n")
SSH=${SSH:-ssh}
here=$(cd "$(dirname "$0")" && pwd)
. "$here/lib.sh"

mkdir -p "$OUT"
exec > >(tee "$OUT/log.txt") 2>&1
echo "== Windows run of pysacd $VERSION, $(date -u +%FT%TZ)"
make_payload "$VERSION" "$OUT/payload"

V="virsh -q -c qemu:///session"
vh() { $SSH "$VM_HOST" "$*"; }
# The guest command is quoted for VM_HOST's shell, so all of it runs in the VM.
sq() { local s=${1//\'/\'\\\'\'}; printf "'%s'" "$s"; }
guest() { $SSH "$VM_HOST" "ssh -q -i $VM_KEY -p $SSH_PORT -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 $VM_USER@127.0.0.1 $(sq "$*")"; }

[ "$(vh "$V domstate $BASE_VM")" = "shut off" ] || { echo "$BASE_VM must be shut off" >&2; exit 1; }
run=$(vh "mkdir -p $RUN_DIR/$NAME && cd $RUN_DIR/$NAME && pwd")
uuid=$(python3 -c 'import uuid; print(uuid.uuid4())')
base_uuid=$(vh "$V domuuid $BASE_VM")

cleanup() {
    vh "$V destroy $NAME" >/dev/null 2>&1 || true
    vh "$V undefine $NAME --nvram --tpm" >/dev/null 2>&1 || true
    vh "rm -rf $run ~/.config/libvirt/qemu/swtpm/$uuid" || true
    echo "== VM $NAME removed"
}
trap cleanup EXIT

# The copy's definition: the base's, renamed, on an overlay, with its own firmware variables,
# serial log and port, without CD-ROMs or a fixed MAC address.
vh "$V dumpxml --inactive $BASE_VM" | python3 -I -c '
import sys, xml.etree.ElementTree as ET
name, uuid, run, port = sys.argv[1:]
d = ET.fromstring(sys.stdin.read())
d.find("name").text, d.find("uuid").text = name, uuid
for tag, val in (("memory", "8"), ("currentMemory", "8")):
    e = d.find(tag)
    if e is not None:
        e.text, e.attrib = val, {"unit": "GiB"}
d.find("vcpu").text = "4"
nv = d.find("os/nvram")
nv.text = run + "/VARS." + (nv.get("format") or "fd")
nv.attrib.pop("template", None); nv.attrib.pop("templateFormat", None)
devs = d.find("devices")
disks = [x for x in devs.findall("disk") if x.get("device") == "disk"]
assert len(disks) == 1, "the base VM must have one disk"
disks[0].find("source").set("file", f"{run}/disk.qcow2")
disks[0].find("driver").set("type", "qcow2")
for x in devs.findall("disk"):
    if x.get("device") != "disk":
        devs.remove(x)
for i in devs.findall("interface"):
    for m in i.findall("mac"):
        i.remove(m)
    for pf in i.findall("portForward/range"):
        pf.set("start", port)
for tag in ("serial", "console"):
    for s in devs.findall(tag):
        src = s.find("source")
        if src is not None and src.get("path"):
            src.set("path", f"{run}/com1.log")
print(ET.tostring(d, encoding="unicode"))
' "$NAME" "$uuid" "$run" "$SSH_PORT" > "$OUT/payload/domain.xml"
grep -q "<source file=\"$run/disk.qcow2\"" "$OUT/payload/domain.xml"

base_disk=$(vh "$V domblklist $BASE_VM --details" | awk '$2 == "disk" {print $4; exit}')
base_nvram=$(vh "$V dumpxml --inactive $BASE_VM" | sed -n 's|.*<nvram[^>]*>\(.*\)</nvram>.*|\1|p')
nv_copy=$(sed -n 's|.*<nvram[^>]*>\(.*\)</nvram>.*|\1|p' "$OUT/payload/domain.xml")
vh "qemu-img create -q -f qcow2 -F qcow2 -b $base_disk $run/disk.qcow2 && cp $base_nvram $nv_copy \
    && if [ -d ~/.config/libvirt/qemu/swtpm/$base_uuid ]; then cp -a ~/.config/libvirt/qemu/swtpm/$base_uuid ~/.config/libvirt/qemu/swtpm/$uuid; fi"
vh "cat > $run/domain.xml" < "$OUT/payload/domain.xml"
vh "$V define $run/domain.xml >/dev/null && $V start $NAME"
echo "== $NAME started; waiting for ssh"
for _ in $(seq 120); do guest 'exit 0' 2>/dev/null && break; sleep 5; done

cp "$here/windows-guest.ps1" "$OUT/payload/guest.ps1"

tar -C "$OUT/payload" -cf - tree.tar album.iso guest.ps1 \
    | guest 'New-Item -ItemType Directory -Force run | Out-Null; tar -xmf - -C run'
start=$(date +%s)
status=0
guest "powershell -NoProfile -ExecutionPolicy Bypass -File run\\guest.ps1 -Version $VERSION -PythonVersion $PYTHON_VERSION -PythonSha256 $PYTHON_WIN_EXE_SHA256" || status=$?
echo "== run time $(( $(date +%s) - start )) s"
for f in junit.xml report.json; do guest "Get-Content -Raw run\\$f" > "$OUT/$f" 2>/dev/null || true; done
echo "== status $status (0: every step passed)"
exit "$status"
