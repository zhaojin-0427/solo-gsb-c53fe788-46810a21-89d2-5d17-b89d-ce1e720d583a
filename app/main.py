"""事件时序矛盾排查台 — FastAPI 入口。"""
import os
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, solver
from .merge import analyze_merge, apply_resolutions

app = FastAPI(title="事件时序矛盾排查台")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def conn():
    c = db.get_conn()
    db.init_db(c)
    return c


@app.on_event("startup")
def _startup():
    c = conn()
    c.close()


# ---------------------------------------------------------------- 数据装配

def _events(conn, branch_id):
    return [
        {"ref": r["ref"], "name": r["name"],
         "lower_bound": r["lower_bound"], "upper_bound": r["upper_bound"]}
        for r in conn.execute(
            "SELECT ref, name, lower_bound, upper_bound FROM events "
            "WHERE branch_id = ? ORDER BY ref", (branch_id,))
    ]


def _relations(conn, branch_id):
    return [
        {"ref": r["ref"], "a_ref": r["a_ref"], "b_ref": r["b_ref"],
         "kind": r["kind"], "n": r["n"]}
        for r in conn.execute(
            "SELECT ref, a_ref, b_ref, kind, n FROM relations "
            "WHERE branch_id = ? ORDER BY ref", (branch_id,))
    ]


def _get_branch_or_404(conn, branch_id):
    branch = db.get_branch(conn, branch_id)
    if branch is None:
        raise HTTPException(404, f"分支 {branch_id} 不存在")
    return branch


def _state_payload(conn, branch_id):
    branch = _get_branch_or_404(conn, branch_id)
    events = _events(conn, branch_id)
    relations = _relations(conn, branch_id)
    result = solver.solve(events, relations)
    return {"branch": {k: branch[k] for k in ("id", "name", "head_id")},
            "events": events, "relations": relations, "solve": result}


# ---------------------------------------------------------------- Pydantic

class EventIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    lower_bound: Optional[int] = None
    upper_bound: Optional[int] = None


class EventPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    lower_bound: Optional[int] = None
    upper_bound: Optional[int] = None


class RelationIn(BaseModel):
    a_ref: int
    b_ref: int
    kind: str
    n: Optional[int] = None


class RelationPatch(BaseModel):
    a_ref: Optional[int] = None
    b_ref: Optional[int] = None
    kind: Optional[str] = None
    n: Optional[int] = None


class BranchIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    from_version: Optional[int] = None  # 缺省从 main 最新版本分叉


class CommitIn(BaseModel):
    message: str = Field(default="", max_length=200)


class MergePreviewIn(BaseModel):
    source_id: int
    expected_head: Optional[int] = None


class MergeCommitIn(BaseModel):
    source_id: int
    expected_head: int
    message: str = Field(default="", max_length=200)
    resolutions: dict = Field(default_factory=dict)


# ---------------------------------------------------------------- 校验

def _validate_bounds(lb, ub):
    if lb is not None and ub is not None and lb > ub:
        # 允许（合法输入但构成矛盾），求解器会报告该范围项。
        pass


def _validate_relation(conn, branch_id, a_ref, b_ref, kind, n):
    if a_ref == b_ref:
        raise HTTPException(400, "关系的两个事件必须不同")
    if kind not in solver.REL_KINDS:
        raise HTTPException(400, f"未知关系类型 {kind!r}")
    if kind in solver.REL_DIRECTIONAL_KINDS:
        if n is None or isinstance(n, bool) or n < 0:
            raise HTTPException(400, "间隔约束的 n 必须是非负整数")
    else:
        n = None
    for ref in (a_ref, b_ref):
        row = conn.execute(
            "SELECT 1 FROM events WHERE branch_id = ? AND ref = ?",
            (branch_id, ref)).fetchone()
        if row is None:
            raise HTTPException(400, f"事件 {ref} 不存在")
    return n


# ---------------------------------------------------------------- 分支/版本

@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/branches")
def get_branches():
    c = conn()
    try:
        return db.list_branches(c)
    finally:
        c.close()


@app.post("/api/branches", status_code=201)
def create_branch(body: BranchIn):
    c = conn()
    try:
        with db.transaction(c):
            if c.execute("SELECT 1 FROM branches WHERE name = ?",
                         (body.name,)).fetchone():
                raise HTTPException(409, f"分支名 {body.name!r} 已存在")
            if body.from_version is not None:
                base = db.get_version(c, body.from_version)
                if base is None:
                    raise HTTPException(404, "基线版本不存在")
                state = db.load_snapshot(c, body.from_version)
            else:
                main = db.get_branch(c, 1)
                state = db.load_snapshot(c, main["head_id"])
                base = db.get_version(c, main["head_id"])
            cur = c.execute(
                "INSERT INTO branches(name, head_id) VALUES(?, NULL)",
                (body.name,))
            new_id = cur.lastrowid
            db.replace_workspace(c, new_id, state)
            # 分叉点版本（kind=fork），其父版本为基线；后续三方合并以它为分叉版本
            vid = db.insert_version(c, new_id, "fork",
                                    f"自版本 #{base['id']} 分叉", state,
                                    parent_id=base["id"])
        return _state_payload(c, new_id)
    finally:
        c.close()


