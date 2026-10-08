#!/usr/bin/env python3
"""The gap sweep: how one machine's kernels respond to the distance, mod 4 KB, between their input and output
buffers (fork-only CI for the colouring allocator):

    python3 gap_sweep.py --base DIR --gap DIR --cases gap_cases.k [--steps 0,1,2,...] [--rounds N] [--seed S]
                         [--label L] [--cc CC] [--json OUT]
    python3 gap_sweep.py --summary JSON...      (every job's tables, one after another)

--base is main, built as usual. --gap is the colour branch at CP=4096 with gap.patch, whose AMBER_COLOUR_STEP=s
gives the n-th large block the colour n*s mod 64 lines, so blocks allocated one after another sit s lines
apart; s=0 is main's data placement with the branch's code. Each case (gap_cases.k) runs in a process of its
own, one thread, and makes its input fresh before each repetition, so input and output are consecutive large
allocations (the log below shows whether they are).

1. The map: each case runs once at s=1 with AMBER_COLOUR_LOG=1; the large blocks one repetition makes are
   listed (j, bytes, payload mod 4096), j counted from the repetition's first block (its input). At step s,
   block j sits j*s lines (mod 64) after the input.
2. The rounds: every round runs every case at every step and on the base, in a fresh random order (one
   discarded run of each first). Per case: each step against s=0 in the same round, and s=0 against the base
   (the control: if s=0 matches the base, the branch's code layout is not what moves the case), each as a
   median over rounds with a bootstrap 95% interval and how many rounds agree in sign.

Writes Markdown to stdout (and to $GITHUB_STEP_SUMMARY when set) and, with --json, every run."""
from __future__ import annotations

import argparse
import json
import os
import platform
import random
import re
import statistics as st
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from colour_sweep import cpu_name, l1d, l1_text, cc_version, boot  # noqa: E402

CASES = ["mmin", "maxprior", "minprior", "deltas", "deltasi", "addone", "msum", "mavg1000", "qgroup"]


def run(d: Path, cases: Path, case: str, step: int | None, log: bool = False) -> tuple[float, str]:
    env = dict(os.environ, AMBER_THREADS="1", NO_COLOR="1", AMBER_DIAG="0", AMBER_NO_EDIT="1")
    env.pop("AMBER_COLOUR_STEP", None); env.pop("AMBER_COLOUR_LOG", None)
    if step is not None:
        env["AMBER_COLOUR_STEP"] = str(step)
    if log:
        env["AMBER_COLOUR_LOG"] = "1"
    p = subprocess.run([str(d / "amber"), str(cases), case], cwd=d, env=env, stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=600)
    for l in p.stdout.splitlines():
        f = l.split()
        if len(f) == 3 and f[0] == case and f[1].isdigit():
            return float(f[1]) / float(f[2]), p.stderr
    raise SystemExit(f"{case} on {d} (step {step}): no result line\n{p.stdout[:1500]}\n{p.stderr[:1500]}")


def gapmap(d: Path, cases: Path, case: str) -> dict:
    """The large blocks one repetition makes, from an AMBER_COLOUR_LOG=1 run at s=1."""
    env = dict(os.environ, AMBER_THREADS="1", NO_COLOR="1", AMBER_DIAG="0", AMBER_NO_EDIT="1",
               AMBER_COLOUR_STEP="1", AMBER_COLOUR_LOG="1")
    p = subprocess.run([str(d / "amber"), str(cases), case], cwd=d, env=env, stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=600)
    k = next((int(l.split()[2]) for l in p.stdout.splitlines() if l.startswith(case + " ")), 0)
    blocks, seen = [], False
    for l in p.stderr.splitlines():
        m = dict(re.findall(r"(\w+)=(\d+)", l)) if l.startswith("COL ") else None
        if not m:
            continue
        if int(m["nb"]) == 777777:
            seen = True; continue
        if seen:
            blocks.append({"n": int(m["n"]), "bytes": int(m["nb"]), "colour": int(m["c"]), "mod4k": int(m["p"])})
    per = len(blocks) // k if k else 0
    rep = blocks[:per]
    for b in rep:
        b["j"] = b["n"] - rep[0]["n"]
    return {"reps": k, "blocks": len(blocks), "per_rep": per, "rep": rep}


