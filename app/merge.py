"""分叉版本的字段级三方合并。

base / ours / theirs 均为同一结构的完整状态（含墓碑）。
  - 无交叠改动自动合入；
  - 同字段双方都改为不同值、删除/修改相撞 => 逐项冲突，供前端选择；
  - resolutions: {"event:<id>:<field>": "ours"|"theirs",
                  "event:<id>:__delete__": ..., "relation:..." : ...}
    实体级删除冲突的字段名为 __delete__。
提交必须带齐全部冲突的裁决，否则返回未决冲突；合并结果再做一次可满足性复核。
"""

from __future__ import annotations

from .db import clone_state
from .model import EVENT_FIELDS, REL_FIELDS


def _conflict_key(kind: str, eid: str, field: str) -> str:
    return f"{kind}:{eid}:{field}"


def _merge_entity(kind: str, eid: str, fields, base, ours, theirs,
                  merged, resolutions, conflicts):
    b = base or {}
    o = ours or {}
    t = theirs or {}

    o_deleted = bool(ours and ours.get("deleted"))
    t_deleted = bool(theirs and theirs.get("deleted"))
    o_changed_entity = not ours or o_deleted or any(
        f in o and o.get(f) != b.get(f) for f in fields)
    t_changed_entity = not theirs or t_deleted or any(
        f in t and t.get(f) != b.get(f) for f in fields)

    # 双方都删除
    if o_deleted and t_deleted:
        merged["deleted"] = True
        return
    # 一方删除，另一方未动 => 自动删除
    if o_deleted and not t_changed_entity:
        merged["deleted"] = True
        return
    if t_deleted and not o_changed_entity:
        merged["deleted"] = True
        return
    # 删除 / 修改相撞
    if o_deleted or t_deleted:
        key = _conflict_key(kind, eid, "__delete__")
        if key in resolutions:
            side = resolutions[key]
            if side == "ours":
                if o_deleted:
                    merged["deleted"] = True
                else:
                    merged.update({f: o.get(f) for f in fields})
            else:
                if t_deleted:
                    merged["deleted"] = True
                else:
                    merged.update({f: t.get(f) for f in fields})
        else:
            side = None
        conflicts.append({
            "key": key, "kind": kind, "id": eid, "field": "__delete__",
            "type": "delete_modify",
            "ours": None if o_deleted else {f: o.get(f) for f in fields},
            "theirs": None if t_deleted else {f: t.get(f) for f in fields},
            "resolution": side,
        })
        return

    # 双方都新建（base 缺失）或双方都修改：字段级比对
    merged["deleted"] = False
    for f in fields:
        in_o = f in o
        in_t = f in t
        ov = o.get(f)
        tv = t.get(f)
        bv = b.get(f)
        o_changed = in_o and ov != bv
        t_changed = in_t and tv != bv
        if not o_changed:
            merged[f] = tv if t_changed else bv
        elif not t_changed:
            merged[f] = ov
        elif ov == tv:
            merged[f] = ov
        else:
            key = _conflict_key(kind, eid, f)
            if key in resolutions:
                side = resolutions[key]
                merged[f] = ov if side == "ours" else tv
            else:
                side = None
            conflicts.append({
                "key": key, "kind": kind, "id": eid, "field": f,
                "type": "both_modified",
                "ours": ov, "theirs": tv, "resolution": side,
            })


def three_way_merge(base: dict, ours: dict, theirs: dict,
                    resolutions: dict | None = None) -> tuple[dict, list[dict]]:
    resolutions = resolutions or {}
    result = clone_state(base)

    for kind, fields in (("event", EVENT_FIELDS), ("relation", REL_FIELDS)):
        bm, om, tm = base[kind + "s"], ours[kind + "s"], theirs[kind + "s"]
        rm = result[kind + "s"]
        all_ids = set(bm) | set(om) | set(tm)
        for eid in sorted(all_ids, key=lambda x: (om.get(x) or tm.get(x) or bm.get(x))["seq"]):
            b, o, t = bm.get(eid), om.get(eid), tm.get(eid)
            conflicts: list[dict] = []
            # 以 o/t 的并集字段准备 merged 基底
            ref = o or t or b
            merged = {k: ref[k] for k in ref}
            _merge_entity(kind, eid, fields, b, o, t, merged,
                          resolutions, conflicts)
            if conflicts:
                result.setdefault("_conflicts", []).extend(conflicts)
            rm[eid] = merged

    # 合并后仍被删除的实体保持墓碑；result 克隆自 base 已包含
    conflicts = result.pop("_conflicts", [])
    return result, conflicts


def auto_changes(base: dict, merged: dict) -> dict:
    """列出自动合入与冲突占位之外的差异，供前端展示。"""
    changes = {"events": [], "relations": []}
    for kind, out in (("event", "events"), ("relation", "relations")):
        bm = base[out]
        mm = merged[out]
        for eid, m in mm.items():
            b = bm.get(eid)
            if b is None:
                changes[out].append({"id": eid, "action": "created"})
            elif m.get("deleted") and not b.get("deleted"):
                changes[out].append({"id": eid, "action": "deleted"})
            elif any(m.get(f) != b.get(f) for f in m if f not in ("id", "seq", "deleted")):
                changes[out].append({"id": eid, "action": "modified"})
    return changes
