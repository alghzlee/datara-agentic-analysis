"""Load the 9 Olist CSV files into a DuckDB database.

Requires:  pip install duckdb
Run from the project root:  python src/load_data.py

Expected layout:
    olist-agent/
    ├── dataset/        (the 9 CSV files)
    ├── src/load_data.py
    └── olist.duckdb    (created by this script)
"""
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
CSV_DIR = ROOT / "dataset"
DB_PATH = ROOT / "olist.duckdb"

# table name -> (csv file, columns to cast to TIMESTAMP)
TABLES = {
    "customers": ("olist_customers_dataset.csv", []),
    "geolocation": ("olist_geolocation_dataset.csv", []),
    "order_items": ("olist_order_items_dataset.csv", ["shipping_limit_date"]),
    "order_payments": ("olist_order_payments_dataset.csv", []),
    "order_reviews": (
        "olist_order_reviews_dataset.csv",
        ["review_creation_date", "review_answer_timestamp"],
    ),
    "orders": (
        "olist_orders_dataset.csv",
        [
            "order_purchase_timestamp",
            "order_approved_at",
            "order_delivered_carrier_date",
            "order_delivered_customer_date",
            "order_estimated_delivery_date",
        ],
    ),
    "products": ("olist_products_dataset.csv", []),
    "sellers": ("olist_sellers_dataset.csv", []),
    "product_category_name_translation": ("product_category_name_translation.csv", []),
}

# Row counts taken from notebook 01 (computed independently with pandas).
EXPECTED_ROWS = {
    "customers": 99441,
    "geolocation": 1000163,
    "order_items": 112650,
    "order_payments": 103886,
    "order_reviews": 99224,
    "orders": 99441,
    "products": 32951,
    "sellers": 3095,
    "product_category_name_translation": 71,
}

# Null counts from notebook 01. If the TIMESTAMP cast silently dropped values,
# these would change.
EXPECTED_NULLS = {
    ("orders", "order_approved_at"): 160,
    ("orders", "order_delivered_carrier_date"): 1783,
    ("orders", "order_delivered_customer_date"): 2965,
}


def build_sql(table: str, csv_dir: Path = CSV_DIR) -> str:
    filename, ts_cols = TABLES[table]
    path = str(csv_dir / filename).replace("\\", "/").replace("'", "''")
    select = "*"
    if ts_cols:
        casts = ", ".join(f"CAST({c} AS TIMESTAMP) AS {c}" for c in ts_cols)
        select = f"* REPLACE ({casts})"
    return (
        f"CREATE OR REPLACE TABLE {table} AS "
        f"SELECT {select} FROM read_csv('{path}', header=true, sample_size=-1)"
    )


def load(csv_dir=CSV_DIR, db_path=DB_PATH, expected_rows=EXPECTED_ROWS, expected_nulls=EXPECTED_NULLS):
    con = duckdb.connect(str(db_path))
    problems = []

    for table in TABLES:
        con.execute(build_sql(table, csv_dir))
        n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        ok = n == expected_rows[table]
        print(f"{'OK      ' if ok else 'MISMATCH'} {table:<36} {n:>10,} rows (expected {expected_rows[table]:,})")
        if not ok:
            problems.append(f"{table}: {n} rows, expected {expected_rows[table]}")

    for (table, column), expected in expected_nulls.items():
        n = con.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} IS NULL").fetchone()[0]
        ok = n == expected
        print(f"{'OK      ' if ok else 'MISMATCH'} {table}.{column} nulls: {n:,} (expected {expected:,})")
        if not ok:
            problems.append(f"{table}.{column}: {n} nulls, expected {expected}")

    con.close()
    if problems:
        raise SystemExit("Load finished with problems:\n  " + "\n  ".join(problems))
    print(f"\nAll checks passed. Database: {db_path}")


if __name__ == "__main__":
    load()
