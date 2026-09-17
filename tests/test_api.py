"""端到端集成测试：API、事务原子性、分叉三方合并。"""
import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_tmp, "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def show(r):
    if r.status_code >= 400:
        print("   ERROR:", r.status_code, r.text[:300])


def test_state_and_edit():
    r = client.get("/api/branches/1/state")
    assert r.status_code == 200
    data = r.json()
    assert data["solve"]["feasible"] is True
    assert len(data["events"]) == 4

    # 新增事件
    r = client.post("/api/branches/1/events",
                    json={"name": "营销上线", "lower_bound": 0})
    assert r.status_code == 201, r.text
    ref = r.json()["events"][-1]["ref"]

    # 改范围
    r = client.patch(f"/api/branches/1/events/{ref}",
                     json={"upper_bound": 50})
    assert r.status_code == 200
    ev = next(e for e in r.json()["events"] if e["ref"] == ref)
    assert ev["lower_bound"] == 0 and ev["upper_bound"] == 50

    # 删除事件连带关系
    r = client.delete(f"/api/branches/1/events/{ref}")
    assert r.status_code == 200
    assert all(e["ref"] != ref for e in r.json()["events"])
    print("  ok  状态/编辑/级联删除")


def test_contradiction_and_core():
    r = client.get("/api/branches/1/state").json()
    e1, e2 = r["events"][0]["ref"], r["events"][1]["ref"]

    r = client.post("/api/branches/1/relations",
                    json={"a_ref": e2, "b_ref": e1, "kind": "earlier"})
    show(r)
    assert r.status_code == 201
    s = r.json()["solve"]
    assert s["feasible"] is False
    core = s["core"]
    # 原种子有 earlier(e1,e2)，新增 earlier(e2,e1) 构成二元矛盾
    assert set(core) == {5, r.json()["relations"][-1]["ref"]}

    # 删除新增关系后恢复可行
    r = client.delete(f"/api/branches/1/relations/{core[1]}")
    assert r.status_code == 200
    assert r.json()["solve"]["feasible"] is True
    print("  ok  矛盾检测与最少矛盾集")


def test_branch_and_merge_clean():
    # 提交 main
    r = client.post("/api/branches/1/commit", json={"message": "main 提交"})
    assert r.status_code == 200

    # 分叉 feature
    r = client.post("/api/branches", json={"name": "feature-clean"})
    assert r.status_code == 201, r.text
    fid = r.json()["branch"]["id"]

    # feature 新增事件；main 新增另一个事件 => 无交叠自动合并
    r = client.post(f"/api/branches/{fid}/events",
                    json={"name": "分支侧事件"})
    assert r.status_code == 201
    client.post(f"/api/branches/{fid}/commit", json={"message": "f"})

    r = client.post("/api/branches/1/events", json={"name": "主干侧事件"})
    assert r.status_code == 201
    main_new = r.json()["events"][-1]["ref"]

    r = client.post("/api/branches/1/merges/preview",
                    json={"source_id": fid})
    assert r.status_code == 200
    assert r.json()["conflicts"] == []

    head = client.get("/api/branches/1/state").json()["branch"]["head_id"]
    r = client.post("/api/branches/1/merges/commit",
                    json={"source_id": fid, "expected_head": head,
                          "resolutions": {}})
    assert r.status_code == 200, r.text
    names = [e["name"] for e in r.json()["events"]]
    assert "分支侧事件" in names and "主干侧事件" in names
    assert r.json()["solve"]["feasible"] is True
    print("  ok  无交叠自动合并")


