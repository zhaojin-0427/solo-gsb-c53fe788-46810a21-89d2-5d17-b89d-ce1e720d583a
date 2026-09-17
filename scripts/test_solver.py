"""求解器快速验证：正确性小例 + 性能压测。

边 u->v 权 w 表示 t_v - t_u <= w：
  t_x >= C : (x, 0, -C)     t_x <= C : (0, x, C)
  A 晚于 B 至少 n (t_A-t_B>=n) : (A, B, -n)
  A 晚于 B 至多 n (1<=t_A-t_B<=n) : (B, A, n), (A, B, -1)
  A 早于 B : (B, A, -1)     同一时刻 : (A,B,0),(B,A,0)
"""
import random
import time
import sys

sys.path.insert(0, "/workspace")
from app.solver import Edge, analyze, min_contradiction


def t_simple_chain():
    # t1>=3, t2>=t1+2, t2<=10  => t1∈[3,8], t2∈[5,10]
    E = [
        Edge(1, 0, -3, "e1", 1),
        Edge(2, 1, -2, "r1", 2),
        Edge(0, 2, 10, "e2", 3),
    ]
    ok, lo, hi = analyze(3, E)
    assert ok
    assert (lo[1], hi[1]) == (3, 8), (lo[1], hi[1])
    assert (lo[2], hi[2]) == (5, 10), (lo[2], hi[2])
    print("simple chain OK", lo, hi)


def t_unbounded():
    # 仅 t1>=3 => [3, +inf)；仅 t2<=7 => (-inf,7]；t3 无约束 => (-inf,+inf)
    E = [Edge(1, 0, -3, "e1", 1), Edge(0, 2, 7, "e2", 2)]
    ok, lo, hi = analyze(4, E)
    assert ok
    assert (lo[1], hi[1]) == (3, None)
    assert (lo[2], hi[2]) == (None, 7)
    assert (lo[3], hi[3]) == (None, None)
    print("unbounded OK")


def t_same_time():
    # t1=t2, t1>=5, t2<=7
    E = [Edge(1, 2, 0, "r", 3), Edge(2, 1, 0, "r", 3),
         Edge(1, 0, -5, "e1", 1), Edge(0, 2, 7, "e2", 2)]
    ok, lo, hi = analyze(3, E)
    assert ok and lo[1] == 5 and hi[1] == 7 and lo[2] == 5 and hi[2] == 7
    print("same time OK")


def t_contradiction_simple():
    # A 晚于 B 且 B 晚于 A => 负环，2 项
    E = [Edge(1, 2, -1, "r1", 1), Edge(2, 1, -1, "r2", 2)]
    ok, *_ = analyze(3, E)
    assert not ok
    assert min_contradiction(3, E, 2) == [1, 2]
    print("2-item contradiction OK")


def t_single_item_contradiction():
    # A 晚于 B 至多 0 分钟 => 1<=tA-tB<=0 => 单项矛盾
    E = [Edge(2, 1, 0, "r1", 5), Edge(1, 2, -1, "r1", 5)]
    assert min_contradiction(3, E, 5) == [5]
    # 负自环（同事件自反关系）
    E2 = [Edge(1, 1, -1, "r9", 9)]
    assert min_contradiction(2, E2, 9) == [9]
    print("single item contradiction OK")


def t_tie_lex():
    # 两个 2 项矛盾环：seqs {1,4} 与 {2,3} => 字典序选 [1,4]
    E = [
        Edge(1, 2, -1, "r1", 1), Edge(2, 1, -1, "r2", 4),
        Edge(3, 4, -1, "r3", 2), Edge(4, 3, -1, "r4", 3),
    ]
    assert min_contradiction(5, E, 4) == [1, 4]
    print("lex tie OK")


def t_smaller_set_wins():
    E = [
        Edge(1, 2, -1, "a", 3), Edge(2, 1, -1, "b", 4),
        Edge(3, 4, -1, "c", 1), Edge(4, 5, -1, "d", 2),
        Edge(5, 3, -1, "e", 5),
    ]
    assert min_contradiction(6, E, 5) == [3, 4]
    print("smaller set wins OK")