@app.get("/api/branches/{branch_id}/state")
def get_state(branch_id: int):
    c = conn()
    try:
        return _state_payload(c, branch_id)
    finally:
        c.close()


@app.post("/api/branches/{branch_id}/commit")
def commit(branch_id: int, body: CommitIn):
    c = conn()
    try:
        with db.transaction(c):
            branch = _get_branch_or_404(c, branch_id)
            state = db.snapshot_branch(c, branch_id)
            # 普通提交允许保存不可满足状态（排查过程本身需要留痕）
            vid = db.insert_version(
                c, branch_id, "commit", body.message or "提交", state,
                parent_id=branch["head_id"])
        return {"version_id": vid}
    finally:
        c.close()


@app.get("/api/branches/{branch_id}/versions")
def get_versions(branch_id: int):
    c = conn()
    try:
        _get_branch_or_404(c, branch_id)
        return db.list_versions(c, branch_id)
    finally:
        c.close()


# ---------------------------------------------------------------- 事件

@app.post("/api/branches/{branch_id}/events", status_code=201)
def create_event(branch_id: int, body: EventIn):
    c = conn()
    try:
        with db.transaction(c):
            _get_branch_or_404(c, branch_id)
            _validate_bounds(body.lower_bound, body.upper_bound)
            ref = db.alloc_ref(c)
            c.execute(
                "INSERT INTO events(branch_id, ref, name, lower_bound, upper_bound) "
                "VALUES(?, ?, ?, ?, ?)",
                (branch_id, ref, body.name, body.lower_bound, body.upper_bound))
        return _state_payload(c, branch_id)
    finally:
        c.close()


@app.patch("/api/branches/{branch_id}/events/{ref}")
def patch_event(branch_id: int, ref: int, body: EventPatch):
    c = conn()
    try:
        with db.transaction(c):
            _get_branch_or_404(c, branch_id)
            row = c.execute(
                "SELECT name, lower_bound, upper_bound FROM events "
                "WHERE branch_id = ? AND ref = ?", (branch_id, ref)).fetchone()
            if row is None:
                raise HTTPException(404, f"事件 {ref} 不存在")
            values = {"name": row["name"],
                      "lower_bound": row["lower_bound"],
                      "upper_bound": row["upper_bound"]}
            for field in ("name", "lower_bound", "upper_bound"):
                if field in body.model_fields_set:
                    values[field] = getattr(body, field)
            if not values["name"]:
                raise HTTPException(400, "事件名称不能为空")
            _validate_bounds(values["lower_bound"], values["upper_bound"])
            c.execute(
                "UPDATE events SET name=?, lower_bound=?, upper_bound=? "
                "WHERE branch_id=? AND ref=?",
                (values["name"], values["lower_bound"], values["upper_bound"],
                 branch_id, ref))
        return _state_payload(c, branch_id)
    finally:
        c.close()


@app.delete("/api/branches/{branch_id}/events/{ref}")
def delete_event(branch_id: int, ref: int):
    c = conn()
    try:
        with db.transaction(c):
            _get_branch_or_404(c, branch_id)
            row = c.execute(
                "SELECT 1 FROM events WHERE branch_id=? AND ref=?",
                (branch_id, ref)).fetchone()
            if row is None:
                raise HTTPException(404, f"事件 {ref} 不存在")
            # 连带删除引用该事件的关系（同一事务）
            c.execute("DELETE FROM relations WHERE branch_id=? AND (a_ref=? OR b_ref=?)",
                      (branch_id, ref, ref))
            c.execute("DELETE FROM events WHERE branch_id=? AND ref=?",
                      (branch_id, ref))
        return _state_payload(c, branch_id)
    finally:
        c.close()


# ---------------------------------------------------------------- 关系

