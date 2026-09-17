"""求解器单元测试：python -m tests.test_solver"""
import random
import time
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.solver import solve, ConstraintError


def ev(ref, lb=None, ub=None):
    return {"ref": ref, "lower_bound": lb, "upper_bound": ub}


def rel(ref, a, b, kind, n=None):
    r = {"ref": ref, "a_ref": a, "b_ref": b, "kind": kind}
    if n is not None:
        r["n"] = n
    return r


def check(name, got, **want):
    for k, v in want.items():
        assert got.get(k) == v, f"{name}: {k} = {got.get(k)!r}, 期望 {v!r}"
    print(f"  ok  {name}")


def test_basic():
    # 无约束：全无界
    r = solve([ev(1)], [])
    check("无约束", r, feasible=True)
    assert r["bounds"][1] == [None, None]

    # t in [3, 10]
    r = solve([ev(1, 3, 10)], [])
    check("闭区间", r, feasible=True)
    assert r["bounds"][1] == [3, 10], r["bounds"]

    # 只有下界 10 不得误判矛盾（超源边回归）
    r = solve([ev(1, 10)], [])
    check("仅下界", r, feasible=True)
    assert r["bounds"][1] == [10, None], r["bounds"]

    r = solve([ev(1, None, 5)], [])
    check("仅上界", r, feasible=True)
    assert r["bounds"][1] == [None, 5]

    # 空区间单项矛盾
    r = solve([ev(1, 10, 5)], [])
    check("空区间矛盾", r, feasible=False)
    assert r["core"] == [1], r["core"]


def test_relations():
    # A 早于 B，A>=0：下界传播 B>=1
    r = solve([ev(1, 0), ev(2)], [rel(3, 1, 2, "earlier")])
    assert r["feasible"] and r["bounds"][2] == [1, None], r["bounds"]

    # 互相早于 => 负环，核心为两条关系
    r = solve([ev(1), ev(2)],
              [rel(3, 1, 2, "earlier"), rel(4, 2, 1, "earlier")])
    assert r["feasible"] is False and r["core"] == [3, 4], r["core"]

    # after_at_least n=0 也强制间隔 >=1
    r = solve([ev(1, 0), ev(2)], [rel(3, 2, 1, "after_at_least", 0)])
    assert r["feasible"] and r["bounds"][2] == [1, None]

    # after_at_most n=0 单项即矛盾
    r = solve([ev(1), ev(2)], [rel(3, 1, 2, "after_at_most", 0)])
    assert r["feasible"] is False and r["core"] == [3], r["core"]

    # after_at_most: tA-tB in [1,3]
    r = solve([ev(1, 0, 0), ev(2)], [rel(3, 2, 1, "after_at_most", 3)])
    assert r["feasible"] and r["bounds"][2] == [1, 3], r["bounds"]

    # later: A 晚于 B ⇔ B+1<=A
    r = solve([ev(1), ev(2, 0, 0)], [rel(3, 1, 2, "later")])
    assert r["feasible"] and r["bounds"][1] == [1, None], r["bounds"]
    print("  ok  关系语义")


def test_core_tiebreak():
    # 三个并列二元矛盾环 {3,4} 与 {3,5}、{4,5}？构造:
    # earlier(1,2) ref3; earlier(2,1) ref4  -> {3,4}
    # 再加 earlier(2,1) 的另一条不会并列。改造成两个独立环：
    # 环一：3,4；环二：5,6（事件 3,4 间）
    r = solve(
        [ev(1), ev(2), ev(3, 4), ev(4)],
        [rel(3, 1, 2, "earlier"), rel(4, 2, 1, "earlier"),
         rel(5, 3, 4, "earlier"), rel(6, 4, 3, "earlier")],
    )
    assert r["feasible"] is False
    assert r["core"] == [3, 4], r["core"]  # 字典序最小

    # 单项矛盾与双项矛盾并存 => 取单项
    r = solve([ev(1, 5, 1), ev(2)],
              [rel(3, 1, 2, "earlier"), rel(4, 2, 1, "earlier")])
    assert r["feasible"] is False and r["core"] == [1], r["core"]
    print("  ok  最少项数与字典序并列选择")


def test_perf():
    random.seed(42)
    events = [ev(i + 1, 0, 100000) for i in range(100)]
    # 生成 500 条可满足关系：按事件“真实时刻”排序只允许顺向间隔
    true_t = {i + 1: random.randint(0, 90000) for i in range(100)}
    rels = []
    seq = 101
    while len(rels) < 500:
        a, b = random.sample(range(1, 101), 2)
        if true_t[a] == true_t[b]:
            continue  # 所有关系都要求严格先后，跳过同时刻抽样
        early, late = (a, b) if true_t[a] < true_t[b] else (b, a)
        gap = true_t[late] - true_t[early]
        kind = random.choice(["after_at_least", "after_at_most", "earlier"])
        if kind == "earlier":
            rels.append(rel(seq, early, late, "earlier"))
        else:
            # late 晚于 early 至多 gap 分钟 ⇒ 可行；至少用随机较小量
            n = gap if kind == "after_at_most" else random.randint(0, gap)
            rels.append(rel(seq, late, early, kind, n))
        seq += 1
    t0 = time.perf_counter()
    r = solve(events, rels)
    dt = time.perf_counter() - t0
    assert r["feasible"], "随机可满足实例被判矛盾"
    print(f"  ok  100事件/500关系边界求解耗时 {dt*1000:.0f} ms (<=2000ms)")
    assert dt <= 2.0, f"超时 {dt:.2f}s"

    # 边界抽查：下界不大于上界，且均在 [0, 100000] 内
    for ref, (lo, hi) in r["bounds"].items():
        assert lo is not None and hi is not None
        assert 0 <= lo <= hi <= 100000

    # 矛盾实例性能：植入一个负环
    rels.append(rel(seq, 1, 2, "earlier"))
    rels.append(rel(seq + 1, 2, 1, "earlier"))
    t0 = time.perf_counter()
    r = solve(events, rels)
    dt = time.perf_counter() - t0
    assert r["feasible"] is False
    print(f"  ok  同规模矛盾求解耗时 {dt*1000:.0f} ms, 核心 {r['core']}")
    assert dt <= 2.0


if __name__ == "__main__":
    print("求解器测试")
    test_basic()
    test_relations()
    test_core_tiebreak()
    test_perf()
    print("全部通过")
