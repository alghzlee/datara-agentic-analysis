"""Tools the agent can call: get_schema and run_sql (read-only, views only).

Safety layers for run_sql:
  1. the database is opened read-only with external file access disabled
  2. only a single SELECT/WITH statement is accepted
  3. raw tables are blocked, so the agent can only use the 4 documented views
  4. at most MAX_ROWS rows are returned and the query is interrupted after TIMEOUT_SECONDS
"""
import re
import threading
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "olist.duckdb"

MAX_ROWS = 200
TIMEOUT_SECONDS = 20

VIEWS = ["order_items_enriched", "orders_clean", "payments_clean", "geo_per_zip"]
RAW_TABLES = [
    "customers", "geolocation", "order_items", "order_payments", "order_reviews",
    "orders", "products", "sellers", "product_category_name_translation",
]
FORBIDDEN_WORDS = [
    "insert", "update", "delete", "drop", "create", "alter", "attach", "detach", "copy",
    "pragma", "install", "load", "export", "import", "call", "set", "truncate", "vacuum",
]

TOOLS = [
    {
        "name": "get_schema",
        "description": (
            "Returns the actual columns and types of the 4 views you may query "
            "(order_items_enriched, orders_clean, payments_clean, geo_per_zip). "
            "Call this first if you are unsure about a column name."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "run_sql",
        "description": (
            "Runs ONE read-only DuckDB SQL query (SELECT or WITH) on the 4 views and returns the rows. "
            f"At most {MAX_ROWS} rows are returned, so aggregate in SQL. "
            "If the query fails, read the error message, fix the SQL, and try again."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "A single DuckDB SELECT or WITH query."}},
            "required": ["sql"],
        },
    },
]


def connect(db_path=DB_PATH):
    return duckdb.connect(str(db_path), read_only=True, config={"enable_external_access": False})


def validate_sql(sql: str) -> str:
    """Return the cleaned SQL, or raise ValueError with an instructive message."""
    cleaned = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    cleaned = re.sub(r"--[^\n]*", " ", cleaned).strip().rstrip(";").strip()
    lowered = cleaned.lower()

    if not lowered:
        raise ValueError("Query kosong.")
    if ";" in cleaned:
        raise ValueError("Hanya satu statement yang diizinkan (hapus tanda titik koma di tengah query).")
    if not re.match(r"(select|with)\b", lowered):
        raise ValueError("Query harus diawali SELECT atau WITH. Hanya query baca yang diizinkan.")
    for word in FORBIDDEN_WORDS:
        if re.search(rf"\b{word}\b", lowered):
            raise ValueError(f"Kata '{word}' tidak diizinkan. Hanya query baca (SELECT) yang boleh dijalankan.")
    if re.search(r"\b(read_\w+|glob)\s*\(", lowered):
        raise ValueError("Membaca file tidak diizinkan. Gunakan view yang tersedia.")
    for table in RAW_TABLES:
        if re.search(rf"\b{table}\b", lowered):
            raise ValueError(
                f"Tabel mentah '{table}' tidak boleh dipakai. Gunakan hanya view: {', '.join(VIEWS)}."
            )
    return cleaned


def format_rows(columns, rows, truncated):
    lines = [" | ".join(columns)]
    for row in rows:
        lines.append(" | ".join("NULL" if v is None else str(v) for v in row))
    lines.append(f"({len(rows)} baris{', DIPOTONG di batas ' + str(MAX_ROWS) + ' baris; agregasi lebih lanjut di SQL' if truncated else ''})")
    return "\n".join(lines)


def run_sql(con, sql: str) -> str:
    cleaned = validate_sql(sql)
    timer = threading.Timer(TIMEOUT_SECONDS, con.interrupt)
    timer.start()
    try:
        cur = con.execute(cleaned)
        columns = [d[0] for d in cur.description]
        rows = cur.fetchmany(MAX_ROWS + 1)
    except duckdb.Error as exc:
        raise ValueError(f"SQL error: {exc}") from exc
    finally:
        timer.cancel()
    truncated = len(rows) > MAX_ROWS
    return format_rows(columns, rows[:MAX_ROWS], truncated)


def get_schema(con) -> str:
    parts = []
    for view in VIEWS:
        cols = con.execute(f"DESCRIBE {view}").fetchall()
        parts.append(f"{view}: " + ", ".join(f"{c[0]} {c[1]}" for c in cols))
    return "\n".join(parts)


def execute_tool(con, name: str, args: dict):
    """Run a tool call. Returns (text, is_error)."""
    try:
        if name == "get_schema":
            return get_schema(con), False
        if name == "run_sql":
            return run_sql(con, args.get("sql", "")), False
        return f"Tool tidak dikenal: {name}", True
    except ValueError as exc:
        return str(exc), True