def test_merge_field_conflict():
    client.post("/api/branches/1/commit", json={"message": "基线"})
    r = client.post("/api/branches", json={"name": "feature-conflict"})
    fid = r.json()["branch"]["id"]
    tgt = r.json()["events"][0]["ref"]

    # 双方改同一事件名字段为不同值
    client.patch(f"/api/branches/{fid}/events/{tgt}",
                 json={"name": "分支命名"})
    client.post(f"/api/branches/{fid}/commit", json={"message": "f"})
    client.patch(f"/api/branches/1/events/{tgt}", json={"name": "主干命名"})

    r = client.post("/api/branches/1/merges/preview",
                    json={"source_id": fid})
    data = r.json()
    field_conflicts = [c for c in data["conflicts"] if c["type"] == "field"]
    assert any(c["field"] == "name" and c["mine"] == "主干命名"
               and c["theirs"] == "分支命名" for c in field_conflicts)

    key = f"E{tgt}.name"
    head = client.get("/api/branches/1/state").json()["branch"]["head_id"]

    # 不解决冲突直接提交 => 409 且不写入
    r = client.post("/api/branches/1/merges/commit",
                    json={"source_id": fid, "expected_head": head,
                          "resolutions": {}})
    assert r.status_code == 409

    # 基线版本不匹配 => 409
    r = client.post("/api/branches/1/merges/commit",
                    json={"source_id": fid, "expected_head": 99999,
                          "resolutions": {"fields": {key: "theirs"}}})
    assert r.status_code == 409

    # 正确裁决 => 采用对方值
    r = client.post("/api/branches/1/merges/commit",
                    json={"source_id": fid, "expected_head": head,
                          "resolutions": {"fields": {key: "theirs"}}})
    assert r.status_code == 200, r.text
    ev = next(e for e in r.json()["events"] if e["ref"] == tgt)
    assert ev["name"] == "分支命名"
    print("  ok  同字段冲突逐项裁决与乐观锁")


def test_merge_unsat_rejected():
    # main: 事件 X [0,10]；分支: 事件 X [100,200]（同字段冲突，选对方），
    # 同时 main 加关系迫使矛盾 => 合并提交必须整体失败
    client.post("/api/branches/1/commit", json={"message": "基线2"})
    r = client.post("/api/branches", json={"name": "feature-unsat"})
    fid = r.json()["branch"]["id"]
    tgt = r.json()["events"][0]["ref"]

    client.patch(f"/api/branches/{fid}/events/{tgt}",
                 json={"lower_bound": 100, "upper_bound": 200})
    client.post(f"/api/branches/{fid}/commit", json={"message": "f"})

    # main 上把另一事件约束到必须早于 tgt 且上界 < 100
    data = client.get("/api/branches/1/state").json()
    other = next(e["ref"] for e in data["events"] if e["ref"] != tgt)
    client.patch(f"/api/branches/1/events/{other}",
                 json={"lower_bound": 0, "upper_bound": 0})
    # tgt 上界压到 0 让 100 下界无解（通过冲突字段选 theirs 后下界100）
    client.patch(f"/api/branches/1/events/{tgt}",
                 json={"lower_bound": 0, "upper_bound": 0})

    r = client.post("/api/branches/1/merges/preview",
                    json={"source_id": fid})
    conflicts = r.json()["conflicts"]
    res = {"fields": {f"{c['key']}.{c['field']}": "theirs"
                      for c in conflicts if c["type"] == "field"},
           "entities": {c["key"]: "keep"
                        for c in conflicts if c["type"] == "entity"}}
    head = client.get("/api/branches/1/state").json()["branch"]["head_id"]
    r = client.post("/api/branches/1/merges/commit",
                    json={"source_id": fid, "expected_head": head,
                          "resolutions": res})
    assert r.status_code == 409
    # 失败不得部分写入：工作区保持合并前状态
    after = client.get("/api/branches/1/state").json()
    ev = next(e for e in after["events"] if e["ref"] == tgt)
    assert ev["lower_bound"] == 0 and ev["upper_bound"] == 0
    print("  ok  不可满足合并整体回滚")


if __name__ == "__main__":
    print("集成测试")
    test_state_and_edit()
    test_contradiction_and_core()
    test_branch_and_merge_clean()
    test_merge_field_conflict()
    test_merge_unsat_rejected()
    print("全部通过")
