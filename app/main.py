"""事件时序矛盾排查台 — FastAPI 入口。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, merge as merge_mod
from .model import (
    ConflictError,
    ValidationError,
    analyze_state,
    apply_operations,
)

app = FastAPI(title="事件时序矛盾排查台", version="1.0.0")


def _rollback(conn) -> None:
    """幂等回滚：仅当存在活动事务时执行。"""
    if conn.in_transaction:
        conn.execute("ROLLBACK")


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------

def serialize_branch(row) -> dict:
    return {
        "name": row["name"],
        "head_id": row["head_id"],
        "fork_from": row["fork_from"],
        "fork_point": row["fork_point"],
        "created_at": row["created_at"],
    }


def build_state_payload(conn, name: str) -> dict:
    branch = db.get_branch(conn, name)
    if branch is None:
        raise HTTPException(404, f"分支不存在: {name}")
    state = db.load_state(conn, branch["head_id"])
    counter = conn.execute(
        "SELECT seq_counter FROM meta WHERE id=1"
    ).fetchone()["seq_counter"]
    analysis = analyze_state(state, counter)
    events, relations = db.live(state)
    return {
        "branch": name,
        "head_id": branch["head_id"],
        "seq_counter": counter,
        "events": events,
        "relations": relations,
        "analysis": analysis,
    }


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class CommitIn(BaseModel):
    base_id: str
    message: str = ""
    ops: list[dict] = Field(default_factory=list)


class BranchIn(BaseModel):
    name: str
    from_branch: str = "main"
    from_commit: str | None = None


class MergeIn(BaseModel):
    source: str
    resolutions: dict[str, str] = Field(default_factory=dict)
    message: str = ""


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/api/state")
def get_state(branch: str = "main"):
    conn = db.get_db()
    try:
        return build_state_payload(conn, branch)
    finally:
        conn.close()


@app.get("/api/branches")
def get_branches():
    conn = db.get_db()
    try:
        return [serialize_branch(r) for r in db.list_branches(conn)]
    finally:
        conn.close()


@app.post("/api/branches")
def create_branch(body: BranchIn):
    name = body.name.strip()
    if not name or name in {"main"} or "/" in name:
        raise HTTPException(400, "分支名不合法")
    conn = db.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if db.get_branch(conn, name) is not None:
            _rollback(conn)
            raise HTTPException(409, f"分支已存在: {name}")
        src = db.get_branch(conn, body.from_branch)
        if src is None:
            _rollback(conn)
            raise HTTPException(404, f"源分支不存在: {body.from_branch}")
        point = body.from_commit or src["head_id"]
        if point not in db.all_commit_ids(conn):
            _rollback(conn)
            raise HTTPException(400, "指定的分叉提交不存在")
        ts = db.now_iso()
        conn.execute(
            "INSERT INTO branches (name, head_id, fork_from, fork_point, created_at)"
            " VALUES (?,?,?,?,?)",
            (name, point, body.from_branch, point, ts),
        )
        conn.execute("COMMIT")
        return {"ok": True, "name": name, "head_id": point}
    except HTTPException:
        raise
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()


@app.get("/api/branches/{name}/history")
def get_history(name: str):
    conn = db.get_db()
    try:
        if db.get_branch(conn, name) is None:
            raise HTTPException(404, "分支不存在")
        out = []
        for r in db.list_commits(conn, name):
            out.append({
                "id": r["id"], "parent_id": r["parent_id"],
                "message": r["message"], "kind": r["kind"],
                "merge_source": r["merge_source"], "created_at": r["created_at"],
            })
        return out
    finally:
        conn.close()


@app.post("/api/branches/{name}/commit")
def commit_changes(name: str, body: CommitIn):
    conn = db.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        branch = db.get_branch(conn, name)
        if branch is None:
            _rollback(conn)
            raise HTTPException(404, "分支不存在")
        if branch["head_id"] != body.base_id:
            _rollback(conn)
            raise HTTPException(
                409,
                detail={"code": "stale_base",
                        "message": "基线版本已变化，请刷新后基于最新版本重试",
                        "head_id": branch["head_id"]},
            )
        state = db.load_state(conn, body.base_id)
        try:
            apply_operations(state, body.ops, lambda: db.next_seq(conn))
        except ValidationError as e:
            _rollback(conn)
            raise HTTPException(400, str(e))
        counter = conn.execute(
            "SELECT seq_counter FROM meta WHERE id=1"
        ).fetchone()["seq_counter"]
        analysis = analyze_state(state, counter)
        if not analysis["satisfiable"]:
            _rollback(conn)
            raise HTTPException(422, detail={
                "code": "unsatisfiable", "message": "提交后约束不可满足",
                "contradiction": analysis["contradiction"],
            })
        cid = db.create_commit(
            conn, name, body.base_id, state,
            body.message or "编辑",
        )
        conn.execute("COMMIT")
        return build_state_payload(conn, name)
    except HTTPException:
        raise
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()


def _merge_states(conn, source: str, ours_branch_name: str, resolutions: dict):
    ours_branch = db.get_branch(conn, ours_branch_name)
    src_branch = db.get_branch(conn, source)
    if ours_branch is None or src_branch is None:
        raise HTTPException(404, "分支不存在")
    if source == ours_branch_name:
        raise HTTPException(400, "不能合并当前分支自身")
    our_head = ours_branch["head_id"]
    src_head = src_branch["head_id"]
    base_id = db.find_lca(conn, our_head, src_head)
    if base_id is None:
        raise HTTPException(400, "两个分支没有公共祖先")
    base = db.load_state(conn, base_id)
    ours = db.load_state(conn, our_head)
    theirs = db.load_state(conn, src_head)
    merged, conflicts = merge_mod.three_way_merge(base, ours, theirs, resolutions)
    return base_id, base, ours, theirs, merged, conflicts


@app.post("/api/branches/{name}/merge-preview")
def merge_preview(name: str, body: MergeIn):
    conn = db.get_db()
    try:
        _base_id, base, _ours, _theirs, merged, conflicts = _merge_states(
            conn, body.source, name, body.resolutions)
        changes = merge_mod.auto_changes(base, merged)
        counter = conn.execute(
            "SELECT seq_counter FROM meta WHERE id=1"
        ).fetchone()["seq_counter"]
        analysis = analyze_state(merged, counter)
        return {
            "conflicts": conflicts,
            "auto_merged": changes,
            "merged_preview": {
                "events": sorted(
                    (e for e in merged["events"].values() if not e.get("deleted")),
                    key=lambda e: e["seq"]),
                "relations": sorted(
                    (r for r in merged["relations"].values() if not r.get("deleted")),
                    key=lambda r: r["seq"]),
            },
            "analysis": analysis,
        }
    finally:
        conn.close()


@app.post("/api/branches/{name}/merge")
def merge_branch(name: str, body: MergeIn):
    conn = db.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ours_branch = db.get_branch(conn, name)
        if ours_branch is None:
            _rollback(conn)
            raise HTTPException(404, "分支不存在")
        base_id, _base, _ours, _theirs, merged, conflicts = _merge_states(
            conn, body.source, name, body.resolutions)
        unresolved = [c for c in conflicts if c.get("resolution") is None]
        if unresolved:
            _rollback(conn)
            raise HTTPException(409, detail={
                "code": "unresolved_conflicts",
                "message": "仍有未解决的合并冲突", "conflicts": unresolved,
            })
        counter = conn.execute(
            "SELECT seq_counter FROM meta WHERE id=1"
        ).fetchone()["seq_counter"]
        analysis = analyze_state(merged, counter)
        if not analysis["satisfiable"]:
            _rollback(conn)
            raise HTTPException(422, detail={
                "code": "unsatisfiable", "message": "合并结果不可满足",
                "contradiction": analysis["contradiction"],
            })
        cid = db.create_commit(
            conn, name, ours_branch["head_id"], merged,
            body.message or f"合并 {body.source}",
            kind="merge", merge_source=body.source,
        )
        conn.execute("COMMIT")
        return build_state_payload(conn, name)
    except HTTPException:
        raise
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 静态页面
# ---------------------------------------------------------------------------

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
