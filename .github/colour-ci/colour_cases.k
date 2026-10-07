/ colour_cases.k - the cases the colouring is about, for colour_sweep.py. Run from the repo root:
/   AMBER_THREADS=1 ./amber colour_cases.k </dev/null
/ Prints one line per case: "pass name us" (microseconds for k runs). Every case runs twice in one
/ process: pass 1 on a young heap, pass 2 after everything before it, because the slow placement shows
/ after earlier cases have left the heap in some state, and seldom in a fresh process.
\l amber.k
\l std.k
rep:{[k;f]t:`t[];k{[f;x]f[];x}[f]/0;`t[]-t}
out:{[p;nm;k;f]`0:($p)," ",nm," ",$rep[k;f]}
N:1000000;H:100000!2654435761*!N;PX:0.001*H
dat:0.0+1000!1048573!7919*!N
ix:1000003!6364136223846793005*!N;sy:`$"s",'$100!ix
jsi:"[",(","/$!100000),"]";lsi:" "/$!100000
run:{[p]
 / sliding sums: the cases the aliasing slows down
 out[p;"s_msum100";10;{msum[100;PX]}];out[p;"s_mavg100";10;{mavg[100;PX]}];out[p;"s_mdev100";10;{mdev[100;PX]}]
 out[p;"s_mavg10";10;{mavg[10;PX]}];out[p;"s_mavg1000";10;{mavg[1000;PX]}]
 out[p;"std_msum20";10;{msum[20;dat]}];out[p;"std_mavg20";10;{mavg[20;dat]}];out[p;"std_mdev20";10;{mdev[20;dat]}]
 / grouping
 out[p;"qgroup";5;{group sy}]
 / controls: windows that do not move on M2, and the prior kernels a misaligned colour would slow
 out[p;"s_mmin100";10;{mmin[100;PX]}];out[p;"std_mmin20";10;{mmin[20;dat]}]
 out[p;"maxprior";10;{|':PX}];out[p;"minprior";10;{&':PX}]
 / allocation-bound controls: small vectors (an's class test), and vectors the parsers grow an item at a time (aa)
 out[p;"small_alloc";20;{+/{+/x+!8}'!20000}];out[p;"jsonints";10;{`j?jsi}];out[p;"parseints";10;{. lsi}]}
run 1;run 2