@app.post("/api/branches/{branch_id}/relations", status_code=201)
def create_relation(branch_id: int, body: RelationIn):
    c = conn()
    try:
        with db.transaction(c):
            _get_branch_or_404(c, branch_id)
            n = _validate_relation(c, branch_id, body.a_ref, body.b_ref,
                                   body.kind, body.n)
            ref = db.alloc_ref(c)
            c.execute(
                "INSERT INTO relations(branch_id, ref, a_ref, b_ref, kind, n) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (branch_id, ref, body.a_ref, body.b_ref, body.kind, n))
        return _state_payload(c, branch_id)
    finally:
        c.close()


@app.patch("/api/branches/{branch_id}/relations/{ref}")
def patch_relation(branch_id: int, ref: int, body: RelationPatch):
    c = conn()
    try:
        with db.transaction(c):
            _get_branch_or_404(c, branch_id)
            row = c.execute(
                "SELECT a_ref, b_ref, kind, n FROM relations "
                "WHERE branch_id=? AND ref=?", (branch_id, ref)).fetchone()
            if row is None:
                raise HTTPException(404, f"关系 {ref} 不存在")
            values = {"a_ref": row["a_ref"], "b_ref": row["b_ref"],
                      "kind": row["kind"], "n": row["n"]}
            for field in ("a_ref", "b_ref", "kind", "n"):
                if field in body.model_fields_set:
                    values[field] = getattr(body, field)
            values["n"] = _validate_relation(
                c, branch_id, values["a_ref"], values["b_ref"],
                values["kind"], values["n"])
            c.execute(
                "UPDATE relations SET a_ref=?, b_ref=?, kind=?, n=? "
                "WHERE branch_id=? AND ref=?",
                (values["a_ref"], values["b_ref"], values["kind"], values["n"],
                 branch_id, ref))
        return _state_payload(c, branch_id)
    finally:
        c.close()


@app.delete("/api/branches/{branch_id}/relations/{ref}")
def delete_relation(branch_id: int, ref: int):
    c = conn()
    try:
        with db.transaction(c):
            _get_branch_or_404(c, branch_id)
            cur = c.execute("DELETE FROM relations WHERE branch_id=? AND ref=?",
                            (branch_id, ref))
            if cur.rowcount == 0:
                raise HTTPException(404, f"关系 {ref} 不存在")
        return _state_payload(c, branch_id)
    finally:
        c.close()


# ---------------------------------------------------------------- 合并

def _fork_base_version(c, target_id, source_id):
    """求两个分支的分叉基线版本（merge-base）。

    分叉分支创建时记录了 kind='fork' 版本，其父版本即分叉点；
    无论合并方向如何，任一方存在该记录都以其父版本为基线。
    否则退化为 main 的 initial 版本。
    """
    for bid in (source_id, target_id):
        row = c.execute(
            "SELECT parent_id FROM versions WHERE branch_id=? AND kind='fork' "
            "AND parent_id IS NOT NULL ORDER BY id LIMIT 1", (bid,)).fetchone()
        if row and row["parent_id"]:
            return db.get_version(c, row["parent_id"])
    row = c.execute(
        "SELECT id FROM versions WHERE branch_id=1 AND kind='initial' "
        "ORDER BY id LIMIT 1").fetchone()
    return db.get_version(c, row["id"]) if row else None


def _do_merge_analysis(c, target_id, source_id, expected_head=None):
    target = _get_branch_or_404(c, target_id)
    source = _get_branch_or_404(c, source_id)
    if target_id == source_id:
        raise HTTPException(400, "目标分支与来源分支相同")
    if expected_head is not None and target["head_id"] != expected_head:
        raise HTTPException(409, "目标分支已被其他提交更新，请刷新后重试合并")
    base_ver = _fork_base_version(c, target_id, source_id)
    if base_ver is None:
        raise HTTPException(400, "无法确定分叉基线版本")
    base = db.load_snapshot(c, base_ver["id"])
    mine = db.snapshot_branch(c, target_id)
    theirs = db.load_snapshot(c, source["head_id"])
    analysis = analyze_merge(base, mine, theirs)
    return target, source, base_ver, analysis


@app.post("/api/branches/{branch_id}/merges/preview")
def merge_preview(branch_id: int, body: MergePreviewIn):
    c = conn()
    try:
        _t, _s, base_ver, analysis = _do_merge_analysis(
            c, branch_id, body.source_id, body.expected_head)
        return {
            "base_version_id": base_ver["id"],
            "auto": analysis["state"],
            "conflicts": analysis["conflicts"],
            "dropped": analysis["dropped"],
        }
    finally:
        c.close()


@app.post("/api/branches/{branch_id}/merges/commit")
def merge_commit(branch_id: int, body: MergeCommitIn):
    c = conn()
    try:
        with db.transaction(c):
            # 同一事务内：复核基线版本 + 三方合并 + 可满足性复核 + 落库
            target, source, base_ver, analysis = _do_merge_analysis(
                c, branch_id, body.source_id, body.expected_head)
            try:
                merged = apply_resolutions(analysis, body.resolutions)
            except ValueError as e:
                raise HTTPException(409, f"仍有冲突未解决：{e}")

            result = solver.solve(merged["events"], merged["relations"])
            if not result["feasible"]:
                raise HTTPException(
                    409,
                    f"合并结果不可满足，矛盾集输入项序号：{result['core']}")

            db.replace_workspace(c, branch_id, merged)
            vid = db.insert_version(
                c, branch_id, "merge",
                body.message or f"合并分支 {source['name']}",
                merged, parent_id=target["head_id"],
                base_head_id=base_ver["id"], source_branch_id=source["id"])
        return _state_payload(c, branch_id)
    finally:
        c.close()


# ---------------------------------------------------------------- 静态页面

@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
