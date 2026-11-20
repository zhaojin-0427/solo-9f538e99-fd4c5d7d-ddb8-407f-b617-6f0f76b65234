"""SQLite 存储层：schema 初始化与仓储函数。

派生结论（分组、可行年份）一律不落库，每次查询都由原始记录现场求解，
因此“每次查询及重启后，分组和年代均来自同一次已完成的提交”由原始记录的
事务原子性天然保证。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sites (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL UNIQUE,
    created TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS strata (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id  INTEGER NOT NULL REFERENCES sites(id),
    trench   TEXT NOT NULL,
    layer_no TEXT NOT NULL,
    note     TEXT,
    created  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (site_id, trench, layer_no)
);

CREATE TABLE IF NOT EXISTS stratum_intervals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    stratum_id INTEGER NOT NULL REFERENCES strata(id) ON DELETE CASCADE,
    year_from  INTEGER NOT NULL,
    year_to    INTEGER NOT NULL,
    CHECK (year_from <= year_to)
);

CREATE TABLE IF NOT EXISTS correlations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id      INTEGER NOT NULL REFERENCES sites(id),
    stratum_a_id INTEGER NOT NULL REFERENCES strata(id),
    stratum_b_id INTEGER NOT NULL REFERENCES strata(id),
    created      TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (stratum_a_id < stratum_b_id),
    UNIQUE (site_id, stratum_a_id, stratum_b_id)
);

CREATE TABLE IF NOT EXISTS superpositions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id      INTEGER NOT NULL REFERENCES sites(id),
    lower_id     INTEGER NOT NULL REFERENCES strata(id),
    upper_id     INTEGER NOT NULL REFERENCES strata(id),
    created      TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (lower_id <> upper_id),
    UNIQUE (site_id, lower_id, upper_id)
);

-- 野外簿：簿号只在遗址内唯一；簿内层号只在簿内有效。
CREATE TABLE IF NOT EXISTS notebooks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id     INTEGER NOT NULL REFERENCES sites(id),
    notebook_no TEXT NOT NULL,
    created     TEXT NOT NULL DEFAULT (datetime('now')),
    updated     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (site_id, notebook_no)
);

-- 簿内层号 -> 现有层位 id 的对照。
CREATE TABLE IF NOT EXISTS notebook_layers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    notebook_id INTEGER NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
    label       TEXT NOT NULL,
    stratum_id  INTEGER NOT NULL REFERENCES strata(id),
    UNIQUE (notebook_id, label)
);

-- 野外簿中带页行号的“下层 -> 上层”记录。提交时按当次对照解析并固化，
-- 之后替换簿会整本重写这些解析结果。
CREATE TABLE IF NOT EXISTS notebook_superpositions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    notebook_id INTEGER NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
    lower_label TEXT NOT NULL,
    upper_label TEXT NOT NULL,
    lower_id    INTEGER NOT NULL REFERENCES strata(id),
    upper_id    INTEGER NOT NULL REFERENCES strata(id),
    page        INTEGER,
    line        INTEGER
);

CREATE INDEX IF NOT EXISTS idx_strata_site ON strata(site_id);
CREATE INDEX IF NOT EXISTS idx_intervals_stratum ON stratum_intervals(stratum_id);
CREATE INDEX IF NOT EXISTS idx_corr_site ON correlations(site_id);
CREATE INDEX IF NOT EXISTS idx_sup_site ON superpositions(site_id);
CREATE INDEX IF NOT EXISTS idx_notebook_site ON notebooks(site_id);
CREATE INDEX IF NOT EXISTS idx_notebook_layers_nb ON notebook_layers(notebook_id);
CREATE INDEX IF NOT EXISTS idx_notebook_sup_nb ON notebook_superpositions(notebook_id);
CREATE INDEX IF NOT EXISTS idx_notebook_sup_pair ON notebook_superpositions(lower_id, upper_id);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('version', '0')"
    )
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------- meta / 版本号 ----------

def get_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
    return int(row["value"])


def bump_version(conn: sqlite3.Connection) -> int:
    conn.execute(
        "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) "
        "WHERE key = 'version'"
    )
    return get_version(conn)


# ---------- 遗址 ----------

def create_site(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    cur = conn.execute("INSERT INTO sites(name) VALUES (?)", (name,))
    return conn.execute("SELECT * FROM sites WHERE id = ?", (cur.lastrowid,)).fetchone()


def get_site(conn: sqlite3.Connection, site_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()


def get_site_by_name(conn: sqlite3.Connection, name: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM sites WHERE name = ?", (name,)).fetchone()


def list_sites(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM sites ORDER BY id").fetchall()


# ---------- 层位 ----------

def count_strata(conn: sqlite3.Connection, site_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM strata WHERE site_id = ?", (site_id,)
    ).fetchone()["c"]


def get_stratum(conn: sqlite3.Connection, stratum_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM strata WHERE id = ?", (stratum_id,)
    ).fetchone()


def find_stratum(
    conn: sqlite3.Connection, site_id: int, trench: str, layer_no: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM strata WHERE site_id = ? AND trench = ? AND layer_no = ?",
        (site_id, trench, layer_no),
    ).fetchone()


def create_stratum(
    conn: sqlite3.Connection,
    site_id: int,
    trench: str,
    layer_no: str,
    note: Optional[str],
    intervals: list[tuple[int, int]],
) -> sqlite3.Row:
    cur = conn.execute(
        "INSERT INTO strata(site_id, trench, layer_no, note) VALUES (?, ?, ?, ?)",
        (site_id, trench, layer_no, note),
    )
    stratum_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO stratum_intervals(stratum_id, year_from, year_to) "
        "VALUES (?, ?, ?)",
        [(stratum_id, lo, hi) for lo, hi in intervals],
    )
    return conn.execute(
        "SELECT * FROM strata WHERE id = ?", (stratum_id,)
    ).fetchone()


def list_strata(conn: sqlite3.Connection, site_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM strata WHERE site_id = ? ORDER BY id", (site_id,)
    ).fetchall()


def get_intervals(
    conn: sqlite3.Connection, stratum_id: int
) -> list[tuple[int, int]]:
    rows = conn.execute(
        "SELECT year_from, year_to FROM stratum_intervals "
        "WHERE stratum_id = ? ORDER BY year_from, year_to",
        (stratum_id,),
    ).fetchall()
    return [(r["year_from"], r["year_to"]) for r in rows]


# ---------- 对接 ----------

def create_correlation(
    conn: sqlite3.Connection,
    site_id: int,
    stratum_a_id: int,
    stratum_b_id: int,
) -> sqlite3.Row:
    lo, hi = sorted((stratum_a_id, stratum_b_id))
    cur = conn.execute(
        "INSERT INTO correlations(site_id, stratum_a_id, stratum_b_id) "
        "VALUES (?, ?, ?)",
        (site_id, lo, hi),
    )
    return conn.execute(
        "SELECT * FROM correlations WHERE id = ?", (cur.lastrowid,)
    ).fetchone()


def find_correlation(
    conn: sqlite3.Connection, stratum_a_id: int, stratum_b_id: int
) -> Optional[sqlite3.Row]:
    lo, hi = sorted((stratum_a_id, stratum_b_id))
    return conn.execute(
        "SELECT * FROM correlations WHERE stratum_a_id = ? AND stratum_b_id = ?",
        (lo, hi),
    ).fetchone()


def get_correlation(
    conn: sqlite3.Connection, correlation_id: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM correlations WHERE id = ?", (correlation_id,)
    ).fetchone()


def list_correlations(
    conn: sqlite3.Connection, site_id: int
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM correlations WHERE site_id = ? ORDER BY id", (site_id,)
    ).fetchall()


def delete_correlation(conn: sqlite3.Connection, correlation_id: int) -> None:
    conn.execute("DELETE FROM correlations WHERE id = ?", (correlation_id,))


# ---------- 叠压 ----------

def create_superposition(
    conn: sqlite3.Connection,
    site_id: int,
    lower_id: int,
    upper_id: int,
) -> sqlite3.Row:
    cur = conn.execute(
        "INSERT INTO superpositions(site_id, lower_id, upper_id) VALUES (?, ?, ?)",
        (site_id, lower_id, upper_id),
    )
    return conn.execute(
        "SELECT * FROM superpositions WHERE id = ?", (cur.lastrowid,)
    ).fetchone()


def find_superposition(
    conn: sqlite3.Connection, lower_id: int, upper_id: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM superpositions WHERE lower_id = ? AND upper_id = ?",
        (lower_id, upper_id),
    ).fetchone()


def get_superposition(
    conn: sqlite3.Connection, superposition_id: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM superpositions WHERE id = ?", (superposition_id,)
    ).fetchone()


def list_superpositions(
    conn: sqlite3.Connection, site_id: int
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM superpositions WHERE site_id = ? ORDER BY id", (site_id,)
    ).fetchall()


def delete_superposition(conn: sqlite3.Connection, superposition_id: int) -> None:
    conn.execute("DELETE FROM superpositions WHERE id = ?", (superposition_id,))


# ---------- 野外簿 ----------

def get_notebook(
    conn: sqlite3.Connection, site_id: int, notebook_no: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM notebooks WHERE site_id = ? AND notebook_no = ?",
        (site_id, notebook_no),
    ).fetchone()


def get_notebook_by_id(
    conn: sqlite3.Connection, notebook_id: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM notebooks WHERE id = ?", (notebook_id,)
    ).fetchone()


def list_notebooks(conn: sqlite3.Connection, site_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM notebooks WHERE site_id = ? ORDER BY notebook_no, id",
        (site_id,),
    ).fetchall()


def upsert_notebook(
    conn: sqlite3.Connection, site_id: int, notebook_no: str
) -> sqlite3.Row:
    """按簿号取簿；不存在则新建。已存在时更新提交时间并清空旧内容。"""
    row = get_notebook(conn, site_id, notebook_no)
    if row is None:
        cur = conn.execute(
            "INSERT INTO notebooks(site_id, notebook_no) VALUES (?, ?)",
            (site_id, notebook_no),
        )
        return get_notebook_by_id(conn, cur.lastrowid)
    conn.execute(
        "UPDATE notebooks SET updated = datetime('now') WHERE id = ?", (row["id"],)
    )
    clear_notebook_contents(conn, row["id"])
    return get_notebook_by_id(conn, row["id"])


def clear_notebook_contents(conn: sqlite3.Connection, notebook_id: int) -> None:
    """整本替换前清空旧对照与旧记录；簿本身（簿号、建档时间）保留。"""
    conn.execute(
        "DELETE FROM notebook_superpositions WHERE notebook_id = ?", (notebook_id,)
    )
    conn.execute("DELETE FROM notebook_layers WHERE notebook_id = ?", (notebook_id,))


def delete_notebook(conn: sqlite3.Connection, notebook_id: int) -> None:
    conn.execute("DELETE FROM notebooks WHERE id = ?", (notebook_id,))


def add_notebook_layer(
    conn: sqlite3.Connection, notebook_id: int, label: str, stratum_id: int
) -> sqlite3.Row:
    cur = conn.execute(
        "INSERT INTO notebook_layers(notebook_id, label, stratum_id) VALUES (?, ?, ?)",
        (notebook_id, label, stratum_id),
    )
    return conn.execute(
        "SELECT * FROM notebook_layers WHERE id = ?", (cur.lastrowid,)
    ).fetchone()


def add_notebook_superposition(
    conn: sqlite3.Connection,
    notebook_id: int,
    lower_label: str,
    upper_label: str,
    lower_id: int,
    upper_id: int,
    page: Optional[int],
    line: Optional[int],
) -> sqlite3.Row:
    cur = conn.execute(
        "INSERT INTO notebook_superpositions"
        "(notebook_id, lower_label, upper_label, lower_id, upper_id, page, line) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            notebook_id,
            lower_label,
            upper_label,
            lower_id,
            upper_id,
            page,
            line,
        ),
    )
    return conn.execute(
        "SELECT * FROM notebook_superpositions WHERE id = ?", (cur.lastrowid,)
    ).fetchone()


def list_notebook_layers(
    conn: sqlite3.Connection, notebook_id: int
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM notebook_layers WHERE notebook_id = ? ORDER BY id",
        (notebook_id,),
    ).fetchall()


def list_notebook_superpositions(
    conn: sqlite3.Connection, notebook_id: int
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM notebook_superpositions WHERE notebook_id = ? "
        "ORDER BY COALESCE(page, 0), COALESCE(line, 0), id",
        (notebook_id,),
    ).fetchall()


def list_site_notebook_superpositions(
    conn: sqlite3.Connection, site_id: int
) -> list[sqlite3.Row]:
    """整站所有野外簿记录（带簿号），用于求解装配与出处查询。"""
    return conn.execute(
        "SELECT ns.*, n.notebook_no "
        "FROM notebook_superpositions ns "
        "JOIN notebooks n ON n.id = ns.notebook_id "
        "WHERE n.site_id = ? "
        "ORDER BY n.notebook_no, COALESCE(ns.page, 0), "
        "COALESCE(ns.line, 0), ns.id",
        (site_id,),
    ).fetchall()
