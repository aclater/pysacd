#!/usr/bin/env python3
"""Release quality comparison: measure the previous release's PCM output and the candidate's.

    python3 tools/quality_compare.py --previous 0.5.5 --candidate . --work /var/tmp/pq \\
        --out docs/quality/0.6.0.md

Installs the previous release from PyPI and the candidate (a source tree or a wheel) side by side,
each in its own virtual environment, installs dsd-measure at the commit pinned in
suites/pysacd.toml, runs the suite on each version on this machine with the same inputs, runs the
null test between them, and writes docs/quality/<version>.md: one table and one conclusion
sentence, with changes beyond the suite's repeatability bounds flagged.

Results in --work are reused only for the same run: each report folder records the pysacd builds
(wheel SHA-256, or the tree's commit and content hash, and what `pysacd --version` printed), the
dsd-measure version and installed commit, the SHA-256 of the measured suite text and of the device
wrapper, and every input's SHA-256; the report's own provenance must agree (dsd-measure version,
pysacd version). Anything that differs or lacks this record is measured again.

Every comparison is checked to be like for like: dsd-measure reads each output file's word length
and rate, and both versions must have written the format the suite names ([pysacd_quality.output],
24-bit integer) at the same rate, or the page is void. Figures within 6 dB of dsd-measure's
reference floor, and figures the output word length may limit, are marked on the page.

Everything it downloads: the previous release (pip, from PyPI), dsd-measure (pip, from its git
repository at the pinned commit, unless --dsd-measure gives a checkout) and their Python
dependencies, and whatever building the candidate from source downloads. See
docs/development.md, "Release quality comparison".
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:     # Python 3.10: the module imports (its tests run); main() says what it needs
    tomllib = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "suites" / "pysacd.toml"

# The null-test source: band-limited pink noise and three tones, stereo, about -20 dBFS RMS,
# encoded to DSD64 by the reference modulator. Deterministic for a seed and a dsd-measure commit.
NULL_SOURCE = r"""
import sys
import numpy as np
from dsd_measure.encode import encode
from dsd_measure.dsf import write_dsf
seconds, seed, path = float(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
fs = 88200
n = int(seconds * fs)
rng = np.random.default_rng(seed)
chans = []
for c in range(2):
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / fs)
    shape = np.where((f >= 20) & (f <= 20000), 1 / np.sqrt(np.maximum(f, 20)), 0.0)
    x = np.fft.irfft(spec * shape, n)
    x *= 10 ** (-23 / 20) / np.sqrt(np.mean(x ** 2))
    t = np.arange(n) / fs
    for k, (freq, db) in enumerate(((220.0, -26), (1760.0 + 3 * c, -30), (7040.0, -36))):
        x += 10 ** (db / 20) * np.sqrt(2) * np.sin(2 * np.pi * freq * t + k)
    ramp = np.minimum(1, np.minimum(t, t[::-1]) / 0.05)
    chans.append(x * ramp)
bits = np.array([encode(x, fs)[0] for x in chans])
write_dsf(path, bits, 2822400)
"""


# What dsd-measure itself says about one device report, with its own rules (run in its environment):
# the figures at the reference floor, whether the output word length may limit the result, and the
# conditions (DSD rate, output rate, word length) that decide whether two reports are like for like.
ANALYSE = r"""
import json, sys
from dsd_measure.conclusions import condition_text, conditions, format_limit
from dsd_measure.floor import flags
out = {}
for path in sys.argv[1:]:
    M = json.load(open(path))["metrics"]
    out[path] = {"floor": sorted(flags(M.get("device_class", "A"), M)), "format_limited": bool(format_limit(M)),
                 "conditions": conditions(M), "condition_text": condition_text(M),
                 "output_format": M.get("output_format")}
json.dump(out, sys.stdout)
"""

WRAPPER = [ROOT / "tools" / "quality_device.py", ROOT / "tests" / "sacdgen.py"]


def say(msg: str) -> None:
    print(f"quality_compare: {msg}", flush=True)


def run(cmd: list[str], **kw) -> None:
    say(" ".join(str(c) for c in cmd))
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def venv(path: Path, python: str, system_site: bool = False) -> Path:
    if not (path / "bin" / "python").exists():
        run([python, "-m", "venv"] + (["--system-site-packages"] if system_site else []) + [path])
    return path / "bin"


def lookup(data: dict, dotted: str):
    for part in dotted.split("."):
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    return data


def safe(name: str) -> str:
    """dsd-measure's report folder name for a device."""
    import re
    return re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-")


