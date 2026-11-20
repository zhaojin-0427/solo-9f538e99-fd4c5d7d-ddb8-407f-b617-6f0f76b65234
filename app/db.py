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

CREATE INDEX IF NOT EXISTS idx_strata_site ON strata(site_id);
CREATE INDEX IF NOT EXISTS idx_intervals_stratum ON stratum_intervals(stratum_id);
CREATE INDEX IF NOT EXISTS idx_corr_site ON correlations(site_id);
CREATE INDEX IF NOT EXISTS idx_sup_site ON superpositions(site_id);
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
