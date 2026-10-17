#!/usr/bin/env python3
"""x86time: paired, interleaved timings of amber refs against a base, on one CI host.

    python3 x86time.py run refs.json OUT [--host LABEL] [--local NAME=DIR ...]
    python3 x86time.py ana OUT            # rewrite OUT/results.md from OUT/raw.jsonl

refs.json:
    {"base": {"repo": "upstream", "commit": "<full sha>"},
     "refs": [{"name": "windows", "repo": "fork", "commit": "<full sha>"}],
     "rows": "rows.k", "rounds": 40, "target_us": 100000, "budget_s": 1500, "seed": 1}

repo is "upstream" (BonucciAndrea/amber), "fork" (dalaing/amber) or a URL. A commit only in a local checkout is
pushed to the fork as a plain branch x86time/ref-<name> first. Sides: base, base2 (a second copy of the base
tree, built separately: the identical-code control) and every ref. Each side is `git archive` of its commit,
built by its own ./build.sh as it is (no CC, no flags). Rows are run.py's case format (a prelude, then
"/@ name" blocks with one out["name";K;{...}] line); K is calibrated on the base to target_us.

A round runs every row once on every side, each run in a process of its own (AMBER_THREADS=1); the rows in a
fresh random order, the sides of each row in an order rotated (and every other cycle reversed) per round, so
each side takes each position equally often. Round 0 is a warm-up and is discarded. Rounds stop at `rounds`
or when `budget_s` has gone. Ratios are medians of the per-round ratios ref/base (base2/base for the control)
with bootstrap 95% intervals (rounds resampled).
"""
import hashlib, json, os, random, re, shutil, statistics, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor

REPOS = {"upstream": "https://github.com/BonucciAndrea/amber.git", "fork": "https://github.com/dalaing/amber.git"}
OUT_RE = re.compile(r'(\bout\["([^"]+)";)K;')
ANS_RE = re.compile(r'^(.*)\bout\["([^"]+)";K;(\{.*\})\]\s*$', re.M)


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, **kw)


def sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def tree_sha(d):
    out = {}
    for root, _, files in os.walk(d):
        for f in files:
            p = os.path.join(root, f)
            if not os.path.islink(p):
                out[os.path.relpath(p, d)] = sha1(p)
    return out


def parse_rows(path):
    src = open(path).read()
    blocks = re.split(r'^(?=/@ )', src, flags=re.M)
    prelude, rows = blocks[0], []
    for b in blocks[1:]:
        name = b.split()[1]
        if not OUT_RE.search(b):
            sys.exit(f"{path}: row {name} has no out[\"{name}\";K;...] line")
        rows.append({"name": name, "src": prelude + b})
    if len({r["name"] for r in rows}) != len(rows):
        sys.exit(f"{path}: a row name repeats")
    return rows


def env1():
    return {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""), "AMBER_DIAG": "0",
            "AMBER_NO_EDIT": "1", "NO_COLOR": "1", "AMBER_THREADS": "1"}


def script(out, row, k):
    p = os.path.join(out, "scripts", f"{row['name']}.k{k}.k")
    if not os.path.exists(p):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write(OUT_RE.sub(lambda m: f"{m.group(1)}{k};" if m.group(2) == row["name"] else m.group(0),
                                      row["src"]))
    return p


def run1(out, d, row, k):
    p = script(out, row, k)
    try:
        r = subprocess.run(["timeout", "300", "./amber", p], cwd=d, env=env1(), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=330)
    except subprocess.TimeoutExpired:
        return -3, "python-side timeout"
    v = None
    for line in r.stdout.splitlines():
        x = line.split()
        if len(x) == 2 and x[0] == row["name"] and re.fullmatch(r"-?\d+", x[1]):
            v = int(x[1])
    if r.returncode or v is None or v < 0:
        return -2, (r.stdout[-300:] + r.stderr[-300:])
    return v, ""


