#!/bin/sh
# pinbuild.sh TREE ORDER: build TREE/amber with clang, every function 64-byte aligned and the functions in ORDER
# (Mach-O names, a leading _) laid out first, in that order: lld's --symbol-ordering-file on Linux, ld64's
# -order_file on macOS. The compile flags are build.sh's own (its F line), plus -falign-functions=64 and
# -ffunction-sections; LTO and OpenMP are probed as build.sh does. Objects go to TREE/o-pin.
set -e
T=$1; ORD=$(cd "$(dirname "$2")" && pwd)/$(basename "$2")
cd "$T"
F=$(sed -n 's/^F="\(-Isrc [^"]*\)".*/\1/p' build.sh)
[ -n "$F" ] || { echo "pinbuild: no F= line in $T/build.sh"; exit 1; }
CC=${PIN_CC:-clang}
F="$F -falign-functions=64 -ffunction-sections"
if [ "$(uname)" = Darwin ]; then LD="-Wl,-order_file,$ORD"
else sed 's/^_//' "$ORD" > .pin.order; LD="-fuse-ld=lld -Wl,--symbol-ordering-file=$PWD/.pin.order -Wl,--no-warn-symbol-ordering"; fi
LTO=""; printf 'int main(){return 0;}' | $CC -flto $LD -x c - -o .ltocheck 2>/dev/null && LTO="-flto"; rm -f .ltocheck
OMP=""; printf '#include <omp.h>\nint main(){return omp_get_max_threads();}' | $CC -fopenmp -x c - -o .ompcheck 2>/dev/null && OMP="-fopenmp"; rm -f .ompcheck
echo "pinbuild: $T with $($CC --version | head -1); flags $F $LTO $OMP; link $LD"
rm -rf o-pin; mkdir -p o-pin
for f in src/*.c; do $CC $F $LTO $OMP -c "$f" -o "o-pin/$(basename "${f%.c}").o"; done
$CC $F $LTO $OMP $LD -o amber o-pin/*.o -lm -ldl 2>/dev/null || $CC $F $LTO $OMP $LD -o amber o-pin/*.o -lm
printf '%s\n' '+/!10' | ./amber
