"""分叉合并：字段级三方合并。

base: 分叉点版本快照；mine: 目标分支工作区；theirs: 来源分支工作区。

规则：
- 无交叠改动自动合入；
- 同字段双方都改成不同值 => 字段冲突，逐项选择；
- 同一实体一方删除、另一方修改 => 删除/修改冲突；
- 双方都删除 => 删除；仅一方删除且对方无改动 => 删除；
- 新增实体自动合入（ref 全局唯一，不存在新增撞号）；
- 合并后引用了已删除事件的关系自动丢弃，计入 dropped。
"""
from copy import deepcopy

EVENT_FIELDS = ("name", "lower_bound", "upper_bound")
RELATION_FIELDS = ("a_ref", "b_ref", "kind", "n")


def _index(state):
    return (
        {"E" + str(e["ref"]): dict(e) for e in state["events"]},
        {"R" + str(r["ref"]): dict(r) for r in state["relations"]},
    )


def _diff_fields(base, other, fields):
    """other 相对 base 改了哪些字段。"""
    if base is None:
        return set(fields)
    return {f for f in fields if base.get(f) != other.get(f)}


def _is_new(key, base_map, mine_map, theirs_map):
    return key not in base_map


def analyze_merge(base, mine, theirs):
    """生成合并预览。

    返回:
      auto: 无冲突合并后的完整状态（冲突字段先取 mine）
      conflicts: [
        {type: 'entity', key, kind:'event'|'relation',
         action: 'deleted_modified', deleted_by: 'mine'|'theirs',
         current: <幸存方实体>},
        {type: 'field', key, kind, field,
         base, mine, theirs, a_ref/... 附带实体引用供前端展示}
      ]
      dropped: [relation_key...] 因事件被删除而连带丢弃
    """
    be, br = _index(base)
    me, mr = _index(mine)
    te, tr = _index(theirs)

    events_out = {}
    rels_out = {}
    conflicts = []

    # ---- 事件 ----
    for key in set(be) | set(me) | set(te):
        b, m, t = be.get(key), me.get(key), te.get(key)
        if b is None:  # 新增
            if m and t:
                # 双方各自新增同 key 不可能（ref 全局唯一）；同存则比对
                merged, cf = _merge_fields(key, "event", m, m, t, EVENT_FIELDS)
                events_out[key] = merged
                conflicts.extend(cf)
            else:
                events_out[key] = deepcopy(m or t)
            continue

        if m is None and t is None:
            continue  # 双方都删除
        if m is None or t is None:
            survivor = m if m is not None else t
            changed = _diff_fields(b, survivor, EVENT_FIELDS)
            if not changed:
                continue  # 一方删除、一方未改 => 删除
            conflicts.append({
                "type": "entity", "kind": "event", "key": key,
                "ref": b["ref"],
                "action": "deleted_modified",
                "deleted_by": "mine" if m is None else "theirs",
                "base": deepcopy(b), "current": deepcopy(survivor),
            })
            events_out[key] = deepcopy(survivor)  # 待用户裁决
            continue

        # 双方都保留：字段级合并
        merged, cf = _merge_fields(key, "event", b, m, t, EVENT_FIELDS)
        events_out[key] = merged
        conflicts.extend(cf)

    # ---- 关系 ----
    for key in set(br) | set(mr) | set(tr):
        b, m, t = br.get(key), mr.get(key), tr.get(key)
        if b is None:
            if m and t:
                merged, cf = _merge_fields(key, "relation", m, m, t, RELATION_FIELDS)
                rels_out[key] = merged
                conflicts.extend(cf)
            else:
                rels_out[key] = deepcopy(m or t)
            continue

        if m is None and t is None:
            continue
        if m is None or t is None:
            survivor = m if m is not None else t
            changed = _diff_fields(b, survivor, RELATION_FIELDS)
            if not changed:
                continue
            conflicts.append({
                "type": "entity", "kind": "relation", "key": key,
                "ref": b["ref"],
                "action": "deleted_modified",
                "deleted_by": "mine" if m is None else "theirs",
                "base": deepcopy(b), "current": deepcopy(survivor),
            })
            rels_out[key] = deepcopy(survivor)
            continue

        merged, cf = _merge_fields(key, "relation", b, m, t, RELATION_FIELDS)
        rels_out[key] = merged
        conflicts.extend(cf)

    # ---- 丢弃引用已删除事件的关系 ----
    dropped = []
    for key, r in list(rels_out.items()):
        if ("E" + str(r["a_ref"])) not in events_out or \
           ("E" + str(r["b_ref"])) not in events_out:
            dropped.append(key)
            del rels_out[key]

    state = {
        "events": [events_out[k] for k in sorted(events_out)],
        "relations": [rels_out[k] for k in sorted(rels_out)],
    }
    conflicts.sort(key=lambda c: (0 if c["type"] == "entity" else 1, c["key"]))
    return {"state": state, "conflicts": conflicts, "dropped": dropped}


def _merge_fields(key, kind, base, mine, theirs, fields):
    """对一条双方都保留的记录做字段级合并。"""
    merged = {}
    conflicts = []
    for f in fields:
        bv = base.get(f)
        mv = mine.get(f)
        tv = theirs.get(f)
        if mv == tv:
            merged[f] = mv
        elif mv == bv:
            merged[f] = tv          # 只有对方改
        elif tv == bv:
            merged[f] = mv          # 只有我方改
        else:
            conflicts.append({
                "type": "field", "kind": kind, "key": key,
                "ref": base["ref"], "field": f,
                "base": bv, "mine": mv, "theirs": tv,
            })
            merged[f] = mv          # 预览先放我方值
    # 复制记录上其余标识
    if "ref" in base:
        merged["ref"] = base["ref"]
    elif "ref" in mine:
        merged["ref"] = mine["ref"]
    return merged, conflicts


def apply_resolutions(analysis, resolutions):
    """按用户裁决生成最终合并状态。

    resolutions:
      {"entities": {"E3": "keep"|"delete", ...},
       "fields":  {"E7.name": "mine"|"theirs", ...}}
    仍有未裁决冲突时抛 ValueError。
    """
    state = deepcopy(analysis["state"])
    ents = {("E" + str(e["ref"])): e for e in state["events"]}
    rels = {("R" + str(r["ref"])): r for r in state["relations"]}
    ent_res = resolutions.get("entities", {})
    fld_res = resolutions.get("fields", {})

    for c in analysis["conflicts"]:
        if c["type"] == "entity":
            choice = ent_res.get(c["key"])
            if choice not in ("keep", "delete"):
                raise ValueError(f"冲突 {c['key']} 尚未裁决")
            if choice == "delete":
                if c["kind"] == "event":
                    for r in list(state["relations"]):
                        if r["a_ref"] == c["ref"] or r["b_ref"] == c["ref"]:
                            state["relations"].remove(r)
                    state["events"] = [e for e in state["events"]
                                       if e["ref"] != c["ref"]]
                else:
                    state["relations"] = [r for r in state["relations"]
                                          if r["ref"] != c["ref"]]
        else:
            fkey = f"{c['key']}.{c['field']}"
            choice = fld_res.get(fkey)
            if choice not in ("mine", "theirs"):
                raise ValueError(f"冲突 {fkey} 尚未裁决")
            table = ents if c["kind"] == "event" else rels
            table[c["key"]][c["field"]] = c[choice]

    return state
