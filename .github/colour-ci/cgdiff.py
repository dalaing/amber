#!/usr/bin/env python3
"""Instructions per case and per function, main against colour builds, under cachegrind (exact counts, so
the runner's noise does not reach them):

    python3 cgdiff.py --arm base=DIR --arm col=DIR [--arm NAME=DIR[:VAR=VAL]...] --cases colour_cases.k
                      --only jsonints,small_alloc [--label L] [--json OUT]

colour_cases.k's lines before `run:` set up the data; each case named in --only is taken from its
out[p;"NAME";K;{BODY}] call. For every arm and case, one process runs the set-up and then BODY K times, and
another runs the set-up alone; their difference, function by function, is the case's cost (cachegrind's Ir:
instructions executed). NAME=DIR:VAR=VAL runs that arm with VAR=VAL in its environment. Prints, per case,
each arm's instructions per run of BODY against the first arm's, and the functions whose count changed most."""
from __future__ import annotations

import argparse, json, os, re, subprocess, sys, tempfile
from collections import defaultdict
from pathlib import Path


def cg(d: Path, script: str, env: dict, out: Path) -> dict:
    """Run d/amber on script under cachegrind: {function: Ir}."""
    with tempfile.NamedTemporaryFile("w", suffix=".k", dir=d, delete=False) as f:
        f.write(script)
    try:
        subprocess.run(["valgrind", "--tool=cachegrind", "--cache-sim=no", f"--cachegrind-out-file={out}",
                        str(d / "amber"), f.name], cwd=d, env=env, stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=1800, check=True)
    finally:
        os.unlink(f.name)
    ir, fn, ev = defaultdict(int), "?", None
    for l in out.read_text().splitlines():
        if l.startswith("events:"):
            ev = l.split()[1:]
        elif l.startswith("fn="):
            fn = l[3:]
        elif l and l[0].isdigit():
            ir[fn] += int(l.split()[1]) if ev and ev[0] == "Ir" else 0
    return ir


def main() -> int:
    a = argparse.ArgumentParser()
    a.add_argument("--arm", action="append", default=[])
    a.add_argument("--cases", type=Path, required=True)
    a.add_argument("--only", required=True)
    a.add_argument("--label", default="")
    a.add_argument("--top", type=int, default=12)
    a.add_argument("--json")
    a = a.parse_args()
    src = a.cases.read_text()
    setup = src[:src.index("\nrun:")] + "\n"
    arms = {}
    for x in a.arm:
        n, rest = x.split("=", 1)
        d, *ev = rest.split(":")
        arms[n] = (Path(d).resolve(), dict(e.split("=", 1) for e in ev))
    base_env = dict(os.environ, AMBER_THREADS="1", NO_COLOR="1", AMBER_DIAG="0", AMBER_NO_EDIT="1")
    res, md = {}, [f"## Instructions per case (cachegrind Ir), {a.label}", ""]
    tmp = Path(tempfile.mkdtemp())
    null = {n: cg(d, setup, dict(base_env, **e), tmp / f"null-{n}") for n, (d, e) in arms.items()}
    for case in a.only.split(","):
        m = re.search(r'out\[p;"' + re.escape(case) + r'";(\d+);\{(.*?)\}\]', src)
        if not m:
            sys.exit(f"no case {case}")
        k, body = int(m[1]), m[2]
        script = setup + f"{k}{{[f;x]f[];x}}[{{{body}}}]/0;\n"
        per = {}
        for n, (d, e) in arms.items():
            ir = cg(d, script, dict(base_env, **e), tmp / f"{case}-{n}")
            per[n] = {f: (ir.get(f, 0) - null[n].get(f, 0)) / k for f in set(ir) | set(null[n])}
        res[case] = per
        b = list(arms)[0]
        tot = {n: sum(v.values()) for n, v in per.items()}
        md += [f"### {case} (`{body}`, {k} runs; instructions per run)", "",
               "| arm | instructions | against " + b + " |", "|---|---:|---:|"]
        md += [f"| {n} | {tot[n]:,.0f} | {tot[n] / tot[b]:.4f} ({tot[n] - tot[b]:+,.0f}) |" for n in arms]
        fs = sorted({f for v in per.values() for f in v},
                    key=lambda f: -max(abs(per[n].get(f, 0) - per[b].get(f, 0)) for n in arms))[:a.top]
        md += ["", "| function | " + " | ".join(arms) + " |", "|---|" + "---:|" * len(arms)]
        md += [f"| {f} | " + " | ".join(f"{per[n].get(f, 0):,.0f}" for n in arms) + " |" for f in fs]
        md += [""]
    out = "\n".join(md) + "\n"
    print(out)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(out)
    if a.json:
        Path(a.json).write_text(json.dumps({"label": a.label, "cases": res}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
