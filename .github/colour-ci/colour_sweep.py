#!/usr/bin/env python3
"""Main against the colouring branch, paired in rounds on one machine (adapted from placemat's
scripts/probes/sqlite_sweep.py):

    python3 colour_sweep.py --base DIR --colour DIR --cases colour_cases.k [--rounds N] [--seed S] [--label L]
                            [--period P] [--json OUT]
    python3 colour_sweep.py --summary JSON...      (one table over several jobs' JSON files)

DIR is a built checkout (./amber in it). Each arm runs the case file in a fresh process, one thread, from
its own checkout (so each loads its own amber.k and std.k). One discarded run of each arm first; then every
round runs both arms once in a fresh random order. For each case (pass 1 and pass 2 separately) the
per-round ratio colour/base gives a median, how many rounds agree in sign, and a bootstrap 95% interval of
the median; each arm's p90/p10 over rounds shows whether it switches between two speeds. The machine (CPU
model, L1D size, ways, line and set stride) is recorded, since hosted runners mix models from job to job:
compare within a job only.

Writes Markdown to stdout (and to $GITHUB_STEP_SUMMARY when set) and, with --json, every run."""
from __future__ import annotations

import argparse
import json
import os
import platform
import random
import statistics as st
import subprocess
import sys
from pathlib import Path


def cpu_name() -> str:
    """The CPU's model (as placemat's scripts/probes/run.py: hosted runners mix AMD and Intel models)."""
    m = {}
    try:
        for l in Path("/proc/cpuinfo").read_text().splitlines():
            if ":" in l:
                k, v = (s.strip() for s in l.split(":", 1))
                m.setdefault(k, v)
    except OSError:
        pass
    if m.get("model name"):
        return m["model name"]
    if m.get("CPU implementer"):
        return f"arm {m.get('CPU implementer')}/{m.get('CPU part', '?')}"
    return sysctl("machdep.cpu.brand_string") or "?"


