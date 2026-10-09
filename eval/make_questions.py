"""Generate eval/questions.csv for the Olist agent.

Every numeric/text answer is computed TWO independent ways:
  1. pandas on the raw CSV files   -> becomes expected_answer
  2. SQL on the DuckDB views       -> becomes gold_sql, and must agree with (1)
If the two disagree, either the view or the pandas logic has a bug.

Requires:  pip install pandas duckdb
Run from the project root (after build_views.py):  python eval/make_questions.py
"""
import csv
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
CSV_DIR = ROOT / "dataset"
DB_PATH = ROOT / "olist.duckdb"
OUT = Path(__file__).resolve().parent / "questions.csv"

# Default analysis window from schema.md: purchase_ts in [2017-01-01, 2018-09-01)
W_START, W_END = "2017-01-01", "2018-09-01"
W_SQL = f"purchase_ts >= '{W_START}' AND purchase_ts < '{W_END}'"
DELIV = "order_status = 'delivered'"
W_TXT = "antara Januari 2017 dan Agustus 2018"


# ---------------------------------------------------------------- pandas side
def load_frames(csv_dir):
    ts = ["order_purchase_timestamp", "order_delivered_customer_date", "order_estimated_delivery_date"]
    o = pd.read_csv(csv_dir / "olist_orders_dataset.csv", parse_dates=ts)
    o = o.rename(columns={
        "order_purchase_timestamp": "purchase_ts",
        "order_delivered_customer_date": "delivered_ts",
        "order_estimated_delivery_date": "estimated_ts",
    })
    cust = pd.read_csv(csv_dir / "olist_customers_dataset.csv")[["customer_id", "customer_unique_id", "customer_state"]]
    o = o.merge(cust, on="customer_id", how="left")

    prod = pd.read_csv(csv_dir / "olist_products_dataset.csv")[["product_id", "product_category_name"]]
    trans = pd.read_csv(csv_dir / "product_category_name_translation.csv")
    prod = prod.merge(trans, on="product_category_name", how="left")
    prod["category"] = (prod["product_category_name_english"]
                        .fillna(prod["product_category_name"]).fillna("unknown"))

    it = pd.read_csv(csv_dir / "olist_order_items_dataset.csv")[["order_id", "seller_id", "product_id", "price"]]
    it = it.merge(prod[["product_id", "category"]], on="product_id", how="left")
    it["category"] = it["category"].fillna("unknown")
    it = it.merge(o[["order_id", "order_status", "purchase_ts"]], on="order_id", how="left")

    pay = pd.read_csv(csv_dir / "olist_order_payments_dataset.csv")
    rev = pd.read_csv(csv_dir / "olist_order_reviews_dataset.csv", parse_dates=["review_answer_timestamp"])
    return {"orders": o, "items": it, "payments": pay, "reviews": rev}


def in_window(df):
    return (df.purchase_ts >= W_START) & (df.purchase_ts < W_END)


def in_month(df, ym):
    return df.purchase_ts.dt.to_period("M") == pd.Period(ym)


def explain_change(o, it, m0, m1, increase):
    """Decompose revenue change m0 -> m1 into volume and price effects, and rank categories."""
    n0 = o[(o.order_status == "delivered") & in_month(o, m0)].order_id.nunique()
    n1 = o[(o.order_status == "delivered") & in_month(o, m1)].order_id.nunique()
    i0 = it[(it.order_status == "delivered") & in_month(it, m0)]
    i1 = it[(it.order_status == "delivered") & in_month(it, m1)]
    r0, r1 = i0.price.sum(), i1.price.sum()
    a0, a1 = r0 / n0, r1 / n1
    if (r1 > r0) != increase:
        raise SystemExit(f"Question premise is wrong: revenue {m0} -> {m1} did not {'rise' if increase else 'fall'} "
                         f"({r0:,.0f} -> {r1:,.0f}). Reword the question.")
    volume, price_eff = (n1 - n0) * a0, n1 * (a1 - a0)
    delta = (i1.groupby("category").price.sum()
             .sub(i0.groupby("category").price.sum(), fill_value=0))
    top = delta.sort_values(ascending=not increase).head(3).index.tolist()
    driver = "jumlah order (volume)" if abs(volume) > abs(price_eff) else "nilai rata-rata order (harga)"
    behavior = (
        f"Menguraikan perubahan revenue {m0} -> {m1}: order {n0} -> {n1}, rata-rata nilai order "
        f"{a0:.2f} -> {a1:.2f}, efek volume {volume:+,.0f}, efek harga {price_eff:+,.0f}; pendorong utama: {driver}. "
        f"Menyebut kategori kontributor terbesar (lihat expected_answer) sebagai 'kontributor', bukan 'penyebab pasti'."
    )
    return "|".join(top), behavior


