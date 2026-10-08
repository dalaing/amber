#!/usr/bin/env python3
"""pincheck.py ORDER BIN... : check that the pinned builds put every ordered function before the allocator
block at the same address. The allocator block is the ordered names from _an on (pin.order puts src/m.c's
allocator functions last, as they differ between the builds). nm each binary; names are compared without a
leading _ and without an LTO suffix (.llvm.N). Prints a Markdown report (and appends it to
$GITHUB_STEP_SUMMARY); exits 1 when more than --allow ordered functions differ in address."""
import os, re, subprocess, sys, argparse


def syms(b):
    out = subprocess.run(["nm", "-n", b], capture_output=True, text=True, check=True).stdout
    d = {}
    for l in out.splitlines():
        f = l.split()
        if len(f) == 3 and f[1] in "tT":
            n = re.sub(r"\.llvm\.\d+$", "", f[2].lstrip("_") if sys.platform == "darwin" else f[2])
            d.setdefault(n, []).append(int(f[0], 16))
    return d


def main():
    a = argparse.ArgumentParser()
    a.add_argument("order"); a.add_argument("bins", nargs="+"); a.add_argument("--allow", type=int, default=3)
    a = a.parse_args()
    names = [n.strip().lstrip("_") for n in open(a.order) if n.strip()]
    hot = names[:names.index("an")] if "an" in names else names
    S = [syms(b) for b in a.bins]
    same, diff, missing, dup = [], [], [], []
    # Compare offsets from the first ordered function, so a uniform shift of the whole ordered block (lld puts a
    # few unordered sections first, of a size that differs between the builds) does not count: every function
    # then keeps its offset mod any power of two up to the shift's alignment.
    first = next((n for n in hot if all(n in s for s in S)), None)
    org = [s[first][0] for s in S] if first else [0] * len(S)
    S = [{n: [x - o for x in v] for n, v in s.items()} for s, o in zip(S, org)]
    for n in hot:
        addrs = [s.get(n) for s in S]
        if any(x is None for x in addrs):
            missing.append(n); continue
        if any(len(x) > 1 for x in addrs):
            dup.append(n)
        (same if len({x[0] for x in addrs}) == 1 else diff).append((n, [x[0] for x in addrs]))
    L = [f"### Pinned layout: {len(same)} of {len(hot)} ordered functions at one offset from `{first}` in all {len(S)} builds (block starts: " + ", ".join(hex(o) for o in org) + ")", ""]
    if diff:
        L += ["| function | " + " | ".join(os.path.basename(os.path.dirname(os.path.abspath(b))) or b for b in a.bins) + " |",
              "|---|" + "---|" * len(S)]
        L += [f"| {n} | " + " | ".join(hex(x) for x in xs) + " |" for n, xs in diff]
    if missing:
        L += ["", "Not in every build (inlined, or renamed): " + ", ".join(missing)]
    if dup:
        L += ["", "More than one symbol of the name (the first is compared): " + ", ".join(dup)]
    md = "\n".join(L) + "\n"
    print(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(md)
    return 1 if len(diff) > a.allow else 0


if __name__ == "__main__":
    sys.exit(main())