def sysctl(name: str) -> str:
    try:
        return subprocess.run(["sysctl", "-n", name], capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


def l1d() -> dict:
    """L1D size, ways, line and set stride (size/ways): sysfs on Linux, sysctl on macOS."""
    d = Path("/sys/devices/system/cpu/cpu0/cache")
    for idx in sorted(d.glob("index*")) if d.exists() else []:
        try:
            if (idx / "level").read_text().strip() == "1" and (idx / "type").read_text().strip() == "Data":
                size = (idx / "size").read_text().strip()
                n = int(size.rstrip("KMk")) * (1024 if size[-1] in "Kk" else 1048576 if size[-1] == "M" else 1)
                ways = int((idx / "ways_of_associativity").read_text())
                line = int((idx / "coherency_line_size").read_text())
                return {"size": n, "ways": ways, "line": line, "stride": n // ways if ways else None, "source": "sysfs"}
        except (OSError, ValueError):
            pass
    if platform.system() == "Darwin":
        # Apple arm64: the performance cores' L1D (hw.perflevel0); macOS does not report the ways. Apple's
        # P-cores (M1-M4) are 128 KB 8-way, so the stride is taken as size/8 there and marked assumed.
        # Intel Macs: hw.l1dcachesize, and machdep.cpu.cache.L1_associativity where the kernel has it.
        n = sysctl("hw.perflevel0.l1dcachesize") or sysctl("hw.l1dcachesize")
        line = sysctl("hw.cachelinesize")
        ways = sysctl("machdep.cpu.cache.L1_associativity")
        if n.isdigit():
            n = int(n)
            r = {"size": n, "line": int(line) if line.isdigit() else None, "source": "sysctl"}
            if ways.isdigit():
                r.update(ways=int(ways), stride=n // int(ways))
            elif platform.machine() == "arm64":
                r.update(ways=8, stride=n // 8, assumed="ways")
            else:
                r.update(ways=None, stride=None)
            return r
    return {}


def cc_version(cc: str) -> str:
    try:
        return subprocess.run([cc, "--version"], capture_output=True, text=True).stdout.splitlines()[0].strip()
    except (OSError, IndexError):
        return cc


def run(d: Path, cases: Path) -> dict:
    env = dict(os.environ, AMBER_THREADS="1", NO_COLOR="1", AMBER_DIAG="0", AMBER_NO_EDIT="1")
    out = subprocess.run([str(d / "amber"), str(cases)], cwd=d, env=env, stdin=subprocess.DEVNULL,
                         capture_output=True, text=True, timeout=600, check=True).stdout
    res = {}
    for l in out.splitlines():
        f = l.split()
        if len(f) == 3 and f[0] in "12" and f[2].isdigit():
            res[f"{f[1]}/{f[0]}"] = float(f[2])
    if not res:
        raise SystemExit(f"no case lines from {d}:\n{out[:2000]}")
    return res


def q(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, round(p * (len(xs) - 1))))]


def boot(rs: list[float], rnd: random.Random, n: int = 2000) -> tuple[float, float]:
    ms = sorted(st.median(rnd.choices(rs, k=len(rs))) for _ in range(n))
    return ms[int(0.025 * n)], ms[int(0.975 * n) - 1]


def analyse(runs: list[dict], seed: int) -> dict:
    rounds = sorted({x["round"] for x in runs})
    by = {(x["round"], x["arm"]): x["times"] for x in runs}
    cases = list(by[(rounds[0], "base")])
    rnd = random.Random(seed)
    out = {}
    for c in cases:
        b = [by[(r, "base")][c] for r in rounds]
        k = [by[(r, "colour")][c] for r in rounds]
        rs = [y / x for x, y in zip(b, k)]
        m = st.median(rs)
        lo, hi = boot(rs, rnd)
        out[c] = {"ratio": m, "lo": lo, "hi": hi, "agree": sum((x > 1) == (m > 1) for x in rs), "n": len(rs),
                  "base_us": st.median(b), "colour_us": st.median(k),
                  "base_p90p10": q(b, 0.9) / q(b, 0.1), "colour_p90p10": q(k, 0.9) / q(k, 0.1)}
    return out


def l1_text(c: dict) -> str:
    if not c:
        return "L1D unknown"
    ways = f"{c['ways']}-way" + (" (assumed)" if c.get("assumed") else "") if c.get("ways") else "ways unknown"
    stride = f"set stride {c['stride']} B" if c.get("stride") else "stride unknown"
    return f"L1D {c['size'] // 1024} KB {ways}, {c.get('line')} B lines, {stride}"


def markdown(r: dict) -> str:
    L = [f"## Colouring, palette period {r['period']} B, {r.get('label', '')}: {r['machine']}, {r['cpu']} "
         f"({l1_text(r['l1d'])}), `{r['cc']}` ({r['rounds']} rounds, the two arms in random order per round)", "",
         "| case | pass | base ms | colour ms | colour/base (95% interval) | rounds agreeing | base p90/p10 | colour p90/p10 |",
         "|---|---|---:|---:|---|---:|---:|---:|"]
    for name, a in sorted(r["analysis"].items(), key=lambda kv: (kv[0].split("/")[1], kv[0])):
        n, p = name.split("/")
        L.append(f"| {n} | {p} | {a['base_us'] / 1000:.2f} | {a['colour_us'] / 1000:.2f} | {a['ratio']:.3f} "
                 f"({a['lo']:.3f} to {a['hi']:.3f}) | {a['agree']}/{a['n']} | {a['base_p90p10']:.2f} | {a['colour_p90p10']:.2f} |")
    L += ["", "(Times are the median over rounds of the case's k runs. Pass 2 runs after pass 1 in the same process. "
          "A p90/p10 well above 1.15 is a case switching between two speeds from one process to the next.)"]
    return "\n".join(L) + "\n"


def summary(files: list[Path]) -> str:
    """One table over several jobs: a column per job (its CPU, L1D and period), a row per case and pass, each cell
    the job's median colour/base ratio and its 95% interval. Jobs ran on different machines: read down a column,
    never across a row."""
    rs = []
    for f in files:
        try:
            rs.append(json.loads(Path(f).read_text()))
        except (OSError, ValueError) as e:
            print(f"skipped {f}: {e}", file=sys.stderr)
    if not rs:
        return "## Colouring: no results\n"
    rs.sort(key=lambda r: (r.get("label", ""), int(r["period"]) if str(r["period"]).isdigit() else 0))
    cases = sorted({c for r in rs for c in r["analysis"]}, key=lambda c: (c.split("/")[1], c))
    L = ["## Colouring across runners: colour/base per case (median over rounds, 95% interval)", "",
         "| job | runner | CPU | L1D | period | rounds |", "|---|---|---|---|---:|---:|"]
    for i, r in enumerate(rs, 1):
        L.append(f"| J{i} | {r.get('label', '')} | {r['cpu']} | {l1_text(r['l1d'])} | {r['period']} | {r['rounds']} |")
    L += ["", "| case | pass | " + " | ".join(f"J{i}" for i in range(1, len(rs) + 1)) + " |",
          "|---|---|" + "---|" * len(rs)]
    for c in cases:
        n, p = c.split("/")
        cells = []
        for r in rs:
            a = r["analysis"].get(c)
            cells.append(f"{a['ratio']:.3f} ({a['lo']:.3f}-{a['hi']:.3f})" if a else "")
        L.append(f"| {n} | {p} | " + " | ".join(cells) + " |")
    L += ["", "(Each job compares main and the colour branch on one machine, paired in rounds. Hosted runners differ "
          "in CPU model from job to job, so compare within a column only.)"]
    return "\n".join(L) + "\n"


def main() -> int:
    a = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    a.add_argument("--summary", nargs="+", type=Path, help="write one table over these jobs' JSON files")
    a.add_argument("--base", type=Path)
    a.add_argument("--colour", type=Path)
    a.add_argument("--cases", type=Path)
    a.add_argument("--period", default="?")
    a.add_argument("--label", default=platform.node())
    a.add_argument("--cc", default=os.environ.get("CC", "cc"))
    a.add_argument("--rounds", type=int, default=15)
    a.add_argument("--seed", type=int, default=0)
    a.add_argument("--json")
    a = a.parse_args()
    if a.summary:
        md = summary(a.summary)
    else:
        if not (a.base and a.colour and a.cases):
            a.error("--base, --colour and --cases are needed (or --summary)")
        arms = {"base": a.base.resolve(), "colour": a.colour.resolve()}
        cases = a.cases.resolve()
        for d in arms.values():
            run(d, cases)
        rnd = random.Random(a.seed)
        runs = []
        for rd in range(a.rounds):
            order = list(arms)
            rnd.shuffle(order)
            for name in order:
                runs.append({"round": rd, "arm": name, "times": run(arms[name], cases)})
        r = {"label": a.label, "machine": platform.machine(), "system": platform.system(), "cpu": cpu_name(),
             "l1d": l1d(), "cc": cc_version(a.cc), "period": a.period, "rounds": a.rounds,
             "analysis": analyse(runs, a.seed), "runs": runs}
        md = markdown(r)
        if a.json:
            Path(a.json).write_text(json.dumps(r, indent=1))
    print(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
