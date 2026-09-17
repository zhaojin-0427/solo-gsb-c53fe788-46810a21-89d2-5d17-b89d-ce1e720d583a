"""暴力交叉验证：python -m tests.test_crossval"""
import random
import sys
from itertools import combinations

sys.path.insert(0, ".")
from app.solver import (  # noqa: E402
    build_model, _subset_infeasible, minimum_unsat_core,
    spfa_feasible, _adjacency, _floyd, INF,
)


def brute_core(edges):
    items = sorted({e[3] for e in edges})
    by = {}
    for u, v, w, s in edges:
        by.setdefault(s, []).append((u, v, w))
    for k in range(1, len(items) + 1):
        for c in combinations(items, k):
            if _subset_infeasible(by, c):
                return list(c)
    return None


def run(seed=99, trials=800):
    random.seed(seed)
    feasible = unsat = 0
    for t in range(trials):
        N = random.randint(2, 6)
        events = []
        ref = 1
        for _ in range(N):
            lb = random.choice([None, random.randint(-3, 5)])
            ub = random.choice([None, random.randint(-3, 8)])
            events.append({"ref": ref, "name": "e",
                           "lower_bound": lb, "upper_bound": ub})
            ref += 1
        rels = []
        for _ in range(random.randint(0, 7)):
            a, b = random.sample(range(1, N + 1), 2)
            kind = random.choice(["earlier", "later",
                                  "after_at_least", "after_at_most"])
            r = {"ref": ref, "a_ref": a, "b_ref": b, "kind": kind}
            if kind in ("after_at_least", "after_at_most"):
                r["n"] = random.randint(0, 5)
            rels.append(r)
            ref += 1
        ni, edges = build_model(events, rels)
        n = len(ni)
        floyd_mat = _floyd(n, edges)
        if spfa_feasible(n, _adjacency(edges)):
            feasible += 1
            assert floyd_mat is not None, f"trial {t}: SPFA/Floyd 可行性不一致"
            # 界不得出现 INF 污染：任意有限界必须是可实现的真实值
            for ref2, idx in ni.items():
                if ref2 == 0:
                    continue
                d0i, di0 = floyd_mat[0][idx], floyd_mat[idx][0]
                if d0i < INF:
                    assert abs(d0i) < 10**9, (ref2, d0i)
                if di0 < INF:
                    assert abs(di0) < 10**9, (ref2, di0)
        else:
            unsat += 1
            assert floyd_mat is None
            got = minimum_unsat_core(n, edges, )
            exp = brute_core(edges)
            assert got == exp, f"trial {t}: got {got} expected {exp}"
    print(f"{trials} 随机实例（可行 {feasible} / 矛盾 {unsat}）交叉验证通过")


if __name__ == "__main__":
    run()