def env_for(bindir: Path) -> dict:
    return dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}")


def installed_commit(bindir: Path) -> str | None:
    """The commit pip recorded for a dsd-measure installed from git (PEP 610 direct_url.json), or None."""
    code = ("import json, importlib.metadata as m; d = m.distribution('dsd-measure').read_text('direct_url.json'); "
            "print(json.loads(d).get('vcs_info', {}).get('commit_id', '') if d else '')")
    r = subprocess.run([bindir / "python", "-I", "-c", code], capture_output=True, text=True, check=False)
    return r.stdout.strip() or None


def install_dsd_measure(work: Path, cfg: dict, checkout: Path | None, python: str) -> tuple[Path, dict]:
    """Install dsd-measure at the pinned commit; return its bin folder and its identity (version, commit)."""
    b = venv(work / "venv-dsd-measure", python, system_site=True)
    if checkout:
        head = subprocess.run(["git", "-C", checkout, "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
        if head != cfg["dsd_measure_commit"]:
            sys.exit(f"--dsd-measure is at {head}, the suite pins {cfg['dsd_measure_commit']}")
        run([b / "python", "-m", "pip", "install", "-q", "--no-build-isolation", checkout])
        commit = head
    else:
        if installed_commit(b) != cfg["dsd_measure_commit"]:   # none yet, or another commit: replace it
            subprocess.run([b / "python", "-m", "pip", "uninstall", "-q", "-y", "dsd-measure"], check=False,
                           capture_output=True)
            run([b / "python", "-m", "pip", "install", "-q",
                 f"dsd-measure @ git+{cfg['dsd_measure_repository']}@{cfg['dsd_measure_commit']}"])
        commit = installed_commit(b)
        if commit != cfg["dsd_measure_commit"]:
            sys.exit(f"dsd-measure installed from commit {commit}, the suite pins {cfg['dsd_measure_commit']}")
    got = subprocess.run([b / "dsd-measure", "--version"], capture_output=True, text=True, check=True).stdout.strip()
    got = got.split()[-1] if got else got           # "dsd-measure 0.1.2" or "0.1.2"
    if got != cfg["dsd_measure_version"]:
        sys.exit(f"dsd-measure {got} installed, the suite pins {cfg['dsd_measure_version']}")
    return b, {"version": got, "commit": commit}


def is_path(spec: str) -> bool:
    """--previous names a wheel file or a folder only when it looks like one; otherwise it is a
    version on PyPI, even if a file of that name happens to exist here."""
    return spec.endswith(".whl") or "/" in spec or os.sep in spec


# A null this deep between two runs of the candidate means they gave the same samples (24-bit
# rounding alone is about 145 dB down): the re-run is the measurement's floor, not dither
REPEAT_IDENTICAL_DB = 250.0


def tree_hash(root: Path) -> str:
    """SHA-256 over a source tree's files (names and contents), build output and VCS data left out."""
    skip_dirs = {".git", "build", "dist", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache"}
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(part in skip_dirs or part.endswith(".egg-info") for part in rel.parts) or not p.is_file() \
                or p.suffix in (".so", ".dll", ".dylib", ".pyc"):
            continue
        h.update(rel.as_posix().encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def git_commit(root: Path) -> str | None:
    r = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def install_pysacd(work: Path, label: str, spec: str, python: str, from_pypi: bool) -> tuple[Path, dict]:
    """Install one pysacd; return its bin folder and what identifies the build that was installed."""
    b = venv(work / f"venv-{label}", python)
    if from_pypi:   # download the wheel first, so its SHA-256 is known, then install that file
        wheels = work / "wheels" / label
        shutil.rmtree(wheels, ignore_errors=True)
        run([b / "python", "-m", "pip", "download", "-q", "--no-deps", "--only-binary", ":all:", "-d", wheels,
             f"pysacd=={spec}"])
        found = sorted(wheels.glob("*.whl"))
        if len(found) != 1:
            sys.exit(f"pip download pysacd=={spec} gave {len(found)} wheels")
        path = found[0]
    else:
        path = Path(spec).resolve()
        if not path.exists():
            sys.exit(f"{spec}: no such wheel or source tree")
    # a wheel and its dependencies as wheels only (no setup code from an index runs); a tree is built
    binary = ["--only-binary", ":all:"] if path.suffix == ".whl" else []
    run([b / "python", "-m", "pip", "install", "-q", "--force-reinstall", *binary, path])
    if path.suffix == ".whl":
        ident = {"kind": "wheel", "file": path.name, "sha256": sha256(path)}
    else:
        ident = {"kind": "tree", "commit": git_commit(path), "tree_sha256": tree_hash(path)}
    ident["version"] = version_of(b)
    return b, ident


def version_of(bindir: Path) -> str:
    out = subprocess.run([bindir / "pysacd", "--version"], capture_output=True, text=True, check=True).stdout
    return out.split()[1]


META = "quality-meta.json"


def reusable(out: Path, want: dict, reports: list[Path]) -> bool:
    """Whether ``out`` holds results of exactly this run.

    ``want`` identifies the run (pysacd builds, dsd-measure version and commit, suite file, source).
    The folder's metadata must match it key for key, and the input hashes recorded there must
    still be those in each report's provenance. Anything else, including no metadata, is stale.
    """
    try:
        meta = json.loads((out / META).read_text())
    except (OSError, ValueError):
        return False
    if meta.get("run") != want:
        return False
    recorded = meta.get("inputs")
    if not isinstance(recorded, dict) or sorted(recorded) != sorted(r.name for r in reports):
        return False
    try:
        return all(recorded[r.name] == input_hashes(r) and provenance_matches(r, want) for r in reports)
    except (OSError, ValueError, KeyError):
        return False


def provenance_matches(report: Path, want: dict) -> bool:
    """Whether a report's own provenance names the dsd-measure version, and the pysacd version, of ``want``."""
    prov = json.loads((report / "provenance.json").read_text())
    dm = (want.get("dsd_measure") or {}).get("version")
    if dm and (prov.get("versions") or {}).get("dsd_measure") != dm:
        return False
    builds = want.get("pysacd")
    if isinstance(builds, dict) and "device_versions" in prov:   # a suite report: the device ran that pysacd
        printed = str((prov["device_versions"] or {}).get("version") or "")
        if printed.split()[1:2] != [builds.get("version")]:
            return False
    return True


def record(out: Path, want: dict, reports: list[Path]) -> None:
    (out / META).write_text(json.dumps({"run": want, "inputs": {r.name: input_hashes(r) for r in reports}},
                                       indent=1, sort_keys=True))


def fresh(out: Path, want: dict, reports: list[Path]) -> bool:
    """True when ``out`` can be reused; otherwise clears it so the caller runs again."""
    if out.exists() and reusable(out, want, reports):
        say(f"{out} matches this run, reusing it")
        return True
    if out.exists():
        say(f"{out} is from another run or has no metadata: measuring again")
        shutil.rmtree(out)
    return False


def measured_suite(suite_text: str, lead: int) -> str:
    """The part of suites/pysacd.toml that dsd-measure runs, with the signal shifted by ``lead`` idle frames.

    [pysacd_quality] is left out, so editing a bound or a note does not make the reports stale.
    """
    text = suite_text.split("\n[pysacd_quality]")[0]
    if lead:
        text = text.replace("{input} {output}", f"--lead-frames {lead} {{input}} {{output}}")
    return text.rstrip("\n") + "\n"


def measure(dm: Path, bindir: Path, suite: Path, out: Path, want: dict) -> None:
    want = dict(want, suite_sha256=sha256(suite))
    reports = [out / safe(d["name"]) for d in tomllib.loads(suite.read_text())["device"]]
    if fresh(out, want, reports):
        return
    r = subprocess.run([dm / "dsd-measure", "run", suite, "-o", out], cwd=ROOT, env=env_for(bindir), check=False)
    if r.returncode:
        sys.exit(f"dsd-measure run exited {r.returncode} for {out}: see the reports' 'Not run' sections")
    record(out, want, reports)


def device_command(dev: dict, bindir: Path, extra: str = "") -> str:
    return f"{dev['command'].replace('{input}', '').replace('{output}', '').strip()} " \
           f"--pysacd {bindir / 'pysacd'}{extra} {{input}} {{output}}"


def null_test(dm: Path, source: Path, a: tuple[str, str], b: tuple[str, str], out: Path, want: dict) -> float:
    want = dict(want, source_sha256=sha256(source), devices=[list(a), list(b)])
    if not fresh(out, want, [out]):
        run([dm / "dsd-measure", "null", "--source", source, "--device", f"{a[0]}={a[1]}",
             "--device", f"{b[0]}={b[1]}", "-o", out], cwd=ROOT)
        record(out, want, [out])
    res = json.loads((out / "results.json").read_text())
    v = res["pairs"][0]["verdict"]
    return float(v["depth_db"]), list(v.get("format_limited") or [])


def input_hashes(report: Path) -> list[str]:
    """The SHA-256 of every input a report records: each file fed to a device, or the null source."""
    prov = json.loads((report / "provenance.json").read_text())
    if "adapter_calls" in prov:
        return [c.get("input_sha256", "") for c in prov["adapter_calls"]]
    return [str(prov.get("source", {}).get("sha256", ""))]


def analyse(dm: Path, results: list[Path]) -> dict[Path, dict]:
    """dsd-measure's floor flags, format limit and conditions for each results.json (see ANALYSE)."""
    r = subprocess.run([dm / "python", "-I", "-c", ANALYSE, *results], capture_output=True, text=True, check=True)
    got = json.loads(r.stdout)
    return {p: got[str(p)] for p in results}


def format_problems(name: str, want: dict, found: list[tuple[str, dict]]) -> list[str]:
    """Why the reports of one device are not like for like with each other and with the suite's output, if they are not."""
    out = []
    base = found[0][1]["conditions"]
    for label, a in found:
        c = a["conditions"]
        if c.get("kind") != want.get("kind") or c.get("bits") != want.get("bits"):
            out.append(f"{name}, {label}: wrote {a['condition_text']}; the suite expects "
                       f"{want.get('bits')}-bit {'integer' if want.get('kind') == 'int' else want.get('kind')}")
        if c != base:
            out.append(f"{name}: not like for like ({found[0][0]}: {found[0][1]['condition_text']}; "
                       f"{label}: {a['condition_text']})")
    return out


def fmt(v) -> str:
    return "n/a" if v is None else f"{v:.1f}"


def spread_text(v) -> str:
    return "n/a" if v is None else f"{v:.2f} dB"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--previous", required=True, help="previous release: a version on PyPI, or a wheel (.whl) or tree given as a path with a /")
    ap.add_argument("--candidate", required=True, help="candidate: a source tree or a wheel file")
    ap.add_argument("--work", required=True, type=Path, help="folder for the environments and reports, without spaces (reused only for the same run)")
    ap.add_argument("--out", type=Path, help="Markdown page to write (default docs/quality/<version>.md)")
    ap.add_argument("--dsd-measure", type=Path, help="a dsd-measure checkout at the pinned commit (default: install from git)")
    ap.add_argument("--python", default=sys.executable, help="Python for the environments (default: this one)")
    ap.add_argument("--draft", action="store_true", help="mark the page as a draft (also implied by final = false in the suite)")
    ap.add_argument("--note", action="append", default=[], help="a line for the page's notes (repeatable)")
    a = ap.parse_args(argv)
    if tomllib is None:
        sys.exit("tools/quality_compare.py needs Python 3.11 or later (tomllib), as dsd-measure does")

    suite_text = SUITE.read_text()
    suite = tomllib.loads(suite_text)
    cfg = suite["pysacd_quality"]
    devices = suite["device"]
    work = a.work.resolve()
    if any(c.isspace() for c in str(work)):   # the null test splits device commands on spaces
        sys.exit(f"--work {work}: the path must not contain spaces")
    work.mkdir(parents=True, exist_ok=True)

    dm, dm_ident = install_dsd_measure(work, cfg, a.dsd_measure.resolve() if a.dsd_measure else None, a.python)
    wrapper = {p.relative_to(ROOT).as_posix(): sha256(p) for p in WRAPPER}
    prev, prev_id = install_pysacd(work, "previous", a.previous, a.python, from_pypi=not is_path(a.previous))
    cand, cand_id = install_pysacd(work, "candidate", a.candidate, a.python, from_pypi=False)
    pv, cv = prev_id["version"], cand_id["version"]
    label_c = cv if cv != pv else f"{cv} (candidate)"
    say(f"previous {pv}, candidate {cv}")

    # 1. the suite on each version
    reports = {"previous": work / "reports" / "previous", "candidate": work / "reports" / "candidate"}
    suites = {}
    for lead in [0] + list(cfg["repeatability"]["lead_frames"]):
        suites[lead] = work / f"suite-lead{lead}.toml"
        suites[lead].write_text(measured_suite(suite_text, lead))
    measure(dm, prev, suites[0], reports["previous"], {"pysacd": prev_id, "dsd_measure": dm_ident, "wrapper": wrapper})
    measure(dm, cand, suites[0], reports["candidate"], {"pysacd": cand_id, "dsd_measure": dm_ident, "wrapper": wrapper})

    # 2. repeatability: the candidate again with the signal shifted by idle frames (other dither draws)
    rep_reports = []
    for lead in cfg["repeatability"]["lead_frames"]:
        out = work / "reports" / f"candidate-lead{lead}"
        measure(dm, cand, suites[lead], out, {"pysacd": cand_id, "dsd_measure": dm_ident, "wrapper": wrapper})
        rep_reports.append(out)

    # 3. the null test: previous against candidate, and the candidate against another dither draw
    ncfg = cfg["null"]
    source, source_meta = work / "null-source.dsf", work / "null-source.json"
    made_by = {"dsd_measure": dm_ident, "seconds": ncfg["seconds"], "seed": ncfg["seed"]}
    try:
        same_source = json.loads(source_meta.read_text()) == made_by and source.exists()
    except (OSError, ValueError):
        same_source = False
    if not same_source:
        run([dm / "python", "-I", "-c", NULL_SOURCE, ncfg["seconds"], ncfg["seed"], source])
        source_meta.write_text(json.dumps(made_by))
    src_hash = sha256(source)
    if ncfg.get("source_sha256") and ncfg["source_sha256"] != src_hash:
        sys.exit(f"null source SHA-256 {src_hash} differs from the suite's {ncfg['source_sha256']}")
    nulls, null_floor, null_limited, floor_limited = {}, {}, {}, {}
    for dev in devices:
        d = safe(dev["name"])
        nulls[dev["name"]], null_limited[dev["name"]] = null_test(dm, source, ("previous", device_command(dev, prev)),
                                       ("candidate", device_command(dev, cand)), work / "null" / d,
                                       {"pysacd": [prev_id, cand_id], "dsd_measure": dm_ident, "wrapper": wrapper})
        null_floor[dev["name"]], floor_limited[dev["name"]] = null_test(dm, source, ("candidate", device_command(dev, cand)),
                                            ("candidate-lead1", device_command(dev, cand, " --lead-frames 1")),
                                            work / "null-dither" / d,
                                            {"pysacd": [cand_id, cand_id], "dsd_measure": dm_ident, "wrapper": wrapper})

    # 4. the table
    metrics = cfg["metric"]
    rows, flags, regressions, spreads = [], [], [], {}
    same_inputs = True
    want_out = cfg.get("output", {"kind": "int", "bits": 24})
    labelled = [("previous", reports["previous"]), ("candidate", reports["candidate"])] + \
               [(r.name, r) for r in rep_reports]
    info = analyse(dm, [r / safe(dev["name"]) / "results.json" for _, r in labelled for dev in devices])
    format_issues, formats, floors, limited = [], {}, {}, {}
    for dev in devices:
        d = safe(dev["name"])
        found = [(lab, info[r / d / "results.json"]) for lab, r in labelled]
        format_issues += format_problems(dev["name"], want_out, found)
        formats[dev["name"]] = found[1][1]["condition_text"]
        floors[dev["name"]] = {lab: a["floor"] for lab, a in found[:2]}
        limited[dev["name"]] = [lab for lab, a in found[:2] if a["format_limited"]]
        rp = json.loads((reports["previous"] / d / "results.json").read_text())
        rc = json.loads((reports["candidate"] / d / "results.json").read_text())
        reps = [json.loads((r / d / "results.json").read_text()) for r in rep_reports]
        if input_hashes(reports["previous"] / d) != input_hashes(reports["candidate"] / d):
            same_inputs = False
        for m in metrics:
            vp, vc = lookup(rp, m["path"]), lookup(rc, m["path"])
            draws = [v for v in [vc] + [lookup(r, m["path"]) for r in reps] if v is not None]
            spreads[(dev["name"], m["key"])] = (max(draws) - min(draws)) if len(draws) > 1 else None
            delta = None if vp is None or vc is None else vc - vp
            mark = ""
            if delta is not None and abs(delta) > m["bound"]:
                worse = delta < 0 if m["better"] == "higher" else delta > 0
                mark = " (worse)" if worse else " (better)"
                flags.append(f"{dev['name']}, {m['label']}: {delta:+.2f} dB{mark.strip()}")
                if worse:
                    regressions.append(flags[-1])
            tags = [f"floor ({lab})" for lab in ("previous", "candidate")
                    if m.get("floor_flag") and m["floor_flag"] in floors[dev["name"]][lab]]
            if m["key"] in ("sinad", "thdn") and limited[dev["name"]]:
                tags.append("word length")
            mark += "".join(f" [{t}]" for t in tags)
            rows.append((dev["name"], m["label"], vp, vc, delta, m["bound"], mark))
        depth, floor = nulls[dev["name"]], null_floor[dev["name"]]
        mark = ""
        if depth < floor - ncfg["bound"]:
            mark = " (beyond the re-run)"
            flags.append(f"{dev['name']}, null depth {depth:.1f} dB against {floor:.1f} dB for the candidate's re-run")
        rows.append((dev["name"], "Null depth against the previous release, 20 Hz to 20 kHz (dB)", None, depth,
                     depth - floor, ncfg["bound"], mark))

    draft = a.draft or not cfg.get("final", False)
    out = a.out or ROOT / "docs" / "quality" / f"{cv}.md"
    worst_spread = {m["key"]: max((s for (_, k), s in spreads.items() if k == m["key"] and s is not None), default=None)
                    for m in metrics}
    if regressions:
        conclusion = (f"{len(regressions)} figure(s) of {cv} are worse than {pv} by more than the suite's repeatability; "
                      "this blocks the release unless DECISIONS.md explains and accepts it.")
    elif flags:
        conclusion = (f"{cv} measures the same as or better than {pv}: the changes beyond repeatability are improvements "
                      "or a null less deep than the candidate's re-run gives.")
    else:
        conclusion = f"{cv} measures the same as {pv} within the suite's repeatability on every figure."
    if not same_inputs:
        conclusion += " The two versions were not fed identical inputs, so this comparison is void."
    if format_issues:
        conclusion += " The outputs were not like for like (see notes), so this comparison is void."

    lines = [
        "<!-- nav: generated by tools/docs_index.py -->",
        f"[pysacd](../../README.md) › [Documentation](../README.md) › PCM quality: {cv}",
        "<!-- /nav -->", "",
        f"# PCM quality: {cv}", "",
    ]
    if draft:
        lines += [(f"**DRAFT.** Measured with dsd-measure {dm_ident['version']}"
                  " and not final: the published comparison is re-run with the pinned releases (see notes)."), ""]
    lines += [
        (f"pysacd {cv}'s PCM output against {pv}'s, measured with dsd-measure {dm_ident['version']} "
        f"(commit {dm_ident['commit'][:7]}) on the same machine with the same inputs, from the "
        "[pinned suite](../../suites/pysacd.toml). A change larger than the bound is flagged."), "",
        "## Results", "",
        f"| Output | Figure (unit) | {pv} | {label_c} | Change (dB) | Bound (dB) |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for name, label, vp, vc, delta, bound, mark in rows:
        if vp is None and label.startswith("Null"):
            # a re-run that nulls this deep gave the same samples (an undithered filter): the change
            # against it is no figure of merit, only whether the null is shallower
            ref = vc - delta
            change = (f"re-run identical ({ref:.0f} dB){mark}" if ref >= REPEAT_IDENTICAL_DB
                      else f"{delta:+.1f} vs re-run{mark}")
            lines.append(f"| {name} | {label} | | {fmt(vc)} | {change} | {bound:.1f} |")
        else:
            lines.append(f"| {name} | {label} | {fmt(vp)} | {fmt(vc)} | "
                         f"{'n/a' if delta is None else f'{delta:+.2f}'}{mark} | {bound:.1f} |")
    lines += ["", f"**Conclusion.** {conclusion}", "", "## Notes", ""]
    lines += [(f"- Inputs: {'identical for both versions (SHA-256 of every file fed to pysacd)' if same_inputs else 'NOT identical'}; "
              f"null-test source SHA-256 `{src_hash[:16]}`.")]
    lines += ["- Output format, read by dsd-measure from each file: "
              + "; ".join(f"{n} {formats[n]}" for n in formats)
              + (f". Not like for like: {'; '.join(format_issues)}." if format_issues
                 else f", the same for {pv} and {cv} on every output.")]
    word = [f"{n} ({', '.join(v)})" for n, v in limited.items() if v]
    if word:
        lines += ["- Word length: SINAD of " + "; ".join(word) + " lies within 6 dB of the SINAD that the output's "
                  "integer word length allows (dsd-measure's \"Not judged: output format\"); it describes the file format "
                  "as much as the converter, and both versions write the same format. Marked [word length]."]
    floored = []
    for n, d in floors.items():
        if d["previous"] == d["candidate"]:
            floored += [f"{n}: {', '.join(d['previous'])}"] if d["previous"] else []
        else:
            floored += [f"{n} {lab}: {', '.join(v)}" for lab, v in d.items() if v]
    if floored:
        lines += ["- Reference floor: figures within 6 dB of dsd-measure's reference floor, limited by the measurement "
                  "(dsd-measure's names; those in the table are marked [floor]): " + "; ".join(floored) + "."]
    nl = [f"{n} ({', '.join(v)})" for n, v in null_limited.items() if v]
    if nl:
        lines += ["- Null depth against the previous release is set by the 24-bit rounding noise of the output, "
                  "not the converter, on: " + "; ".join(nl) + " (dsd-measure's format limit)."]
    lines += ["- Repeatability: largest spread over three runs of the candidate with the signal shifted by idle "
              "frames, per figure: " + ", ".join(f"{m['key']} {spread_text(worst_spread[m['key']])}" for m in metrics)
              + ". For a dithered filter (pysacd) the runs are other dither draws; for an undithered one (precise) "
              "they are deterministic re-runs, so the spread is the measurement's, not the converter's."]
    lines += ["- Null depth: " + "; ".join(f"{n} {nulls[n]:.1f} dB (the candidate against its own re-run: {null_floor[n]:.1f} dB)"
                                         for n in nulls) + ". The re-run reference is the measurement's floor for an "
              "undithered filter (the same samples, shifted) and two dither draws for a dithered one; the null "
              "against the previous release is judged against it."]
    lines += [f"- Builds: {pv} is `{prev_id.get('file', 'a source tree')}`"
              + (f" (SHA-256 `{prev_id['sha256']}`)" if "sha256" in prev_id else "")
              + f"; {cv} is " + (f"`{cand_id['file']}` (SHA-256 `{cand_id['sha256']}`)" if cand_id["kind"] == "wheel"
                                 else f"commit `{cand_id['commit'] or 'none (not a git tree)'}`, source SHA-256 "
                                      f"`{cand_id['tree_sha256'][:16]}`") + "."]
    lines += [f"- {n}" for n in a.note]
    lines += ["", "## See also", "", "- [Development: release quality comparison](../development.md#release-quality-comparison)",
              "- [Performance](../performance.md)", "", "Part of the [DSD Toolbox](https://github.com/dsd-toolbox/dsd-toolbox).", ""]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines))
    (work / "summary.json").write_text(json.dumps({"previous": pv, "candidate": cv, "builds": [prev_id, cand_id],
                                                   "dsd_measure": dm_ident, "wrapper": wrapper, "rows": rows, "flags": flags,
                                                   "formats": formats, "format_issues": format_issues,
                                                   "floor_flags": floors, "word_length_limited": limited,
                                                   "null_format_limited": null_limited,
                                                   "null_floor_format_limited": floor_limited,
                                                   "regressions": regressions, "nulls": nulls, "null_floor": null_floor,
                                                   "spreads": {f"{k[0]}|{k[1]}": v for k, v in spreads.items()},
                                                   "null_source_sha256": src_hash, "same_inputs": same_inputs},
                                                  indent=1))
    say(f"wrote {out}: {conclusion}")
    return 1 if regressions or not same_inputs or format_issues else 0


if __name__ == "__main__":
    sys.exit(main())