def reference(f):
    o, it, pay, rev = f["orders"], f["items"], f["payments"], f["reviews"]
    od = o[(o.order_status == "delivered") & in_window(o)]
    itd = it[(it.order_status == "delivered") & in_window(it)]
    dd = od[od.delivered_ts.notna()]
    latest = rev.sort_values("review_answer_timestamp", ascending=False).drop_duplicates("order_id")
    deliv_items = it[it.order_status == "delivered"]

    r = {}
    r["N01"] = deliv_items[deliv_items.purchase_ts.dt.year == 2017].price.sum()
    r["N02"] = o[(o.order_status == "delivered") & in_month(o, "2017-11")].order_id.nunique()
    r["N03"] = itd.groupby("category").price.sum().idxmax()
    r["N04"] = od.groupby("customer_state").order_id.nunique().idxmax()
    r["N05"] = int((od.groupby("customer_unique_id").order_id.nunique() > 1).sum())
    r["N06"] = itd.price.sum() / od.order_id.nunique()
    r["N07"] = (dd.delivered_ts.dt.normalize() > dd.estimated_ts.dt.normalize()).mean() * 100
    r["N08"] = od.merge(latest[["order_id", "review_score"]], on="order_id", how="left").review_score.mean()
    r["N09"] = (pay[pay.payment_type != "not_defined"].merge(od[["order_id"]], on="order_id")
                .payment_type.value_counts().idxmax())
    r["N10"] = itd.groupby("seller_id").price.sum().idxmax()
    r["N11"] = ((dd.delivered_ts - dd.purchase_ts).dt.total_seconds() / 86400).mean()
    r["N12"] = int((o.order_status == "canceled").sum())
    r["T01"] = deliv_items[in_month(deliv_items, "2016-12")].price.sum()
    r["T02"] = deliv_items[in_month(deliv_items, "2018-10")].price.sum()
    r["W01"], r["W01_behavior"] = explain_change(o, it, "2017-10", "2017-11", increase=True)
    r["W02"], r["W02_behavior"] = explain_change(o, it, "2017-11", "2017-12", increase=False)
    return r


# ------------------------------------------------------------------ SQL side
def next_month(ym):
    y, m = int(ym[:4]), int(ym[5:7])
    return f"{y + (m == 12)}-{(m % 12) + 1:02d}"


def month_sql(ym):
    return f"purchase_ts >= '{ym}-01' AND purchase_ts < '{next_month(ym)}-01'"


def change_sql(m0, m1, increase):
    order = "DESC" if increase else "ASC"
    return (
        f"SELECT string_agg(category, '|' ORDER BY delta {order}) FROM ("
        "SELECT category, "
        f"SUM(CASE WHEN {month_sql(m1)} THEN price ELSE 0 END) - "
        f"SUM(CASE WHEN {month_sql(m0)} THEN price ELSE 0 END) AS delta "
        f"FROM order_items_enriched WHERE {DELIV} "
        f"AND purchase_ts >= '{m0}-01' AND purchase_ts < '{next_month(m1)}-01' "
        f"GROUP BY category ORDER BY delta {order} LIMIT 3)"
    )


