from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from ralfloop_agent.local_arch.evolver import compute_fitness
from ralfloop_agent.local_arch.sandbox import BubblewrapSandbox, TestVector


PROBLEMS = {
    "sorting_search": {
        "test": TestVector("abracadabra a\n", "5\n", "count-char"),
        "baseline": r'''#include <stdio.h>
#include <string.h>
int main(void){char s[4096],c;if(scanf("%4095s %c",s,&c)!=2)return 2;int n=0;for(size_t i=0;i<strlen(s);i++)if(s[i]==c)n++;printf("%d\n",n);return 0;}''',
        "candidate": r'''#include <stdio.h>
int main(void){char s[4096],c;if(scanf("%4095s %c",s,&c)!=2)return 2;int n=0;for(char *p=s;*p;p++)n+=*p==c;printf("%d\n",n);return 0;}''',
    },
    "graph_shortest_path": {
        "test": TestVector("5 6\n0 1 2\n1 2 2\n0 3 10\n2 3 1\n3 4 3\n1 4 20\n0 4\n", "8\n", "shortest"),
        "baseline": r'''#include <stdio.h>
int main(void){int n,m,u[256],v[256],w[256],d[128],s,t;if(scanf("%d%d",&n,&m)!=2)return 2;for(int i=0;i<m;i++)scanf("%d%d%d",&u[i],&v[i],&w[i]);scanf("%d%d",&s,&t);for(int i=0;i<n;i++)d[i]=1000000000;d[s]=0;for(int k=1;k<n;k++)for(int i=0;i<m;i++)if(d[u[i]]+w[i]<d[v[i]])d[v[i]]=d[u[i]]+w[i];printf("%d\n",d[t]);return 0;}''',
        "candidate": r'''#include <stdio.h>
int main(void){int n,m,g[128][128]={0},d[128],seen[128]={0},s,t;if(scanf("%d%d",&n,&m)!=2)return 2;for(int i=0,u,v,w;i<m;i++){scanf("%d%d%d",&u,&v,&w);g[u][v]=w;}scanf("%d%d",&s,&t);for(int i=0;i<n;i++)d[i]=1000000000;d[s]=0;for(int k=0;k<n;k++){int u=-1;for(int i=0;i<n;i++)if(!seen[i]&&(u<0||d[i]<d[u]))u=i;if(u<0)break;seen[u]=1;for(int v=0;v<n;v++)if(g[u][v]&&d[u]+g[u][v]<d[v])d[v]=d[u]+g[u][v];}printf("%d\n",d[t]);return 0;}''',
    },
    "parser_string": {
        "test": TestVector("1,2,3,4,5,6,7,8,9,10\n", "55\n", "csv-sum"),
        "baseline": r'''#include <stdio.h>
#include <stdlib.h>
int main(void){char s[8192],*p,*e;long sum=0;if(!fgets(s,sizeof s,stdin))return 2;p=s;while(*p){long x=strtol(p,&e,10);if(e==p)break;sum+=x;p=*e?e+1:e;}printf("%ld\n",sum);return 0;}''',
        "candidate": r'''#include <stdio.h>
int main(void){int c;long sum=0,x=0;while((c=getchar())!=EOF){if(c>='0'&&c<='9')x=x*10+c-'0';else{sum+=x;x=0;}}printf("%ld\n",sum);return 0;}''',
    },
}


def run() -> dict[str, object]:
    sandbox = BubblewrapSandbox()
    output = {}
    for name, problem in PROBLEMS.items():
        baseline = sandbox.evaluate(problem["baseline"], [problem["test"]], warmups=2, runs=7)
        candidate = sandbox.evaluate(problem["candidate"], [problem["test"]], warmups=2, runs=7)
        baseline_fit = compute_fitness(baseline)
        candidate_fit = compute_fitness(candidate, baseline={"wall_ms": baseline.wall_ms or 1, "rss_mb": baseline.rss_mb or 1, "binary_kb": baseline.binary_kb or 1})
        improvement = None
        if baseline.median_ms and candidate.median_ms:
            improvement = (baseline.median_ms - candidate.median_ms) / baseline.median_ms
        output[name] = {
            "baseline": baseline.as_dict(),
            "candidate": candidate.as_dict(),
            "baseline_fitness": asdict(baseline_fit),
            "candidate_fitness": asdict(candidate_fit),
            "wall_improvement": improvement,
            "promoted": bool(candidate.correct and baseline.correct and improvement is not None and improvement > max(0.01, 2 * (candidate.noise or 0))),
            "fake_population_candidates": 10,
        }
    return {"v": 1, "mode": "controlled_real_bwrap_micro_evolution", "problems": output}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run()
    rendered = json.dumps(result, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
