/ gap_cases.k CASE - one case of the gap sweep (gap_sweep.py), timed in a process of its own. Run from the repo root:
/   AMBER_THREADS=1 AMBER_COLOUR_STEP=s ./amber gap_cases.k CASE </dev/null
/ Each repetition makes the case's input fresh (untimed), then times the kernel, so the input and the kernel's
/ output are consecutive large allocations: with gap.patch's AMBER_COLOUR_STEP=s they sit s lines apart, mod
/ the period. Before the repetitions it allocates a marker block of 777777 bytes, so an AMBER_COLOUR_LOG=1 log
/ shows which large blocks a repetition makes. Prints "CASE us k" (microseconds for the k repetitions).
\l amber.k
\l std.k
c:`$(`argv(::))2
N:1000000;PX:0.001*100000!2654435761*!N
$[c=`deltasi;ix:1000003!6364136223846793005*!N;];
$[c=`qgroup;[sy:`$"s",'$100!1000003!6364136223846793005*!N;I:0+!N];];
cs:`mmin`maxprior`minprior`deltas`deltasi`addone`msum`mavg1000`qgroup
mk:cs!({PX+0.0};{PX+0.0};{PX+0.0};{PX+0.0};{ix+0};{PX+0.0};{PX+0.0};{PX+0.0};{sy I})
fs:cs!({mmin[100;x]};{|':x};{&':x};{-':x};{-':x};{x+1.0};{msum[100;x]};{mavg[1000;x]};{group x})
ks:cs!10 20 20 20 20 20 20 10 5
tm:{[mk;f;i]x:mk[];t:`t[];f x;`t[]-t}
{x;}tm[mk c;fs c]0   / one untimed repetition first
M:777777#0x00
`0:($c)," ",($+/tm[mk c;fs c]'!ks c)," ",$ks c