# --------------------------------------------------------------- the questions
# (id, type, question, answer_type, tolerance, must_include, expected_behavior, gold_sql)
def questions():
    N = "normal"
    return [
        ("N01", N, "Berapa total revenue (GMV) order yang sudah delivered sepanjang tahun 2017?", "number", 0.01, "", "",
         f"SELECT SUM(price) FROM order_items_enriched WHERE {DELIV} AND purchase_ts >= '2017-01-01' AND purchase_ts < '2018-01-01'"),
        ("N02", N, "Berapa jumlah order delivered yang dibeli pada November 2017?", "number", 0, "", "",
         f"SELECT COUNT(*) FROM orders_clean WHERE {DELIV} AND {month_sql('2017-11')}"),
        ("N03", N, f"{W_TXT[0].upper() + W_TXT[1:]}, kategori produk apa yang menghasilkan revenue tertinggi pada order delivered?", "text", 0, "", "",
         f"SELECT category FROM order_items_enriched WHERE {DELIV} AND {W_SQL} GROUP BY category ORDER BY SUM(price) DESC LIMIT 1"),
        ("N04", N, f"{W_TXT[0].upper() + W_TXT[1:]}, pelanggan dari state mana yang punya order delivered terbanyak?", "text", 0, "", "",
         f"SELECT customer_state FROM orders_clean WHERE {DELIV} AND {W_SQL} GROUP BY customer_state ORDER BY COUNT(*) DESC LIMIT 1"),
        ("N05", N, f"{W_TXT[0].upper() + W_TXT[1:]}, berapa pelanggan unik yang punya lebih dari satu order delivered?", "number", 0, "", "",
         f"SELECT COUNT(*) FROM (SELECT customer_unique_id FROM orders_clean WHERE {DELIV} AND {W_SQL} GROUP BY customer_unique_id HAVING COUNT(*) > 1)"),
        ("N06", N, f"{W_TXT[0].upper() + W_TXT[1:]}, berapa rata-rata nilai order (AOV) pada order delivered?", "number", 0.01, "", "",
         f"SELECT SUM(items_revenue) / COUNT(*) FROM orders_clean WHERE {DELIV} AND {W_SQL}"),
        ("N07", N, f"{W_TXT[0].upper() + W_TXT[1:]}, berapa persen order delivered yang tiba lebih lambat dari tanggal estimasi?", "number", 0.01, "", "",
         f"SELECT 100.0 * AVG(CAST(is_late AS INTEGER)) FROM orders_clean WHERE delivery_days IS NOT NULL AND {W_SQL}"),
        ("N08", N, f"{W_TXT[0].upper() + W_TXT[1:]}, berapa rata-rata skor review pada order delivered?", "number", 0.001, "", "",
         f"SELECT AVG(review_score) FROM orders_clean WHERE {DELIV} AND {W_SQL}"),
        ("N09", N, f"{W_TXT[0].upper() + W_TXT[1:]}, metode pembayaran apa yang paling sering dipakai pada order delivered?", "text", 0, "", "",
         f"SELECT p.payment_type FROM payments_clean p JOIN orders_clean o ON o.order_id = p.order_id WHERE o.{DELIV} AND o.{W_SQL} GROUP BY p.payment_type ORDER BY COUNT(*) DESC LIMIT 1"),
        ("N10", N, f"{W_TXT[0].upper() + W_TXT[1:]}, seller mana (seller_id) dengan revenue tertinggi pada order delivered?", "text", 0, "", "",
         f"SELECT seller_id FROM order_items_enriched WHERE {DELIV} AND {W_SQL} GROUP BY seller_id ORDER BY SUM(price) DESC LIMIT 1"),
        ("N11", N, f"{W_TXT[0].upper() + W_TXT[1:]}, berapa rata-rata lama pengiriman (hari) dari pembelian sampai tiba di pelanggan?", "number", 0.01, "", "",
         f"SELECT AVG(delivery_days) FROM orders_clean WHERE delivery_days IS NOT NULL AND {W_SQL}"),
        ("N12", N, "Berapa jumlah order yang berstatus canceled?", "number", 0, "", "Tidak boleh memfilter delivered, karena yang ditanya adalah status canceled.",
         "SELECT COUNT(*) FROM orders_clean WHERE order_status = 'canceled'"),

        ("T01", "trap", "Berapa revenue Desember 2016?", "number", 0.01, "", "Boleh menyebut angka hasil query, tetapi wajib memperingatkan bahwa data Desember 2016 sangat tidak lengkap (sangat sedikit order) dan tidak layak dibandingkan dengan bulan lain.",
         f"SELECT COALESCE(SUM(price), 0) FROM order_items_enriched WHERE {DELIV} AND {month_sql('2016-12')}"),
        ("T02", "trap", "Berapa revenue Oktober 2018?", "number", 0.01, "", "Boleh menyebut angka hasil query, tetapi wajib memperingatkan bahwa data Oktober 2018 tidak lengkap (data berakhir 2018-10-17 dan ordernya sangat sedikit).",
         f"SELECT COALESCE(SUM(price), 0) FROM order_items_enriched WHERE {DELIV} AND {month_sql('2018-10')}"),
        ("T03", "trap", "Berapa laba (profit) per kategori produk?", "refusal", 0, "", "Menolak menghitung laba atau margin karena dataset tidak memuat biaya/harga pokok. Boleh menawarkan revenue (GMV) sebagai alternatif. Tidak boleh mengarang angka.", ""),
        ("T04", "trap", "Seller mana yang paling lambat memproses pesanan sebelum diserahkan ke kurir?", "refusal", 0, "", "Menjelaskan bahwa tanggal serah-terima ke kurir tidak andal (banyak anomali) sehingga waktu proses seller tidak dihitung. Tidak boleh mengarang jawaban.", ""),
        ("T05", "trap", "Berapa pendapatan Olist dari komisi seller?", "clarify", 0, "GMV", "Menjelaskan bahwa data hanya memuat penjualan kotor seller (GMV) dan tidak memuat komisi atau pendapatan Olist. Tidak boleh menyajikan GMV sebagai pendapatan Olist.", ""),
        ("T06", "trap", "Berapa penjualan bulan lalu?", "clarify", 0, "2018", "Menyebutkan bahwa data berakhir pada 2018-10-17 sehingga 'bulan lalu' menurut tanggal hari ini tidak ada. Menyatakan asumsi periode (misalnya bulan lengkap terakhir, Agustus 2018) atau meminta klarifikasi.", ""),

        ("W01", "why", "Kenapa revenue November 2017 jauh lebih tinggi dibanding Oktober 2017?", "list", 0, "", "(diisi otomatis)", change_sql("2017-10", "2017-11", True)),
        ("W02", "why", "Kenapa revenue Desember 2017 turun dibanding November 2017?", "list", 0, "", "(diisi otomatis)", change_sql("2017-11", "2017-12", False)),
    ]


