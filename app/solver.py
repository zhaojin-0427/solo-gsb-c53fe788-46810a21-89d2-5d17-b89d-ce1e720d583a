"""时序约束求解引擎。

模型：差分约束系统，边 (u, v, w) 表示 t_u - t_v <= w。

约束来源（节点 0 为锚点，t0 = 0）：
- 事件时间范围（每个事件一个输入项）：下界 L => t0 - tE <= -L；上界 U => tE - t0 <= U
- 关系 earlier:  A 早于 B，tA + 1 <= tB  => tA - tB <= -1
- 关系 later:    A 晚于 B，tB + 1 <= tA  => tB - tA <= -1
- 间隔 after_at_least (A 晚于 B 至少 n 分钟): tA - tB >= max(1, n)
                => tB - tA <= -max(1, n)
- 间隔 after_at_most  (A 晚于 B 至多 n 分钟): 1 <= tA - tB <= n
                => tA - tB <= n 且 tB - tA <= -1（n=0 时两式成负环，单项即矛盾）

可行性 ⇔ 约束图无负环（Floyd-Warshall）。
可行时：事件 E 的最紧下界 = -dist[0][E]，最紧上界 = dist[E][0]（None 表示无界）。
不可行时：返回项数最少的矛盾集（最小基数负环上的输入项），
并列时取创建序号字典序最小者。
"""

from collections import deque
from itertools import combinations

INF = 10**15

REL_KINDS = ("earlier", "later", "after_at_least", "after_at_most")
REL_DIRECTIONAL_KINDS = ("after_at_least", "after_at_most")


class ConstraintError(ValueError):
    """输入数据本身不合法（区别于“合法但不可满足”的矛盾）。"""


def _int_or_none(value, field):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConstraintError(f"{field} 必须是整数")
    return value


def normalize_event(ev):
    lb = _int_or_none(ev.get("lower_bound"), "时间下界")
    ub = _int_or_none(ev.get("upper_bound"), "时间上界")
    return lb, ub


def normalize_relation(rel):
    kind = rel.get("kind")
    if kind not in REL_KINDS:
        raise ConstraintError(f"未知关系类型: {kind!r}")
    n = None
    if kind in REL_DIRECTIONAL_KINDS:
        n = rel.get("n")
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise ConstraintError("间隔 n 必须是非负整数")
    return kind, n


def build_model(events, relations):
    """构造节点表与边表。

    节点 0 为锚点（t0=0）；事件按 ref 升序排列为节点 1..k。
    边为 (u, v, w, item_seq) 列表，含义 t_u - t_v <= w。
    """
    node_index = {0: 0}
    for ev in sorted(events, key=lambda e: e["ref"]):
        node_index[ev["ref"]] = len(node_index)

    edges = []

    # 时间范围：每个事件一个输入项（seq=事件 ref）
    for ev in events:
        i = node_index[ev["ref"]]
        lb, ub = normalize_event(ev)
        seq = ev["ref"]
        if lb is not None:
            edges.append((0, i, -lb, seq))  # t0 - tE <= -lb
        if ub is not None:
            edges.append((i, 0, ub, seq))   # tE - t0 <= ub
        if lb is not None and ub is not None and lb > ub:
            # 两边经 0-i-0 组成权 ub-lb<0 的负环，无需额外加边。
            pass

    # 关系
    for rel in relations:
        kind, n = normalize_relation(rel)
        a = rel.get("a_ref")
        b = rel.get("b_ref")
        if a not in node_index or b not in node_index:
            raise ConstraintError("关系引用了不存在的事件")
        u, v = node_index[a], node_index[b]
        seq = rel["ref"]
        if kind == "earlier":           # A 早于 B: tA - tB <= -1
            edges.append((u, v, -1, seq))
        elif kind == "later":           # A 晚于 B: tB - tA <= -1
            edges.append((v, u, -1, seq))
        elif kind == "after_at_least":  # tB - tA <= -max(1,n)
            edges.append((v, u, -max(1, n), seq))
        else:                           # after_at_most: 1 <= tA-tB <= n
            edges.append((u, v, n, seq))    # tA - tB <= n
            edges.append((v, u, -1, seq))   # tB - tA <= -1

    return node_index, edges


def _floyd(n, edges):
    """全源最短距离矩阵；存在负环时返回 None。"""
    dist = [[INF] * n for _ in range(n)]
    for i in range(n):
        dist[i][i] = 0
    for u, v, w, _seq in edges:
        if w < dist[u][v]:
            dist[u][v] = w

    for k in range(n):
        dk = dist[k]
        for i in range(n):
            dik = dist[i][k]
            if dik >= INF:
                continue
            di = dist[i]
            for j in range(n):
                if dk[j] >= INF:
                    continue  # 防止有限负值与 INF 相加造成“无穷远污染”
                nd = dik + dk[j]
                if nd < di[j]:
                    di[j] = nd
        for i in range(n):
            if dist[i][i] < 0:
                return None
    return dist


def _adjacency(model_edges, seqs=None):
    """边表整理为 {u: [(v, w), ...]} 邻接表；可按输入项集合过滤。"""
    adj = {}
    for u, v, w, s in model_edges:
        if seqs is not None and s not in seqs:
            continue
        adj.setdefault(u, []).append((v, w))
    return adj