def answer(out, d, row):
    src = ANS_RE.sub(lambda m: (m.group(1) + f's:-8!{m.group(3)}[];`0:"{m.group(2)} "," "/$(#s;+/(1+!#s)*`i$s)')
                     if m.group(2) == row["name"] else m.group(0), row["src"].rstrip("\n"))
    p = os.path.join(out, "answers", row["name"] + ".k")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").write(src + "\n")
    r = sh(["timeout", "300", "./amber", p], cwd=d, env=env1(), stdin=subprocess.DEVNULL)
    lines = [l for l in r.stdout.splitlines() if l.startswith(row["name"] + " ")]
    return (lines[-1] if lines else "?" + r.stdout[-80:] + r.stderr[-80:]) + (f" rc={r.returncode}" if r.returncode else "")


def host_info():
    lscpu = sh("lscpu").stdout
    model = next((l.split(":", 1)[1].strip() for l in lscpu.splitlines() if l.startswith("Model name")), "?")
    return {"cpu": model, "nproc": sh("nproc").stdout.strip(), "uname": sh("uname -a").stdout.strip(),
            "cc": sh("cc --version").stdout.splitlines()[:1], "gcc": sh("gcc --version").stdout.splitlines()[:1],
            "cc_path": sh("readlink -f $(command -v cc)").stdout.strip(), "lscpu": lscpu}


def fetch_and_build(cfg, out):
    sides = [("base", cfg["base"]), ("base2", cfg["base"])] + [(r["name"], r) for r in cfg["refs"]]
    repo = os.path.join(out, "git")
    if not os.path.isdir(repo):
        sh(["git", "init", "-q", repo])
    for _, s in sides:
        url = REPOS.get(s.get("repo", "upstream"), s.get("repo"))
        if sh(["git", "-C", repo, "cat-file", "-e", s["commit"] + "^{commit}"]).returncode:
            r = sh(["git", "-C", repo, "fetch", "-q", "--depth", "1", url, s["commit"]])
            if r.returncode:
                sys.exit(f"fetch {s['commit']} from {url} failed: {r.stderr}")
    dirs = {}
    for name, s in sides:
        d = os.path.join(out, "sides", name)
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
        r = subprocess.run(f"git -C {repo} archive {s['commit']} | tar -x -C {d}", shell=True)
        if r.returncode:
            sys.exit(f"archive {name} failed")
        dirs[name] = d
    if tree_sha(dirs["base"]) != tree_sha(dirs["base2"]):
        sys.exit("base2's tree differs from the base's")

    def build(name):
        e = {k: v for k, v in os.environ.items() if k not in ("CC", "CFLAGS", "AMBER_NATIVE", "AMBER_SHARED")}
        t = time.time()
        r = subprocess.run(["timeout", "1800", "./build.sh"], cwd=dirs[name], env=e, capture_output=True, text=True)
        log = r.stdout + r.stderr
        open(os.path.join(out, f"build-{name}.log"), "w").write(log)
        if r.returncode or not os.path.exists(os.path.join(dirs[name], "amber")):
            sys.exit(f"build {name} failed:\n{log[-2000:]}")
        mode = next((l for l in log.splitlines() if "compiling with" in l), "?")
        return name, {"mode": mode, "sha1": sha1(os.path.join(dirs[name], "amber")), "secs": round(time.time() - t)}

    with ThreadPoolExecutor(max_workers=max(1, min(len(sides), os.cpu_count() or 1))) as ex:
        builds = dict(ex.map(build, [n for n, _ in sides]))
    return dirs, builds