# ------------------------------------------------------------------- main
def fmt(v):
    if isinstance(v, (int,)) or (hasattr(v, "dtype") and "int" in str(v.dtype)):
        return str(int(v))
    if isinstance(v, float):
        return str(round(v, 4))
    return str(v)


def same(a, b, tol, answer_type):
    if answer_type == "number":
        a, b = float(a), float(b)
        return abs(a - b) <= max(tol, 1e-9 * abs(b))
    return str(a) == str(b)


def build_rows(frames):
    ref = reference(frames)
    rows = []
    for qid, qtype, text, atype, tol, must, behavior, sql in questions():
        expected = fmt(ref[qid]) if qid in ref else ""
        if behavior == "(diisi otomatis)":
            behavior = ref[qid + "_behavior"]
        rows.append({"id": qid, "type": qtype, "question": text, "answer_type": atype,
                     "expected_answer": expected, "tolerance": tol, "must_include": must,
                     "expected_behavior": behavior, "gold_sql": sql})
    return rows


def cross_check(rows, db_path):
    import duckdb
    con = duckdb.connect(str(db_path), read_only=True)
    bad = 0
    for r in rows:
        if not r["gold_sql"] or not r["expected_answer"]:
            continue
        got = con.execute(r["gold_sql"]).fetchone()[0]
        ok = got is not None and same(got, r["expected_answer"], float(r["tolerance"]), r["answer_type"])
        print(f"{'OK      ' if ok else 'MISMATCH'} {r['id']}  pandas={r['expected_answer']!s:<40.40} sql={got!s:.40}")
        bad += not ok
    con.close()
    return bad


def main():
    rows = build_rows(load_frames(CSV_DIR))
    with open(OUT, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} questions to {OUT}\n")

    if not DB_PATH.exists():
        print("olist.duckdb not found: SQL cross-check skipped.")
        return
    bad = cross_check(rows, DB_PATH)
    if bad:
        raise SystemExit(f"\n{bad} question(s) where pandas and SQL disagree. Do not use these answers yet.")
    print("\nAll computed answers agree between pandas and SQL.")


if __name__ == "__main__":
    main()
