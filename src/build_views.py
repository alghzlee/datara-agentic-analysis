"""Create the agent views from views.sql and verify them.

Requires:  pip install duckdb
Run from the project root (after load_data.py):  python src/build_views.py
"""
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "olist.duckdb"
VIEWS_SQL = Path(__file__).resolve().parent / "views.sql"

# (label, SQL returning one number, expected value, tolerance)
# Expected values come from notebooks 01-03 or from the raw tables, not from the views.
CHECKS = [
    ("orders_clean rows = orders", "SELECT COUNT(*) FROM orders_clean", 99441, 0),
    ("order_items_enriched rows = order_items", "SELECT COUNT(*) FROM order_items_enriched", 112650, 0),
    ("SUM(price) view = raw", "SELECT SUM(price) FROM order_items_enriched", "SELECT SUM(price) FROM order_items", 0.01),
    ("SUM(freight) view = raw", "SELECT SUM(freight_value) FROM order_items_enriched", "SELECT SUM(freight_value) FROM order_items", 0.01),
    ("SUM(items_revenue) orders_clean = raw", "SELECT SUM(items_revenue) FROM orders_clean", "SELECT SUM(price) FROM order_items", 0.01),
    ("orders without items", "SELECT COUNT(*) FROM orders_clean WHERE n_items = 0", 775, 0),
    ("orders without review", "SELECT COUNT(*) FROM orders_clean WHERE review_score IS NULL", 768, 0),
    ("orders without payment", "SELECT COUNT(*) FROM orders_clean WHERE paid_total IS NULL", 1, 0),
    ("delivered orders with delivery_days (96,478 - 8)", "SELECT COUNT(delivery_days) FROM orders_clean", 96470, 0),
    ("unique customers", "SELECT COUNT(DISTINCT customer_unique_id) FROM orders_clean", 96096, 0),
    ("order_items_enriched: items without order", "SELECT COUNT(*) FROM order_items_enriched WHERE order_status IS NULL", 0, 0),
    ("order_items_enriched: items without customer", "SELECT COUNT(*) FROM order_items_enriched WHERE customer_unique_id IS NULL", 0, 0),
    ("order_items_enriched: NULL category", "SELECT COUNT(*) FROM order_items_enriched WHERE category IS NULL", 0, 0),
    ("payments_clean rows (103,886 - 3)", "SELECT COUNT(*) FROM payments_clean", 103883, 0),
    ("geo_per_zip rows", "SELECT COUNT(*) FROM geo_per_zip", 19015, 0),
    ("geo_per_zip multi_state zips", "SELECT COUNT(*) FROM geo_per_zip WHERE multi_state", 8, 0),
]

# Informational numbers to compare against pandas by yourself (no pass/fail).
INFO = [
    ("GMV of delivered orders, SUM(price)", "SELECT ROUND(SUM(price), 2) FROM order_items_enriched WHERE order_status = 'delivered'"),
    ("Late rate among delivered orders", "SELECT ROUND(AVG(CAST(is_late AS INTEGER)), 4) FROM orders_clean WHERE delivery_days IS NOT NULL"),
    ("Items with category 'unknown'", "SELECT COUNT(*) FROM order_items_enriched WHERE category = 'unknown'"),
    ("Zips without coordinates", "SELECT COUNT(*) FROM geo_per_zip WHERE lat IS NULL"),
]


def one(con, sql):
    return con.execute(sql).fetchone()[0]


def fmt(x):
    return f"{x:,}" if isinstance(x, (int, float)) else str(x)


def main():
    con = duckdb.connect(str(DB_PATH))

    statements = [s.strip() for s in VIEWS_SQL.read_text().split(";") if "CREATE" in s]
    for stmt in statements:
        con.execute(stmt)
    print(f"Created {len(statements)} views from {VIEWS_SQL.name}\n")

    problems = []
    for label, sql, expected, tol in CHECKS:
        got = one(con, sql)
        want = one(con, expected) if isinstance(expected, str) else expected
        ok = got is not None and abs(got - want) <= tol
        print(f"{'OK      ' if ok else 'MISMATCH'} {label:<52} got {fmt(got)} | expected {fmt(want)}")
        if not ok:
            problems.append(label)

    print("\nFor your own comparison:")
    for label, sql in INFO:
        print(f"  {label}: {one(con, sql)}")

    con.close()
    if problems:
        raise SystemExit("\nChecks failed:\n  " + "\n  ".join(problems))
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