def _subset_infeasible(edges_by_item, combo):
    """快速判定一个小输入项子集是否构成负环：

    只装配该子集的边和触达的节点，SPFA 初始队列为实际源点，
    避免对 100+ 节点做全量初始化。
    """
    adj = {}
    touched = set()
    for s in combo:
        for u, v, w in edges_by_item.get(s, ()):
            adj.setdefault(u, []).append((v, w))
            touched.add(u)
            touched.add(v)
    dist = {node: 0 for node in touched}
    plen = {}  # 最短路径树上的路径边数（而非松弛次数，避免并行入边假阳性）
    queue = deque(touched)
    in_queue = set(touched)
    limit = len(touched)
    while queue:
        u = queue.popleft()
        in_queue.discard(u)
        du = dist[u]
        for v, w in adj.get(u, ()):
            nd = du + w
            if nd < dist[v]:
                dist[v] = nd
                plen[v] = plen.get(u, 0) + 1
                if plen[v] >= limit:
                    return True
                if v not in in_queue:
                    in_queue.add(v)
                    queue.append(v)
    return False


def spfa_feasible(n, adj):
    """SPFA 判定可行性（全 0 初值等价虚拟超源，路径边数检测负环）。"""
    dist = [0] * n
    plen = [0] * n
    in_queue = [True] * n
    queue = deque(range(n))
    while queue:
        u = queue.popleft()
        in_queue[u] = False
        du = dist[u]
        for v, w in adj.get(u, ()):
            nd = du + w
            if nd < dist[v]:
                dist[v] = nd
                plen[v] = plen[u] + 1
                if plen[v] >= n:
                    return False
                if not in_queue[v]:
                    in_queue[v] = True
                    queue.append(v)
    return True


def _extract_cycle(n, edges):
    """不可行时用 n 轮 Bellman-Ford 前驱链抽取一个负环，返回环上输入项集合。"""
    dist = [0] * n
    prev = [None] * n
    last = -1
    for _ in range(n):
        last = -1
        for idx, (u, v, w, _s) in enumerate(edges):
            nd = dist[u] + w
            if nd < dist[v]:
                dist[v] = nd
                prev[v] = (u, idx)
                last = v
    if last < 0:
        return set()

    cur = last
    for _ in range(n):
        p = prev[cur]
        if p is None:
            break
        cur = p[0]

    seqs = set()
    start = cur
    while True:
        p = prev[cur]
        if p is None:
            break
        pu, eidx = p
        seqs.add(edges[eidx][3])
        cur = pu
        if cur == start:
            break
    return seqs


def _fallback_core(n, model_edges, candidates):
    """删除过滤：从初始负环项集逐项尝试删除，得到包含极小矛盾集。"""
    full_adj = _adjacency(model_edges)
    core = set(_extract_cycle(n, model_edges)) or set(candidates)
    for seq in sorted(core):
        trial = core - {seq}
        if trial and not spfa_feasible(n, _adjacency(model_edges, trial)):
            core = trial
    return sorted(core)


def minimum_unsat_core(n, model_edges, time_budget=2.5):
    """求项数最少的矛盾集。

    该问题在一般差分约束图上为 NP-hard，故采用“按基数枚举 + SPFA 快判”，
    在给定时间预算内保证精确：

    1) Floyd 求“出现在某个负环上”的输入项 involved，其余不可能属于任何核；
    2) 抽一个负环得到初始核，其大小 m 为基数上界；
    3) 按 k=1..m 枚举 involved 的组合（组合按序号升序，即字典序），
       第一个不可行子集即项数最少且字典序最小的核；
    4) 超出时间预算（病态 NP-hard 实例）时退化为删除过滤的包含极小核，
       保证始终有界返回。
    """
    import time
    deadline = time.perf_counter() + time_budget
    mat = [[INF] * n for _ in range(n)]
    for i in range(n):
        mat[i][i] = 0
    for u, v, w, _seq in model_edges:
        if w < mat[u][v]:
            mat[u][v] = w

    for k in range(n):
        dk = mat[k]
        for i in range(n):
            dik = mat[i][k]
            if dik >= INF:
                continue
            di = mat[i]
            for j in range(n):
                if dk[j] >= INF:
                    continue
                nd = dik + dk[j]
                if nd < di[j]:
                    di[j] = nd

    involved = set()
    for u, v, w, seq in model_edges:
        if mat[v][u] + w < 0:  # 边 (u,v,w) 在负环上
            involved.add(seq)

    if not involved:
        return set()

    candidates = sorted(involved)
    initial = set(_extract_cycle(n, model_edges)) or set(candidates)
    initial = initial & involved or set(candidates)
    upper = len(initial)

    edges_by_item = {}
    for u, v, w, s in model_edges:
        edges_by_item.setdefault(s, []).append((u, v, w))

    for k in range(1, upper + 1):
        if k > len(candidates) or time.perf_counter() >= deadline:
            return _fallback_core(n, model_edges, candidates)
        for combo in combinations(candidates, k):
            if _subset_infeasible(edges_by_item, combo):
                return list(combo)
    return sorted(initial)


def solve(events, relations, feasibility_only=False):
    """计算全体可行解的最紧上下界，或最小矛盾集。

    返回:
      {feasible: True,  bounds: {ref: [lower, upper]}}
      {feasible: False, core: [seq, ...]}
    """
    node_index, edges = build_model(events, relations)
    n = len(node_index)

    dist = _floyd(n, edges)
    if dist is not None:
        if feasibility_only:
            return {"feasible": True}
        bounds = {}
        for ref, idx in node_index.items():
            if ref == 0:
                continue
            # t0 - tE <= dist[0][i] => 下界 -dist[0][i]
            # tE - t0 <= dist[i][0] => 上界  dist[i][0]
            bounds[ref] = [
                -dist[0][idx] if dist[0][idx] < INF else None,
                dist[idx][0] if dist[idx][0] < INF else None,
            ]
        return {"feasible": True, "bounds": bounds}

    if feasibility_only:
        return {"feasible": False}
    core = minimum_unsat_core(n, edges)
    return {"feasible": False, "core": list(core)}
