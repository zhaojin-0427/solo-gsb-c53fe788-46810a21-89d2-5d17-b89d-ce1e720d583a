"""状态变更校验、差分约束建模与全量分析。"""

from __future__ import annotations

from typing import Callable

from . import db
from .solver import Edge, analyze, min_contradiction

REL_TYPES = {"earlier", "later", "same", "at_least", "at_most"}
EVENT_FIELDS = ("label", "lower", "upper")
REL_FIELDS = ("type", "a", "b", "n")


class ValidationError(Exception):
    """输入不合法（400）。"""


class ConflictError(Exception):
    """基线不匹配或提交内容不可满足（409/422）。"""

    def __init__(self, message: str, contradiction: list[dict] | None = None,
                 code: str = "unsatisfiable"):
        super().__init__(message)
        self.code = code
        self.contradiction = contradiction or []


# ---------------------------------------------------------------------------

def _as_int(v, name: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValidationError(f"{name} 必须是整数")
    return v


def _opt_int(v, name: str):
    if v is None:
        return None
    return _as_int(v, name)


def apply_operations(
    state: dict, ops: list[dict], alloc_seq: Callable[[], int]
) -> None:
    """把一批操作就地应用到 state；非法输入抛 ValidationError。

    操作: {op: event_create, label, lower?, upper?}
          {op: event_update, id, label?, lower?, upper?, clear?:[...]}
          {op: event_delete, id}
          {op: relation_create, type, a, b, n?}
          {op: relation_update, id, ...同创建}
          {op: relation_delete, id}
    """
    if not isinstance(ops, list):
        raise ValidationError("ops 必须是数组")

    # 先做删除，保证同一批次内对将删关系的更新被自然忽略
    for o in ops:
        kind = o.get("op", "")
        if kind == "event_delete":
            eid = o.get("id")
            ev = state["events"].get(eid)
            if ev is None:
                raise ValidationError(f"事件不存在: {eid}")
            ev["deleted"] = True
        elif kind == "relation_delete":
            rid = o.get("id")
            rel = state["relations"].get(rid)
            if rel is None:
                raise ValidationError(f"关系不存在: {rid}")
            rel["deleted"] = True

    for o in ops:
        kind = o.get("op", "")
        if kind == "event_create":
            label = str(o.get("label") or "").strip()
            if not label:
                raise ValidationError("事件名称不能为空")
            lower = _opt_int(o.get("lower"), "下界")
            upper = _opt_int(o.get("upper"), "上界")
            if lower is not None and upper is not None and lower > upper:
                # 允许创建，但必然不可满足；仍校验整数范围本身
                pass
            eid = db.new_id()
            state["events"][eid] = {
                "id": eid, "seq": alloc_seq(), "label": label,
                "lower": lower, "upper": upper, "deleted": False,
            }
        elif kind == "event_update":
            eid = o.get("id")
            ev = state["events"].get(eid)
            if ev is None:
                raise ValidationError(f"事件不存在: {eid}")
            if "label" in o:
                label = str(o.get("label") or "").strip()
                if not label:
                    raise ValidationError("事件名称不能为空")
                ev["label"] = label
            clear = set(o.get("clear", []))
            if "lower" in o:
                ev["lower"] = _opt_int(o.get("lower"), "下界")
            if "lower" in clear:
                ev["lower"] = None
            if "upper" in o:
                ev["upper"] = _opt_int(o.get("upper"), "上界")
            if "upper" in clear:
                ev["upper"] = None
        elif kind in ("relation_create", "relation_update"):
            rid = o.get("id") if kind == "relation_update" else None
            rel = state["relations"].get(rid) if rid else None
            if kind == "relation_update" and rel is None:
                raise ValidationError(f"关系不存在: {rid}")
            rtype = o.get("type", rel["type"] if rel else None)
            if rtype not in REL_TYPES:
                raise ValidationError(f"未知关系类型: {rtype}")
            a = o.get("a", rel["a"] if rel else None)
            b = o.get("b", rel["b"] if rel else None)
            evs = state["events"]
            if a not in evs or evs[a].get("deleted"):
                raise ValidationError("关系引用的事件 A 不存在")
            if b not in evs or evs[b].get("deleted"):
                raise ValidationError("关系引用的事件 B 不存在")
            n_val = o.get("n", rel["n"] if rel else None)
            if rtype in ("at_least", "at_most"):
                n_val = _as_int(n_val, "n")
                if n_val < 0:
                    raise ValidationError("n 必须是非负整数")
            else:
                n_val = None
            if kind == "relation_create":
                new_rid = db.new_id()
                state["relations"][new_rid] = {
                    "id": new_rid, "seq": alloc_seq(), "type": rtype,
                    "a": a, "b": b, "n": n_val, "deleted": False,
                }
            else:
                rel.update({"type": rtype, "a": a, "b": b, "n": n_val})
        elif kind not in ("event_delete", "relation_delete"):
            raise ValidationError(f"未知操作: {kind}")

    # 级联：事件已删除则其关联关系一并墓碑化
    deleted_events = {
        eid for eid, e in state["events"].items() if e.get("deleted")
    }
    if deleted_events:
        for rel in state["relations"].values():
            if not rel.get("deleted") and (rel["a"] in deleted_events or rel["b"] in deleted_events):
                rel["deleted"] = True


# ---------------------------------------------------------------------------

def relation_edges(rel: dict, na: int, nb: int) -> list[Edge]:
    """以 a 相对 b 的方向生成边（u->v 权 w 表示 t_v-t_u<=w）。"""
    i, s = rel["id"], rel["seq"]
    t = rel["type"]
    if t == "earlier":                       # a 早于 b: t_a-t_b<=-1 => 边 b->a
        return [Edge(nb, na, -1, i, s)]
    if t == "later":                         # a 晚于 b: t_b-t_a<=-1 => 边 a->b
        return [Edge(na, nb, -1, i, s)]
    if t == "same":
        return [Edge(na, nb, 0, i, s), Edge(nb, na, 0, i, s)]
    if t == "at_least":                      # t_a-t_b>=max(1,n) => t_b-t_a<=-max(1,n)
        return [Edge(na, nb, -max(1, rel["n"]), i, s)]
    # at_most: 1 <= t_a-t_b <= n
    return [Edge(nb, na, rel["n"], i, s), Edge(na, nb, -1, i, s)]


def build_edges(state: dict) -> tuple[dict[str, int], list[Edge]]:
    events, relations = db.live(state)
    nodes = {"origin": 0}
    for idx, ev in enumerate(events, start=1):
        nodes[ev["id"]] = idx
    edges: list[Edge] = []
    for ev in events:
        v = nodes[ev["id"]]
        if ev["lower"] is not None:
            edges.append(Edge(v, 0, -ev["lower"], ev["id"], ev["seq"]))
        if ev["upper"] is not None:
            edges.append(Edge(0, v, ev["upper"], ev["id"], ev["seq"]))
    for rel in relations:
        edges.extend(relation_edges(rel, nodes[rel["a"]], nodes[rel["b"]]))
    return nodes, edges


def _item_index(state: dict) -> dict[int, dict]:
    idx: dict[int, dict] = {}
    for ev in state["events"].values():
        idx[ev["seq"]] = {"kind": "event", "id": ev["id"], "label": ev["label"]}
    for rel in state["relations"].values():
        idx[rel["seq"]] = {"kind": "relation", "id": rel["id"]}
    return idx


def analyze_state(state: dict, max_seq: int) -> dict:
    nodes, edges = build_edges(state)
    n = len(nodes)
    ok, lower, upper = analyze(n, edges)
    if not ok:
        seqs = min_contradiction(n, edges, max_seq)
        item_idx = _item_index(state)
        contradiction = [item_idx[s] for s in seqs if s in item_idx]
        return {"satisfiable": False, "contradiction": contradiction,
                "bounds": {}}
    events, _rels = db.live(state)
    bounds = {}
    for ev in events:
        v = nodes[ev["id"]]
        bounds[ev["id"]] = {"lower": lower[v], "upper": upper[v]}
    return {"satisfiable": True, "contradiction": [], "bounds": bounds}
