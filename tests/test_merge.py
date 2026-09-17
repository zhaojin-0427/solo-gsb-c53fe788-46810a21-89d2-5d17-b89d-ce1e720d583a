"""字段级三方合并单元测试。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.merge import analyze_merge, apply_resolutions


def E(ref, name, lb=None, ub=None):
    return {"ref": ref, "name": name, "lower_bound": lb, "upper_bound": ub}


def R(ref, a, b, kind, n=None):
    return {"ref": ref, "a_ref": a, "b_ref": b, "kind": kind, "n": n}


def test_non_overlapping():
    base = {"events": [E(1, "A", 0, 10), E(2, "B")],
            "relations": [R(10, 1, 2, "earlier")]}
    mine = {"events": [E(1, "A", 0, 10), E(2, "B"), E(3, "C")],
            "relations": [R(10, 1, 2, "earlier")]}
    theirs = {"events": [E(1, "A", 0, 20), E(2, "B-rename")],
              "relations": [R(10, 1, 2, "earlier"), R(11, 2, 1, "later")]}
    a = analyze_merge(base, mine, theirs)
    assert a["conflicts"] == [], a["conflicts"]
    e1 = next(e for e in a["state"]["events"] if e["ref"] == 1)
    e2 = next(e for e in a["state"]["events"] if e["ref"] == 2)
    e3 = next(e for e in a["state"]["events"] if e["ref"] == 3)
    assert e1["upper_bound"] == 20           # 对方改动自动合入
    assert e2["name"] == "B-rename"          # 对方改动自动合入
    assert e3["name"] == "C"                 # 我方新增自动合入
    assert {r["ref"] for r in a["state"]["relations"]} == {10, 11}
    print("  ok  无交叠改动自动合入")


def test_field_conflict():
    base = {"events": [E(1, "X", 0, 10)], "relations": []}
    mine = {"events": [E(1, "X", None, 20)], "relations": []}
    theirs = {"events": [E(1, "X", 5, 30)], "relations": []}
    a = analyze_merge(base, mine, theirs)
    assert len(a["conflicts"]) == 2, a["conflicts"]
    fields = {c["field"]: c for c in a["conflicts"]}
    assert fields["lower_bound"]["mine"] is None and fields["lower_bound"]["theirs"] == 5
    assert fields["upper_bound"]["mine"] == 20 and fields["upper_bound"]["theirs"] == 30

    # 未裁决 => 报错
    try:
        apply_resolutions(a, {"fields": {}})
        assert False, "应拒绝未裁决合并"
    except ValueError:
        pass

    merged = apply_resolutions(a, {"fields": {
        "E1.lower_bound": "theirs", "E1.upper_bound": "mine"}})
    e = merged["events"][0]
    assert e["lower_bound"] == 5 and e["upper_bound"] == 20
    print("  ok  同字段冲突逐项选择")


def test_delete_modify_and_both_delete():
    # E1: 我方删除、对方修改 => 删除/修改冲突
    # E2: 对方删除、我方修改 => 删除/修改冲突
    # E3: 双方都删除 => 直接删除
    # E4: 仅对方删除、我方未改 => 自动删除
    # E5: 双方都保留不动 => 保留
    base = {"events": [E(1, "A"), E(2, "B"), E(3, "C"), E(4, "D"), E(5, "E")],
            "relations": []}
    mine = {"events": [E(2, "B-mod"), E(4, "D"), E(5, "E")], "relations": []}
    theirs = {"events": [E(1, "A-mod"), E(5, "E")], "relations": []}
    a = analyze_merge(base, mine, theirs)
    kinds = {c["key"]: c for c in a["conflicts"] if c["type"] == "entity"}
    assert set(kinds) == {"E1", "E2"}, kinds
    assert kinds["E1"]["deleted_by"] == "mine"
    assert kinds["E2"]["deleted_by"] == "theirs"

    merged = apply_resolutions(a, {"entities": {"E1": "delete", "E2": "keep"}})
    refs = {e["ref"] for e in merged["events"]}
    # E1 确认删除；E2 保留修改；E3/E4 自动删除；E5 保留
    assert refs == {2, 5}, refs
    assert next(e for e in merged["events"] if e["ref"] == 2)["name"] == "B-mod"
    print("  ok  删除/修改冲突与双方删除")


def test_relation_dropped_on_event_delete():
    # R10 双方都保留；事件 B(2) 仅对方删除、我方未改 => 自动删除，
    # 合并时 R10 因端点消失而被连带丢弃并计入 dropped。
    base = {"events": [E(1, "A"), E(2, "B")],
            "relations": [R(10, 1, 2, "earlier")]}
    mine = {"events": [E(1, "A"), E(2, "B")], "relations": [R(10, 1, 2, "earlier")]}
    theirs = {"events": [E(1, "A")], "relations": [R(10, 1, 2, "earlier")]}
    a = analyze_merge(base, mine, theirs)
    assert a["conflicts"] == [], a["conflicts"]
    assert a["state"]["relations"] == []
    assert a["dropped"] == ["R10"], a["dropped"]
    print("  ok  事件删除连带丢弃关系")


if __name__ == "__main__":
    print("三方合并测试")
    test_non_overlapping()
    test_field_conflict()
    test_delete_modify_and_both_delete()
    test_relation_dropped_on_event_delete()
    print("全部通过")