def t_range_contradiction():
    E = [Edge(1, 0, -10, "ev", 7), Edge(0, 1, 5, "ev", 7)]
    assert min_contradiction(2, E, 7) == [7]
    print("range contradiction OK")


def t_chain_contradiction_min():
    # 环: 1->2 -1, 2->3 -1, 3->1 -1 (3项) 与一个 4 项环并存
    E = [
        Edge(1, 2, -1, "a", 10), Edge(2, 3, -1, "b", 11),
        Edge(3, 1, -1, "c", 12),
        Edge(3, 4, -1, "d", 13), Edge(4, 5, -1, "e", 14),
        Edge(5, 6, -1, "f", 15), Edge(6, 3, -1, "g", 16),
    ]
    assert min_contradiction(7, E, 16) == [10, 11, 12]
    print("min-length cycle OK")


def t_perf_satisfiable(n_events=100, n_rels=500):
    random.seed(42)
    truth = [0] + [random.randint(0, 200) for _ in range(n_events)]
    edges: list[Edge] = []
    seq = 0
    for i in range(1, n_events + 1):
        seq += 1
        lo = truth[i] - random.randint(0, 50)
        hi = truth[i] + random.randint(0, 50)
        edges.append(Edge(i, 0, -lo, f"ev{i}", seq))
        edges.append(Edge(0, i, hi, f"ev{i}", seq))
    for k in range(n_rels):
        a, b = random.sample(range(1, n_events + 1), 2)
        d = truth[a] - truth[b]
        seq += 1
        kind = random.choice(["earlier", "later", "same", "ge", "le"])
        if kind == "earlier" and d <= -1:
            edges.append(Edge(b, a, -1, f"r{k}", seq))
        elif kind == "later" and d >= 1:
            edges.append(Edge(a, b, -1, f"r{k}", seq))
        elif kind == "same" and d == 0:
            edges.append(Edge(a, b, 0, f"r{k}", seq))
            edges.append(Edge(b, a, 0, f"r{k}", seq))
        elif kind == "ge" and d >= 1:
            n = random.randint(1, d)
            edges.append(Edge(a, b, -n, f"r{k}", seq))
        elif kind == "le" and d >= 1:
            n = random.randint(d, d + 30)
            edges.append(Edge(b, a, n, f"r{k}", seq))   # t_a-t_b<=n
            edges.append(Edge(a, b, -1, f"r{k}", seq))  # t_a-t_b>=1
        else:
            # 与 truth 兼容的早于关系兜底
            if d >= 1:
                edges.append(Edge(a, b, -1, f"r{k}", seq))
            else:
                edges.append(Edge(b, a, -1, f"r{k}", seq))
    t0 = time.perf_counter()
    ok, lo, hi = analyze(n_events + 1, edges)
    t1 = time.perf_counter()
    assert ok, "generated instance must be satisfiable"
    for i in range(1, n_events + 1):
        assert lo[i] <= truth[i] <= hi[i], i
    print(f"perf satisfiable: {t1 - t0:.3f}s  ({len(edges)} edges)")
    return t1 - t0


def t_perf_unsat_ring():
    n = 500
    edges = [Edge(i, i + 1, -1, f"r{i}", i) for i in range(1, n)]
    edges.append(Edge(n, 1, -1, "r0", n))
    t0 = time.perf_counter()
    m = min_contradiction(n + 1, edges, n)
    t1 = time.perf_counter()
    assert len(m) == n
    print(f"perf unsat ring k*=500: {t1 - t0:.3f}s")


if __name__ == "__main__":
    t_simple_chain()
    t_unbounded()
    t_same_time()
    t_contradiction_simple()
    t_single_item_contradiction()
    t_tie_lex()
    t_smaller_set_wins()
    t_range_contradiction()
    t_chain_contradiction_min()
    t_perf_satisfiable()
    t_perf_unsat_ring()
