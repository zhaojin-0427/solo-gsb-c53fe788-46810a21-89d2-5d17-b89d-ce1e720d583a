"""SQLite 持久化层。

所有变更在单连接单事务内完成；提交（commit）在同一事务内复核基线版本
与可满足性，任何一步失败整体回滚，不会部分写入。
"""
import json
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("DATABASE_PATH", "/data/tconsole.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS branches (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    head_id     INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS versions (
    id          INTEGER PRIMARY KEY,
    branch_id   INTEGER NOT NULL REFERENCES branches(id),
    parent_id   INTEGER REFERENCES versions(id),
    kind        TEXT NOT NULL,                 -- commit | initial | merge
    message     TEXT NOT NULL DEFAULT '',
    base_head_id INTEGER,                      -- 分叉基线版本（合并提交用）
    source_branch_id INTEGER,                  -- 来源分支（合并提交用）
    snapshot    TEXT NOT NULL,                 -- 完整状态快照 JSON
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS events (
    branch_id    INTEGER NOT NULL REFERENCES branches(id),
    ref          INTEGER NOT NULL,             -- 全局创建序号（输入项序号）
    name         TEXT NOT NULL,
    lower_bound  INTEGER,
    upper_bound  INTEGER,
    PRIMARY KEY (branch_id, ref)
);

CREATE TABLE IF NOT EXISTS relations (
    branch_id    INTEGER NOT NULL REFERENCES branches(id),
    ref          INTEGER NOT NULL,             -- 全局创建序号（输入项序号）
    a_ref        INTEGER NOT NULL,
    b_ref        INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    n            INTEGER,
    PRIMARY KEY (branch_id, ref)
);

CREATE TABLE IF NOT EXISTS seq_counter (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    next_value INTEGER NOT NULL
);
"""


def get_conn():
    path = DB_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def transaction(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def init_db(conn):
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT id FROM branches WHERE id = 1").fetchone()
    if row is None:
        _seed(conn)


def alloc_ref(conn):
    """分配全局单调递增的创建序号（事件、关系共用同一计数器）。"""
    conn.execute(
        "INSERT INTO seq_counter(id, next_value) VALUES(1, 2) "
        "ON CONFLICT(id) DO UPDATE SET next_value = next_value + 1"
    )
    return conn.execute("SELECT next_value - 1 FROM seq_counter WHERE id = 1").fetchone()[0]


def _seed(conn):
    """初始演示数据：一次小型发布会筹备时序（可满足）。"""
    conn.execute("INSERT INTO branches(id, name, head_id) VALUES(1, 'main', NULL)")
    conn.execute("INSERT INTO seq_counter(id, next_value) VALUES(1, 9)")
    events = [
        (1, "需求定稿", 0, 10),
        (2, "开发完成", None, None),
        (3, "测试通过", None, None),
        (4, "正式发布", 20, None),
    ]
    relations = [
        (5, 1, 2, "earlier", None),       # 需求定稿 早于 开发完成
        (6, 2, 3, "earlier", None),       # 开发完成 早于 测试通过
        (7, 3, 4, "earlier", None),       # 测试通过 早于 正式发布
        (8, 4, 3, "after_at_most", 30),   # 正式发布 晚于 测试通过 至多 30 分钟
    ]
    for ref, name, lb, ub in events:
        conn.execute(
            "INSERT INTO events(branch_id, ref, name, lower_bound, upper_bound) "
            "VALUES(1, ?, ?, ?, ?)",
            (ref, name, lb, ub),
        )
    for ref, a, b, kind, n in relations:
        conn.execute(
            "INSERT INTO relations(branch_id, ref, a_ref, b_ref, kind, n) "
            "VALUES(1, ?, ?, ?, ?, ?)",
            (ref, a, b, kind, n),
        )
    snap = _snapshot(conn, 1)
    cur = conn.execute(
        "INSERT INTO versions(branch_id, parent_id, kind, message, snapshot) "
        "VALUES(1, NULL, 'initial', ?, ?)",
        ("初始演示数据", json.dumps(snap, ensure_ascii=False)),
    )
    conn.execute("UPDATE branches SET head_id = ? WHERE id = 1", (cur.lastrowid,))


def _snapshot(conn, branch_id):
    events = [
        {"ref": r["ref"], "name": r["name"],
         "lower_bound": r["lower_bound"], "upper_bound": r["upper_bound"]}
        for r in conn.execute(
            "SELECT ref, name, lower_bound, upper_bound FROM events "
            "WHERE branch_id = ? ORDER BY ref", (branch_id,))
    ]
    relations = [
        {"ref": r["ref"], "a_ref": r["a_ref"], "b_ref": r["b_ref"],
         "kind": r["kind"], "n": r["n"]}
        for r in conn.execute(
            "SELECT ref, a_ref, b_ref, kind, n FROM relations "
            "WHERE branch_id = ? ORDER BY ref", (branch_id,))
    ]
    return {"events": events, "relations": relations}


def snapshot_branch(conn, branch_id):
    return _snapshot(conn, branch_id)


def list_branches(conn):
    return [dict(r) for r in conn.execute(
        "SELECT b.id, b.name, b.head_id, "
        "(SELECT created_at FROM versions v WHERE v.id = b.head_id) AS head_at "
        "FROM branches b ORDER BY b.id")]


def get_branch(conn, branch_id):
    row = conn.execute("SELECT * FROM branches WHERE id = ?", (branch_id,)).fetchone()
    return dict(row) if row else None


def list_versions(conn, branch_id):
    return [dict(r) for r in conn.execute(
        "SELECT id, parent_id, kind, message, base_head_id, source_branch_id, created_at "
        "FROM versions WHERE branch_id = ? ORDER BY id", (branch_id,))]


def get_version(conn, version_id):
    row = conn.execute("SELECT * FROM versions WHERE id = ?", (version_id,)).fetchone()
    return dict(row) if row else None


def load_snapshot(conn, version_id):
    row = conn.execute("SELECT snapshot FROM versions WHERE id = ?",
                       (version_id,)).fetchone()
    return json.loads(row["snapshot"]) if row else None


def replace_workspace(conn, branch_id, state):
    """用给定状态整体覆盖某分支工作区（分叉/合并使用，保留原 ref）。"""
    conn.execute("DELETE FROM events WHERE branch_id = ?", (branch_id,))
    conn.execute("DELETE FROM relations WHERE branch_id = ?", (branch_id,))
    for ev in state["events"]:
        conn.execute(
            "INSERT INTO events(branch_id, ref, name, lower_bound, upper_bound) "
            "VALUES(?, ?, ?, ?, ?)",
            (branch_id, ev["ref"], ev["name"],
             ev.get("lower_bound"), ev.get("upper_bound")),
        )
    for rel in state["relations"]:
        conn.execute(
            "INSERT INTO relations(branch_id, ref, a_ref, b_ref, kind, n) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (branch_id, rel["ref"], rel["a_ref"], rel["b_ref"],
             rel["kind"], rel.get("n")),
        )


def insert_version(conn, branch_id, kind, message, snapshot,
                   parent_id=None, base_head_id=None, source_branch_id=None):
    cur = conn.execute(
        "INSERT INTO versions(branch_id, parent_id, kind, message, base_head_id, "
        "source_branch_id, snapshot) VALUES(?, ?, ?, ?, ?, ?, ?)",
        (branch_id, parent_id, kind, message, base_head_id, source_branch_id,
         json.dumps(snapshot, ensure_ascii=False)),
    )
    new_id = cur.lastrowid
    conn.execute("UPDATE branches SET head_id = ? WHERE id = ?",
                 (new_id, branch_id))
    return new_id
