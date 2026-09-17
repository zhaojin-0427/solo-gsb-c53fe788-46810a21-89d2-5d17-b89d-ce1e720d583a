"""端到端 API 测试（FastAPI TestClient，覆盖全部业务规则）。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, "/workspace")

TEST_DB = Path("/workspace/data/app.db")
for p in TEST_DB.parent.glob("app.db*"):
    p.unlink()

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

PASS = 0
CTX = {"base": None}


def check(cond, msg):
    global PASS
    assert cond, msg
    PASS += 1


with TestClient(app) as client:
    st = client.get("/api/state").json()
    CTX["base"] = st["head_id"]

    def commit(branch, ops, base_id, msg="t"):
        return client.post(f"/api/branches/{branch}/commit",
                           json={"base_id": base_id, "ops": ops, "message": msg})

    def new_commit(ops, msg="t"):
        r = commit("main", ops, CTX["base"], msg)
        if r.status_code == 200:
            CTX["base"] = r.json()["head_id"]
        return r

    base = property(lambda self: CTX["base"]) if False else None

    # 1) 创建事件
    r = new_commit([
        {"op": "event_create", "label": "A", "lower": 3, "upper": 10},
        {"op": "event_create", "label": "B", "lower": 5, "upper": 12},
        {"op": "event_create", "label": "C", "lower": 0, "upper": 20},
    ], "events")
    check(r.status_code == 200, r.text)
    st = r.json()
    A, B, C = [e["id"] for e in st["events"]]
    bnd = st["analysis"]["bounds"]
    check(bnd[A] == {"lower": 3, "upper": 10}, bnd[A])
    check(bnd[B] == {"lower": 5, "upper": 12}, bnd[B])
    check(st["analysis"]["satisfiable"], "should be sat")

    # 2) A 晚于 B 至少 2 分钟 => A∈[7,10], B∈[5,8]
    r = new_commit([{"op": "relation_create", "type": "at_least",
                     "a": A, "b": B, "n": 2}])
    check(r.status_code == 200, r.text)
    bnd = r.json()["analysis"]["bounds"]
    check(bnd[A] == {"lower": 7, "upper": 10}, bnd[A])
    check(bnd[B] == {"lower": 5, "upper": 8}, bnd[B])

    # 3) C 早于 A（t_C+1<=t_A）=> C∈[0,6]
    r = new_commit([{"op": "relation_create", "type": "earlier", "a": C, "b": A}])
    check(r.status_code == 200, r.text)
    bnd = r.json()["analysis"]["bounds"]
    check(bnd[C] == {"lower": 0, "upper": 9}, bnd[C])

    # 4) B 与 C 同一时刻 => B=C∈[5,6]，再与 A 晚于 B 至少 2 => A∈[7,10]
    r = new_commit([{"op": "relation_create", "type": "same", "a": B, "b": C}])
    check(r.status_code == 200, r.text)
    bnd = r.json()["analysis"]["bounds"]
    check(bnd[B] == {"lower": 5, "upper": 8}, bnd[B])
    check(bnd[C] == {"lower": 5, "upper": 8}, bnd[C])

    # 5) 制造矛盾：再加 A 早于 C（t_A+1<=t_C），与 A>=C+3 冲突 => 422 回滚
    r = new_commit([{"op": "relation_create", "type": "earlier", "a": A, "b": C}])
    check(r.status_code == 422, r.text)
    d = r.json()["detail"]
    check(d["code"] == "unsatisfiable", d)
    # 矛盾环：C早于A 与 A早于C 直接互斥 => 2 项最小环
    cont = d["contradiction"]
    check(len(cont) == 2, cont)
    check(all(c["kind"] == "relation" for c in cont), cont)
    # 回滚确认：状态 head 未变，关系仍是 3 条
    st2 = client.get("/api/state").json()
    check(st2["head_id"] == CTX["base"], "head must not move after failed commit")
    check(len(st2["relations"]) == 3, "no partial writes")

    # 6) 旧基线提交被拒绝（stale base）
    r = commit("main", [{"op": "relation_create", "type": "earlier", "a": B, "b": A}],
               "deadbeef" * 4)
    check(r.status_code == 409, r.text)

    # 7) 时间范围单项矛盾（lower>upper）
    r = new_commit([{"op": "event_create", "label": "D", "lower": 9, "upper": 3}])
    check(r.status_code == 422, r.text)
    d = r.json()["detail"]
    check(len(d["contradiction"]) == 1 and d["contradiction"][0]["kind"] == "event", d)
    st2 = client.get("/api/state").json()
    check(len(st2["events"]) == 3, "failed event create rolled back")

    # 8) at_most n=0 => 单项矛盾
    r = new_commit([{"op": "relation_create", "type": "at_most",
                     "a": A, "b": B, "n": 0}])
    check(r.status_code == 422, r.text)
    check(len(r.json()["detail"]["contradiction"]) == 1, "single-item at_most0")

    # 9) at_most 正常：A 晚于 B 至多 5 分钟（1<=t_A-t_B<=5），已有 >=2 => 2<=差<=5
    #    当前 B∈[5,6], A∈[7,10]，约束后 B∈[5,8?]... 直接验证可满足
    r = new_commit([{"op": "relation_create", "type": "at_most",
                     "a": A, "b": B, "n": 5}])
    check(r.status_code == 200, r.text)

    # 10) 无界显示：新建 E 只给下界
    r = new_commit([{"op": "event_create", "label": "E", "lower": 100}])
    check(r.status_code == 200, r.text)
    E = [e["id"] for e in r.json()["events"] if e["label"] == "E"][0]
    check(r.json()["analysis"]["bounds"][E] == {"lower": 100, "upper": None},
          r.json()["analysis"]["bounds"][E])

    # 11) 清空上界/下界（update + clear）
    r = new_commit([{"op": "event_update", "id": E, "clear": ["lower"]}])
    check(r.status_code == 200, r.text)
    ev = [e for e in r.json()["events"] if e["id"] == E][0]
    check(ev["lower"] is None, ev)
    check(r.json()["analysis"]["bounds"][E] == {"lower": None, "upper": None}, "free")

    # 12) 非法输入
    r = new_commit([{"op": "relation_create", "type": "at_least",
                     "a": A, "b": B, "n": -3}])
    check(r.status_code == 400, r.text)
    r = new_commit([{"op": "event_create", "label": ""}])
    check(r.status_code == 400, r.text)

    # ---- 分支与三方合并 ----
    # 在 main 上记录基线
    main_head = CTX["base"]
    r = client.post("/api/branches", json={"name": "feat", "from_branch": "main"})
    check(r.status_code == 200, r.text)
    feat_base = r.json()["head_id"]

    # feat：修改 A 名称与下界（字段级）
    r = commit("feat", [
        {"op": "event_update", "id": A, "label": "A改"},
        {"op": "event_update", "id": A, "lower": 4},
    ], feat_base)
    check(r.status_code == 200, r.text)
    feat_head = r.json()["head_id"]

    # main：改 A 上界（与 feat 无交叠）=> 自动合并
    r = commit("main", [{"op": "event_update", "id": A, "upper": 11}], main_head)
    check(r.status_code == 200, r.text)
    main_head = r.json()["head_id"]

    r = client.post("/api/branches/feat/merge-preview",
                    json={"source": "main", "resolutions": {}})
    check(r.status_code == 200, r.text)
    pv = r.json()
    check(pv["conflicts"] == [], pv["conflicts"])
    mergedA = [e for e in pv["merged_preview"]["events"] if e["id"] == A][0]
    check(mergedA["label"] == "A改" and mergedA["lower"] == 4 and mergedA["upper"] == 11,
          mergedA)
    check(pv["analysis"]["satisfiable"], True)

    r = client.post("/api/branches/feat/merge",
                    json={"source": "main", "resolutions": {}})
    check(r.status_code == 200, r.text)
    feat_head = r.json()["head_id"]
    mergedA = [e for e in r.json()["events"] if e["id"] == A][0]
    check(mergedA["upper"] == 11 and mergedA["lower"] == 4, mergedA)

    # 冲突合并：main 改 A.label 为 "A主"，feat 改为 "A分"
    r = commit("main", [{"op": "event_update", "id": A, "label": "A主"}], main_head)
    check(r.status_code == 200, r.text)
    main_head = r.json()["head_id"]
    # feat 再改 label
    r = commit("feat", [{"op": "event_update", "id": A, "label": "A分"}], feat_head)
    check(r.status_code == 200, r.text)
    feat_head = r.json()["head_id"]

    r = client.post("/api/branches/feat/merge-preview",
                    json={"source": "main", "resolutions": {}})
    check(r.status_code == 200, r.text)
    conflicts = r.json()["conflicts"]
    check(len(conflicts) == 1 and conflicts[0]["field"] == "label", conflicts)
    ckey = conflicts[0]["key"]
    check(conflicts[0]["ours"] == "A分" and conflicts[0]["theirs"] == "A主", conflicts)

    # 未裁决提交 => 409
    r = client.post("/api/branches/feat/merge",
                    json={"source": "main", "resolutions": {}})
    check(r.status_code == 409, r.text)
    # 选择 theirs
    r = client.post("/api/branches/feat/merge",
                    json={"source": "main", "resolutions": {ckey: "theirs"}})
    check(r.status_code == 200, r.text)
    mergedA = [e for e in r.json()["events"] if e["id"] == A][0]
    check(mergedA["label"] == "A主", mergedA)

    # 删除/修改冲突：main 删除 B；feat 修改 B
    r = commit("main", [{"op": "event_delete", "id": B}], main_head)
    check(r.status_code == 200, r.text)
    main_head = r.json()["head_id"]
    # feat 修改 B 上界
    r = commit("feat", [{"op": "event_update", "id": B, "upper": 50}],
               r.json()["head_id"] if False else client.get("/api/state?branch=feat").json()["head_id"])
    check(r.status_code == 200, r.text)
    feat_head = r.json()["head_id"]

    r = client.post("/api/branches/feat/merge-preview",
                    json={"source": "main", "resolutions": {}})
    check(r.status_code == 200, r.text)
    dc = [c for c in r.json()["conflicts"] if c["field"] == "__delete__"]
    check(len(dc) == 1, r.json()["conflicts"])
    dkey = dc[0]["key"]
    # ours=保留修改(feat), theirs=删除(main)
    r = client.post("/api/branches/feat/merge",
                    json={"source": "main", "resolutions": {dkey: "theirs"}})
    check(r.status_code == 200, r.text)
    check(all(e["id"] != B for e in r.json()["events"]), "B deleted after merge")
    # B 的关系应随级联在 main 删除时已删；合并后 feat 同样不应保留
    check(all(rel["a"] != B and rel["b"] != B for rel in r.json()["relations"]),
          "cascade delete")

    # 历史
    hist = client.get("/api/branches/feat/history").json()
    check(hist[0]["kind"] == "merge", hist[0])

    # 刷新持久化
    st = client.get("/api/state?branch=feat").json()
    check(len(st["events"]) >= 3, st)

    # ---- 性能：100 事件 500 条可满足关系 < 2s ----
    import random
    random.seed(7)
    truth = {f"X{i}": random.randint(0, 300) for i in range(100)}
    ops = []
    for name, tv in truth.items():
        ops.append({"op": "event_create", "label": name,
                    "lower": tv - 30, "upper": tv + 30})
    r = client.post("/api/branches", json={"name": "perf", "from_branch": "main"})
    perf_base = r.json()["head_id"]
    r = commit("perf", ops, perf_base)
    check(r.status_code == 200, r.text)
    idmap = {e["label"]: e["id"] for e in r.json()["events"]}
    rel_ops = []
    made = 0
    while made < 500:
        na, nb = random.sample(list(truth), 2)
        d = truth[na] - truth[nb]
        if d >= 2 and random.random() < 0.5:
            rel_ops.append({"op": "relation_create", "type": "at_least",
                            "a": idmap[na], "b": idmap[nb],
                            "n": random.randint(1, d)})
            made += 1
        elif d >= 1:
            rel_ops.append({"op": "relation_create", "type": "later",
                            "a": idmap[na], "b": idmap[nb]})
            made += 1
        elif d <= -1:
            rel_ops.append({"op": "relation_create", "type": "earlier",
                            "a": idmap[na], "b": idmap[nb]})
            made += 1
    t0 = time.time()
    r = commit("perf", rel_ops, r.json()["head_id"])
    dt = time.time() - t0
    check(r.status_code == 200, r.text)
    check(r.json()["analysis"]["satisfiable"], "perf instance must be sat")
    check(dt < 2.0, f"bound solve took {dt:.3f}s > 2s")
    # GET 性能同样测一下
    t0 = time.time()
    client.get("/api/state?branch=perf").json()
    dt2 = time.time() - t0
    check(dt2 < 2.0, f"state analyze took {dt2:.3f}s")
    print(f"\n性能：提交求解 {dt:.3f}s，GET 分析 {dt2:.3f}s")

print(f"\n✅ 全部 {PASS} 项端到端断言通过")
