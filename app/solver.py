"""差分约束求解器。

所有时间约束都可写成  t_v - t_u <= w  的形式（有向边 u -> v，权 w）。

系统可满足  <=>  约束图中不存在负环（等价于从权为 0、指向所有节点的虚拟
超源出发的最短路有界）。

变量 t_x 的可行域为闭区间：
  上界 = 固定原点 t_0=0 到 x 的最短路（不可达 => 无上界）
  下界 = x 到 t_0 的最短路取负（不可达 => 无下界）

不可满足时返回项数最少的矛盾集：矛盾即负环，环上各边所属输入项构成
不可满足子集。平局按创建序号 seq 的升序字典序选取。
"""

from __future__ import annotations

from dataclasses import dataclass

INF = 10 ** 30


@dataclass(frozen=True, slots=True)
class Edge:
    u: int       # t_u
    v: int       # t_v，约束 t_v - t_u <= w
    w: int
    item: str    # 输入项 id（事件 id 或关系 id）
    seq: int     # 输入项创建序号（全局递增，字典序平局用）


def _relax_zero(edges: list[Edge], dist: list[int]) -> bool:
    changed = False
    for e in edges:
        nd = dist[e.u] + e.w
        if nd < dist[e.v]:
            dist[e.v] = nd
            changed = True
    return changed


def _relax_origin(edges: list[Edge], dist: list[int | None]) -> bool:
    changed = False
    for e in edges:
        du = dist[e.u]
        if du is None:
            continue
        nd = du + e.w
        dv = dist[e.v]
        if dv is None or nd < dv:
            dist[e.v] = nd
            changed = True
    return changed


def analyze(
    n_nodes: int, edges: list[Edge]
) -> tuple[bool, list[int | None], list[int | None]]:
    """返回 (可满足, 下界数组, 上界数组)；节点 0 为固定时间原点。

    不可满足时上下界数组为空。
    """
    # 1) 可行性：全零初始化等价于加入权 0 的虚拟超源
    dist = [0] * n_nodes
    for _ in range(n_nodes - 1):
        if not _relax_zero(edges, dist):
            break
    if _relax_zero(edges, dist):
        return False, [], []

    # 2) 上界：固定原点 0 出发的最短路
    upper: list[int | None] = [None] * n_nodes
    upper[0] = 0
    for _ in range(n_nodes - 1):
        if not _relax_origin(edges, upper):
            break

    # 3) 下界：反图上从原点出发的最短路取负
    rev = [Edge(e.v, e.u, e.w, e.item, e.seq) for e in edges]
    back: list[int | None] = [None] * n_nodes
    back[0] = 0
    for _ in range(n_nodes - 1):
        if not _relax_origin(rev, back):
            break
    lower = [None if d is None else -d for d in back]
    return True, lower, upper


# ---------------------------------------------------------------------------
# 最小矛盾集
# ---------------------------------------------------------------------------

def _single_item_unsats(edges: list[Edge]) -> list[int]:
    """单项即不可满足：负自环，或同一项在同一对节点上的两边权和为负
    （例如 "至多 0 分钟" 产生的 -1 / 0 边对）。"""
    found: dict[str, int] = {}
    # (item, 小端, 大端) -> {方向(u,v): 权}
    pair_edges: dict[tuple[str, int, int], dict[tuple[int, int], int]] = {}
    for e in edges:
        if e.u == e.v and e.w < 0:
            found[e.item] = e.seq
        elif e.u != e.v:
            a, b = (e.u, e.v) if e.u < e.v else (e.v, e.u)
            key = (e.item, a, b)
            pair_edges.setdefault(key, {})[(e.u, e.v)] = e.w
    for (item, _a, _b), dirs in pair_edges.items():
        if len(dirs) == 2 and sum(dirs.values()) < 0:
            for e in edges:
                if e.item == item:
                    found[item] = e.seq
                    break
    return sorted(found.values())


def min_contradiction(
    n_nodes: int, edges: list[Edge], max_seq: int
) -> list[int]:
    """返回矛盾集的项 seq 升序列表：项数最少，平局字典序最小。"""
    singles = _single_item_unsats(edges)
    if singles:
        return [singles[0]]

    adj: list[list[Edge]] = [[] for _ in range(n_nodes)]
    for e in edges:
        adj[e.u].append(e)

    # 阶段 1：分层矩阵 DP。
    # d[u][s] = 从 s 出发恰好走 k 条边到 u 的最小权（k=0 时 d[s][s]=0）。
    d = [[INF] * n_nodes for _ in range(n_nodes)]
    for i in range(n_nodes):
        d[i][i] = 0
    k_star: int | None = None
    hit: list[int] = []
    for _k in range(1, n_nodes + 1):
        nd = [[INF] * n_nodes for _ in range(n_nodes)]
        for e in edges:
            ru = d[e.u]
            rv = nd[e.v]
            w = e.w
            for s in range(n_nodes):
                base = ru[s]
                if base != INF:
                    val = base + w
                    if val < rv[s]:
                        rv[s] = val
        d = nd
        hit = [s for s in range(n_nodes) if d[s][s] < 0]
        if hit:
            k_star = _k
            break
    if k_star is None:
        return []

    # 阶段 2：在所有长度恰为 k* 的负环中，选序号集合字典序最小者。
    # 得分 = sum(BASE ** (max_seq - seq))；简单环上每项至多一条边，
    # 数字为 0/1 数位，无进位，得分大者字典序小。
    BASE = 10000
    powers = {q: BASE ** (max_seq - q) for q in range(1, max_seq + 1)}

    # 状态元组: (终点 t, 权 w, 得分 sc, 前驱层, 前驱状态索引, 项 seq)
    best: list[int] | None = None
    for s in hit:
        layers: list[list[tuple]] = [[(s, 0, 0, -1, -1, -1)]]
        for _step in range(k_star):
            prev = layers[-1]
            # 同一 (终点, 权) 只需保留得分最高的扩展
            bucket: dict[tuple[int, int], tuple] = {}
            for pi, st in enumerate(prev):
                t, w0, sc0, _pl, _pi, _it = st
                layer_no = len(layers) - 1
                for e in adj[t]:
                    w1 = w0 + e.w
                    sc1 = sc0 + powers[e.seq]
                    key = (e.v, w1)
                    old = bucket.get(key)
                    cand = (e.v, w1, sc1, layer_no, pi, e.seq)
                    if old is None or sc1 > old[2]:
                        bucket[key] = cand
            # Pareto 剪枝：同终点按权升序，仅保留得分刷新最大值的状态
            by_t: dict[int, list[tuple]] = {}
            for st in bucket.values():
                by_t.setdefault(st[0], []).append(st)
            pruned: list[tuple] = []
            for lst in by_t.values():
                lst.sort(key=lambda z: (z[1], -z[2]))
                max_score = -1
                for st in lst:
                    if st[2] > max_score:
                        pruned.append(st)
                        max_score = st[2]
            layers.append(pruned)

        cands = [st for st in layers[k_star] if st[0] == s and st[1] < 0]
        if not cands:
            continue
        cur = max(cands, key=lambda z: z[2])
        seqs: list[int] = []
        for _ in range(k_star):
            seqs.append(cur[5])
            cur = layers[cur[3]][cur[4]]
        seqs.sort()
        if best is None or seqs < best:
            best = seqs
    return best or []
