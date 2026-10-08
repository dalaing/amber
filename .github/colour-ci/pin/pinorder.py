#!/usr/bin/env python3
"""pinorder.py ORDER SAME BASE_BIN OTHER_BIN... : a symbol order that puts every function the builds share
unchanged at one address. Run on unordered builds made with the pinned flags (pinbuild.sh with an empty
order). A function counts as unchanged when every build has exactly one text symbol of its name, of the same
size; those come first, in BASE_BIN's address order, and the rest (the code that changed, and the functions
that inlined it) after them. With every function 64-byte aligned, the unchanged ones then sit at the same
address in every build, and only the changed code moves. Writes ORDER (all of them) and SAME (the unchanged
ones, for pincheck.py) with Mach-O style names (a leading _), as pinbuild.sh expects."""
import re, subprocess, sys


def syms(b):
    """name -> [(address, size)] for the text symbols of b (nm -S: ELF sizes; LTO suffixes dropped)."""
    out = subprocess.run(["nm", "-S", "-n", b], capture_output=True, text=True, check=True).stdout
    d = {}
    for l in out.splitlines():
        f = l.split()
        if len(f) == 4 and f[2] in "tT":
            n = re.sub(r"\.llvm\.\d+$", "", f[3])
            d.setdefault(n, []).append((int(f[0], 16), int(f[1], 16)))
    return d


def main():
    if len(sys.argv) < 5:
        sys.exit(__doc__)
    order, samef, bins = sys.argv[1], sys.argv[2], sys.argv[3:]
    S = [syms(b) for b in bins]
    base = sorted(S[0], key=lambda n: S[0][n][0][0])
    same, changed = [], []
    for n in base:
        xs = [s.get(n) for s in S]
        (same if all(x and len(x) == 1 and x[0][1] == xs[0][0][1] for x in xs) else changed).append(n)
    open(order, "w").write("".join(f"_{n}\n" for n in same + changed))
    open(samef, "w").write("".join(f"_{n}\n" for n in same))
    print(f"pinorder: {len(same)} functions unchanged in all {len(bins)} builds; changed or not in all: "
          + " ".join(changed))


if __name__ == "__main__":
    main()
