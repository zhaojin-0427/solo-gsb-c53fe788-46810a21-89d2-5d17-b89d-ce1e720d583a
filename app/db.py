"""SQLite 持久化层。

数据模型：
  meta              全局单行：全局创建序号计数
  branches          分支（main 为初始分支；fork_from/fork_point 记录分叉）
  commits           提交 DAG；root 提交为各分叉基点
  entities          每个提交保存一份完整实体快照（含墓碑）

实体（事件的时间范围与每条关系各为一个输入项，各自持有全局创建序号）：
  event    : {id, seq, label, lower, upper, deleted}
  relation : {id, seq, type, a, b, n, deleted}
             type ∈ earlier | later | same | at_least | at_most
             方向以 a 相对 b 描述（a 早于/晚于 b，a 晚于 b 至少/至多 n 分钟）
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "app.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    seq_counter INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO meta (id, seq_counter) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS branches (
    name TEXT PRIMARY KEY,
    head_id TEXT NOT NULL,
    fork_from TEXT,
    fork_point TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commits (
    id TEXT PRIMARY KEY,
    parent_id TEXT,
    branch_name TEXT NOT NULL,
    message TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'commit',   -- commit | merge
    merge_source TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entities (
    commit_id TEXT NOT NULL,
    kind TEXT NOT NULL,                    -- event | relation
    data TEXT NOT NULL,
    PRIMARY KEY (commit_id, kind, data)
);

CREATE INDEX IF NOT EXISTS idx_entities_commit ON entities(commit_id);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex


def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    conn = get_db()
    try:
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT name FROM branches WHERE name='main'").fetchone()
        if row is None:
            ts = now_iso()
            cid = new_id()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO commits (id, parent_id, branch_name, message, kind, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (cid, None, "main", "初始提交", "commit", ts),
            )
            conn.execute(
                "INSERT INTO branches (name, head_id, fork_from, fork_point, created_at)"
                " VALUES ('main', ?, NULL, NULL, ?)",
                (cid, ts),
            )
            conn.execute("COMMIT")
    finally:
        conn.close()


# ---------------------------------------------------------------------------

def next_seq(conn: sqlite3.Connection) -> int:
    conn.execute("UPDATE meta SET seq_counter = seq_counter + 1 WHERE id = 1")
    row = conn.execute("SELECT seq_counter FROM meta WHERE id = 1").fetchone()
    return row["seq_counter"]


def load_state(conn: sqlite3.Connection, commit_id: str) -> dict:
    """加载某提交的完整状态（含墓碑集合）。"""
    events: dict[str, dict] = {}
    relations: dict[str, dict] = {}
    for kind, table in (("event", events), ("relation", relations)):
        rows = conn.execute(
            "SELECT data FROM entities WHERE commit_id=? AND kind=?",
            (commit_id, kind),
        ).fetchall()
        for r in rows:
            d = json.loads(r["data"])
            table[d["id"]] = d
    return {"events": events, "relations": relations}


def clone_state(state: dict) -> dict:
    return {
        "events": {k: dict(v) for k, v in state["events"].items()},
        "relations": {k: dict(v) for k, v in state["relations"].items()},
    }


def live(state: dict) -> tuple[list[dict], list[dict]]:
    evs = sorted((e for e in state["events"].values() if not e.get("deleted")),
                 key=lambda e: e["seq"])
    rels = sorted((r for r in state["relations"].values() if not r.get("deleted")),
                  key=lambda r: r["seq"])
    return evs, rels


def write_snapshot(conn: sqlite3.Connection, commit_id: str, state: dict) -> None:
    rows = []
    for d in state["events"].values():
        rows.append((commit_id, "event", json.dumps(d, ensure_ascii=False, sort_keys=True)))
    for d in state["relations"].values():
        rows.append((commit_id, "relation", json.dumps(d, ensure_ascii=False, sort_keys=True)))
    conn.executemany(
        "INSERT INTO entities (commit_id, kind, data) VALUES (?,?,?)", rows
    )


def create_commit(
    conn: sqlite3.Connection,
    branch_name: str,
    parent_id: str,
    state: dict,
    message: str,
    kind: str = "commit",
    merge_source: str | None = None,
) -> str:
    cid = new_id()
    ts = now_iso()
    conn.execute(
        "INSERT INTO commits (id, parent_id, branch_name, message, kind, merge_source, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (cid, parent_id, branch_name, message, kind, merge_source, ts),
    )
    write_snapshot(conn, cid, state)
    conn.execute("UPDATE branches SET head_id=? WHERE name=?", (cid, branch_name))
    return cid


def get_branch(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM branches WHERE name=?", (name,)).fetchone()


def list_branches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM branches ORDER BY (name='main') DESC, name"
    ).fetchall()


def list_commits(conn: sqlite3.Connection, name: str) -> list[sqlite3.Row]:
    """沿 first-parent 链列出分支历史。"""
    branch = get_branch(conn, name)
    if branch is None:
        return []
    out: list[sqlite3.Row] = []
    cur = branch["head_id"]
    seen: set[str] = set()
    while cur and cur not in seen:
        seen.add(cur)
        row = conn.execute("SELECT * FROM commits WHERE id=?", (cur,)).fetchone()
        if row is None:
            break
        out.append(row)
        cur = row["parent_id"]
    return out


def all_commit_ids(conn: sqlite3.Connection) -> set[str]:
    return {r["id"] for r in conn.execute("SELECT id FROM commits").fetchall()}


def find_lca(conn: sqlite3.Connection, head_a: str, head_b: str) -> str | None:
    """提交 DAG 上的最近公共祖先（BFS 收集 a 的祖先集，再沿 b 上行）。"""
    ancestors: set[str] = set()
    queue = [head_a]
    while queue:
        cid = queue.pop()
        if cid in ancestors:
            continue
        ancestors.add(cid)
        row = conn.execute("SELECT parent_id FROM commits WHERE id=?", (cid,)).fetchone()
        if row and row["parent_id"]:
            queue.append(row["parent_id"])
    cur = head_b
    seen: set[str] = set()
    while cur and cur not in seen:
        if cur in ancestors:
            return cur
        seen.add(cur)
        row = conn.execute("SELECT parent_id FROM commits WHERE id=?", (cur,)).fetchone()
        if not row:
            return None
        cur = row["parent_id"]
    return None