def calibrate(out, d, rows, tgt):
    cal = {}
    lo = min(20000, max(tgt // 4, 200))
    for row in rows:
        k = 1
        while True:
            v, err = run1(out, d, row, k)
            if v < 0:
                sys.exit(f"calibrate {row['name']}: {err}")
            if v >= lo or k >= 10 ** 8:
                cal[row["name"]] = max(1, round(k * tgt / max(v, 1)))
                break
            k = k * 10 if v < lo / 10 else max(k + 1, round(k * 1.25 * lo / max(v, 1)))
        print(f"calib {row['name']} K={cal[row['name']]}", flush=True)
    return cal


def order(sides, rnd):
    n = len(sides)
    o = sides[rnd % n:] + sides[:rnd % n]
    return o[::-1] if (rnd // n) % 2 else o


def run(cfg_path, out, host, local):
    cfg = json.load(open(cfg_path))
    os.makedirs(out, exist_ok=True)
    here = os.path.dirname(os.path.abspath(cfg_path))
    rows = parse_rows(os.path.join(here, cfg.get("rows", "rows.k")))
    meta = {"host": host, "config": cfg, "started": time.strftime("%FT%TZ", time.gmtime()), **host_info()}
    print(f"host {host}: {meta['cpu']}, nproc {meta['nproc']}, cc {meta['cc']} -> {meta['cc_path']}", flush=True)
    t0 = time.time()
    if local:
        dirs = dict(x.split("=", 1) for x in local)
        builds = {n: {"mode": "local", "sha1": sha1(os.path.join(d, "amber")), "secs": 0} for n, d in dirs.items()}
    else:
        dirs, builds = fetch_and_build(cfg, out)
    meta["builds"] = builds
    meta["build_secs"] = round(time.time() - t0)
    for n, b in builds.items():
        print(f"build {n}: {b['mode']} sha1 {b['sha1'][:12]} ({b['secs']} s)", flush=True)
    sides = list(dirs)
    refs = [s for s in sides if s not in ("base", "base2")]
    # answers: every side against the base
    diffs = []
    for row in rows:
        a0 = answer(out, dirs["base"], row)
        for s in sides[1:]:
            a = answer(out, dirs[s], row)
            if a != a0:
                diffs.append({"row": row["name"], "side": s, "base": a0[:120], "side_answer": a[:120]})
    meta["answer_diffs"] = diffs
    for x in diffs:
        print(f"ANSWER DIFFERS: {x}", flush=True)
    if any(x["side"] == "base2" for x in diffs):
        sys.exit("base2's answers differ from the base's")
    cal = calibrate(out, dirs["base"], rows, cfg.get("target_us", 100000))
    meta["K"] = cal
    json.dump(meta, open(os.path.join(out, "meta.json"), "w"), indent=1)
    seed = f"{cfg.get('seed', 1)}/{host}"
    raw = open(os.path.join(out, "raw.jsonl"), "w")
    budget, nr = cfg.get("budget_s", 1500), cfg.get("rounds", 40)
    tr = time.time()
    rnd = 0
    while rnd <= nr:
        if rnd > 1 and time.time() - tr > budget:
            print(f"budget: stopping after round {rnd - 1}", flush=True)
            break
        rng = random.Random(f"{seed}/{rnd}")
        rs = rows[:]
        rng.shuffle(rs)
        t1, recs = time.time(), []
        for row in rs:
            for j, s in enumerate(order(sides, rnd)):
                v, err = run1(out, dirs[s], row, cal[row["name"]])
                r = {"round": rnd, "row": row["name"], "side": s, "us": v, "k": cal[row["name"]], "pos": j}
                if err:
                    r["err"] = err[-200:]
                recs.append(r)
        for r in recs:
            raw.write(json.dumps(r) + "\n")
        raw.flush()
        print(f"round {rnd}{' (warm-up)' if rnd == 0 else ''}: {len(recs)} runs, {time.time() - t1:.0f}s, "
              f"{sum(r['us'] < 0 for r in recs)} errors", flush=True)
        rnd += 1
    raw.close()
    meta["rounds_done"] = rnd - 1
    meta["timing_secs"] = round(time.time() - tr)
    meta["ended"] = time.strftime("%FT%TZ", time.gmtime())
    json.dump(meta, open(os.path.join(out, "meta.json"), "w"), indent=1)
    return ana(out)


def boot(xs, n=4000, seed=7):
    rng = random.Random(seed)
    ms = sorted(statistics.median(rng.choices(xs, k=len(xs))) for _ in range(n))
    return ms[int(0.025 * n)], ms[int(0.975 * n) - 1]


def ana(out):
    meta = json.load(open(os.path.join(out, "meta.json")))
    by = {}
    errs = 0
    for line in open(os.path.join(out, "raw.jsonl")):
        r = json.loads(line)
        if r["round"] == 0:
            continue
        if r["us"] < 0:
            errs += 1
            continue
        by.setdefault(r["row"], {}).setdefault(r["round"], {})[r["side"]] = r["us"] / r["k"]
    refs = [n for n in meta["builds"] if n not in ("base", "base2")]
    rows = list(meta["K"])
    md = [f"# x86time: {meta['host']} ({meta['cpu']})", "",
          f"- cc: {' '.join(meta['cc'])} ({meta['cc_path']}); nproc {meta['nproc']}",
          f"- base {meta['config']['base']['commit'][:12]}; refs: "
          + ", ".join(f"{r['name']} {r['commit'][:12]}" for r in meta["config"]["refs"]),
          "- builds: " + "; ".join(f"{n} `{b['mode']}` sha1 {b['sha1'][:12]}" for n, b in meta["builds"].items()),
          f"- rounds: {meta.get('rounds_done')} (after a warm-up round), K calibrated to "
          f"{meta['config'].get('target_us', 100000) // 1000} ms on the base, AMBER_THREADS=1; timing "
          f"{meta.get('timing_secs', '?')} s, builds {meta.get('build_secs', '?')} s; run errors {errs}",
          "- answer differences: " + ("none" if not meta["answer_diffs"] else
                                      "; ".join(f"{x['row']} on {x['side']}" for x in meta["answer_diffs"])),
          "", "Ratios: medians of per-round ratios with bootstrap 95% intervals; under 1 = faster than the base.", ""]
    floor = 0.0
    for ref in refs:
        md += [f"## {ref} / base", "",
               "| row | base µs/call | rounds | ref/base [95%] | slower/faster rounds | control base2/base [95%] |",
               "|---|---:|---:|---|---:|---|"]
        for row in rows:
            rs = [v for v in by.get(row, {}).values() if all(s in v for s in ("base", "base2", ref))]
            if not rs:
                md.append(f"| {row} | - | 0 | ERROR | - | - |")
                continue
            q = [v[ref] / v["base"] for v in rs]
            c = [v["base2"] / v["base"] for v in rs]
            lo, hi = boot(q)
            clo, chi = boot(c)
            cm = statistics.median(c)
            floor = max(floor, abs(cm - 1))
            md.append(f"| {row} | {statistics.median(v['base'] for v in rs):.1f} | {len(rs)} | "
                      f"**{statistics.median(q):.3f}** [{lo:.3f}, {hi:.3f}] | {sum(x > 1 for x in q)}/{sum(x < 1 for x in q)} | "
                      f"{cm:.3f} [{clo:.3f}, {chi:.3f}] |")
        md.append("")
    md.append(f"Control floor (max |base2/base median - 1|): {floor:.3f}")
    text = "\n".join(md) + "\n"
    open(os.path.join(out, "results.md"), "w").write(text)
    print(text)
    s = os.environ.get("GITHUB_STEP_SUMMARY")
    if s:
        open(s, "a").write(text)
    return 0


def main():
    a = sys.argv[1:]
    if len(a) >= 3 and a[0] == "run":
        host, local, i = "local", [], 3
        while i < len(a):
            if a[i] == "--host":
                host, i = a[i + 1], i + 2
            elif a[i] == "--local":
                local.append(a[i + 1]); i += 2
            else:
                sys.exit(f"unknown option {a[i]}")
        sys.exit(run(a[1], a[2], host, local))
    if len(a) == 2 and a[0] == "ana":
        sys.exit(ana(a[1]))
    sys.exit(__doc__)


if __name__ == "__main__":
    main()