def analyse(runs: list[dict], steps: list[int], seed: int) -> dict:
    rnd = random.Random(seed)
    rounds = sorted({x["round"] for x in runs})
    t = {(x["round"], x["case"], x["arm"]): x["us"] for x in runs}
    out = {}
    for c in sorted({x["case"] for x in runs}, key=CASES.index):
        e = {"us": {}, "pairs": {}}
        arms = ["base"] + [f"s{s}" for s in steps]
        for a in arms:
            e["us"][a] = st.median(t[(r, c, a)] for r in rounds)
        def pair(a, b):
            rs = [t[(r, c, a)] / t[(r, c, b)] for r in rounds]
            m = st.median(rs)
            lo, hi = boot(rs, rnd)
            return {"ratio": m, "lo": lo, "hi": hi, "agree": sum((x > 1) == (m > 1) for x in rs), "n": len(rs)}
        e["pairs"]["s0/base"] = pair("s0", "base")
        for s in steps[1:]:
            e["pairs"][f"s{s}/s0"] = pair(f"s{s}", "s0")
        out[c] = e
    return out


def cell(p: dict) -> str:
    flag = "**" if p["lo"] > 1.02 or p["hi"] < 0.98 else ""
    return f"{flag}{p['ratio']:.3f}{flag} ({p['lo']:.2f}-{p['hi']:.2f})"


def markdown(r: dict) -> str:
    steps = r["steps"]
    L = [f"## Gap sweep, {r.get('label', '')}: {r['machine']}, {r['cpu']} ({l1_text(r['l1d'])}), `{r['cc']}` "
         f"({r['rounds']} rounds; CP 4096; each step against s=0 in the same round)", "",
         "Ratios are medians over rounds with a bootstrap 95% interval; bold where the interval is clear of +-2%. "
         "s0/base is the control: the branch's code at main's data placement against main.", "",
         "| case | base ms | s0/base | " + " | ".join(f"s={s}" for s in steps[1:]) + " |",
         "|---|---:|---|" + "---|" * (len(steps) - 1)]
    for c, e in r["analysis"].items():
        L.append(f"| {c} | {e['us']['base'] / 1000:.3f} | {cell(e['pairs']['s0/base'])} | "
                 + " | ".join(cell(e["pairs"][f"s{s}/s0"]) for s in steps[1:]) + " |")
    L += ["", "**The blocks one repetition makes** (from a log at s=1: j counts large allocations from the input; "
          "at step s block j sits j*s mod 64 lines after the input):", ""]
    for c, m in r["map"].items():
        L.append(f"- {c}: {m['per_rep']} a repetition: " + ", ".join(
            f"j{b['j']} {b['bytes']} B @{b['mod4k']}" for b in m["rep"][:8]) + (" ..." if m["per_rep"] > 8 else ""))
    return "\n".join(L) + "\n"


def main() -> int:
    a = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    a.add_argument("--summary", nargs="+", type=Path)
    a.add_argument("--base", type=Path)
    a.add_argument("--gap", type=Path)
    a.add_argument("--cases", type=Path)
    a.add_argument("--only", default=",".join(CASES))
    a.add_argument("--steps", default="0,1,2,3,4,6,8,16,32,48,63")
    a.add_argument("--label", default=platform.node())
    a.add_argument("--cc", default=os.environ.get("CC", "cc"))
    a.add_argument("--rounds", type=int, default=10)
    a.add_argument("--seed", type=int, default=0)
    a.add_argument("--json")
    a = a.parse_args()
    if a.summary:
        md = "\n".join(markdown(json.loads(Path(f).read_text())) for f in sorted(a.summary))
    else:
        if not (a.base and a.gap and a.cases):
            a.error("--base, --gap and --cases are needed (or --summary)")
        base, gap, cases = a.base.resolve(), a.gap.resolve(), a.cases.resolve()
        steps = [int(x) for x in a.steps.split(",")]
        if steps[0] != 0:
            steps = [0] + [s for s in steps if s != 0]
        only = [c for c in a.only.split(",") if c]
        gm = {c: gapmap(gap, cases, c) for c in only}
        arms = [("base", base, None)] + [(f"s{s}", gap, s) for s in steps]
        for c in only:
            for _, d, s in arms:
                run(d, cases, c, s)
        rnd = random.Random(a.seed)
        runs = []
        for rd in range(a.rounds):
            jobs = [(c, arm) for c in only for arm in arms]
            rnd.shuffle(jobs)
            for c, (name, d, s) in jobs:
                runs.append({"round": rd, "case": c, "arm": name, "us": run(d, cases, c, s)[0]})
        r = {"label": a.label, "machine": platform.machine(), "system": platform.system(), "cpu": cpu_name(),
             "l1d": l1d(), "cc": cc_version(a.cc), "steps": steps, "rounds": a.rounds, "map": gm,
             "analysis": analyse(runs, steps, a.seed), "runs": runs}
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
