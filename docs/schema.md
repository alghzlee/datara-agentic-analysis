# Olist E-Commerce: Skema Data dan Aturan Bisnis

Dokumen ini adalah satu-satunya sumber kebenaran untuk agent analisis data. Agent hanya boleh mengakses **4 view** di bagian 3, bukan tabel mentah. Aturan di bagian 2 dan 4 wajib diikuti.

## 1. Gambaran Data

- Sumber: dataset publik Olist (marketplace Brasil), 9 CSV.
- Periode data: 2016-09-04 sampai 2018-10-17. **Periode yang lengkap: 2017-01 sampai 2018-08.**
- Ukuran: 99.441 order, 112.650 baris item, 96.096 pelanggan unik, 3.095 seller, 32.951 produk.
- Mata uang: tidak ada kolom mata uang. Diasumsikan Real Brasil (BRL).
- Timestamp tidak punya zona waktu dan diperlakukan apa adanya (tanpa konversi).
- Nama toko dan mitra di dataset sudah disamarkan.

Status order (seluruh data): `delivered` 96.478, `shipped` 1.107, `canceled` 625, `unavailable` 609, `invoiced` 314, `processing` 301, `created` 5, `approved` 2.

## 2. Definisi Metrik (WAJIB)

| Metrik | Definisi |
|---|---|
| **Revenue (GMV)** | `SUM(price)` dari order `delivered`. Ongkir tidak termasuk. Ini penjualan kotor seluruh seller, **bukan pendapatan Olist** (komisi tidak ada di data). |
| **Ongkir** | `SUM(freight_value)`, dilaporkan terpisah dari revenue. |
| **Total dibayar pelanggan** | `price + freight_value` (terbukti: untuk 99,6% order, jumlah payment sama dengan nilai ini). |
| **Jumlah order** | `COUNT(DISTINCT order_id)`. Jangan menghitung baris dari view level item. |
| **Jumlah pelanggan** | `COUNT(DISTINCT customer_unique_id)`. Jangan memakai `customer_id` (unik per order). |
| **Rata-rata nilai order (AOV)** | `SUM(price) / COUNT(DISTINCT order_id)` dari order `delivered`. |
| **Lama pengiriman** | `delivery_days` di `orders_clean` (pembelian sampai tiba di pelanggan). |
| **Terlambat** | `is_late` di `orders_clean` (tiba setelah tanggal estimasi, dibandingkan per tanggal). |
| **Skor review order** | `review_score` di `orders_clean` (1 review terbaru per order). |

**Filter bawaan:** semua metrik penjualan (revenue, AOV, produk/kategori/seller terlaris) hanya memakai order `delivered`. Pengecualian: pertanyaan tentang status, pembatalan, atau tingkat kegagalan tidak difilter.

**Atribusi waktu bawaan:** order dikelompokkan berdasarkan `purchase_ts` (tanggal pembelian), bukan tanggal tiba.

## 3. View yang Tersedia

### `order_items_enriched` (grain: 1 baris = 1 item dalam order)
Semua status order. Dipakai untuk revenue, produk, kategori, dan seller.

| Kolom | Arti |
|---|---|
| `order_id`, `order_item_id` | Key. `order_item_id` adalah urutan item dalam order. |
| `order_status` | Status order. |
| `purchase_ts` | Waktu pembelian (TIMESTAMP). |
| `customer_unique_id` | Pelanggan sebenarnya. |
| `customer_state` | State pelanggan. |
| `seller_id`, `seller_state` | Seller dan state-nya. |
| `product_id` | Produk. |
| `category` | Kategori (Inggris). Fallback ke nama Portugis, lalu `'unknown'`. |
| `price` | Harga barang. |
| `freight_value` | Ongkos kirim. |
| `item_total` | `price + freight_value`. |

### `orders_clean` (grain: 1 baris = 1 order, seluruh 99.441 order)
Dipakai untuk status, waktu, pengiriman, review, dan pelanggan.

| Kolom | Arti |
|---|---|
| `order_id` | Key. |
| `customer_unique_id`, `customer_state` | Pelanggan dan state-nya. |
| `order_status` | Status order. |
| `purchase_ts` | Waktu pembelian. |
| `delivered_customer_ts` | Waktu tiba di pelanggan (NULL jika belum). |
| `estimated_delivery_date` | Tanggal estimasi tiba (DATE). |
| `n_items` | Jumlah item. 0 jika order tidak punya item. |
| `items_revenue` | `SUM(price)` order ini. NULL jika tanpa item. |
| `freight_total` | `SUM(freight_value)` order ini. |
| `paid_total` | `SUM(payment_value)` order ini. NULL jika tanpa payment. |
| `review_score` | Skor review terbaru (1-5). NULL jika tanpa review. |
| `delivery_days` | Hari dari pembelian sampai tiba. Hanya terisi untuk `delivered` yang punya `delivered_customer_ts`. |
| `is_late` | TRUE jika tanggal tiba > `estimated_delivery_date`. Syarat terisi sama dengan `delivery_days`. |

Kolom tanggal kurir (`order_delivered_carrier_date`) sengaja **tidak disediakan** (lihat bagian 4).

### `payments_clean` (grain: 1 baris = 1 catatan pembayaran)
Satu order bisa punya banyak baris (cicilan, voucher). Baris bertipe `not_defined` (3 baris) sudah dibuang.

| Kolom | Arti |
|---|---|
| `order_id`, `payment_sequential` | Key. |
| `payment_type` | `credit_card`, `boleto`, `voucher`, `debit_card`. |
| `payment_installments` | Jumlah cicilan. |
| `payment_value` | Nilai pembayaran. |

Untuk revenue, **jangan** pakai tabel ini. Pakai `price` dari `order_items_enriched`.

### `geo_per_zip` (grain: 1 baris = 1 zip code prefix)

| Kolom | Arti |
|---|---|
| `zip_code_prefix` | Key. |
| `lat`, `lng` | Median koordinat. NULL jika semua baris zip berada di luar Brasil. |
| `state` | State yang paling sering muncul untuk zip ini. |
| `multi_state` | TRUE jika zip terpetakan ke lebih dari satu state (8 zip). |

## 4. Aturan Join dan Jebakan Data

| Hal | Aturan |
|---|---|
| Orders → items | 1 order : 0..N item. Pakai `LEFT JOIN`. 775 order tidak punya item (hampir semuanya `unavailable`/`canceled`). |
| Orders → payments | 1 : 0..N. **Agregasi payment per order sebelum di-join**, kalau tidak nilai terhitung ganda. 2.961 order punya lebih dari satu payment. |
| Orders → reviews | 1 : 0..N. 768 order tanpa review. 547 order punya lebih dari satu review. Sudah diselesaikan di `orders_clean` (ambil `review_answer_timestamp` terbaru). |
| `review_id` | Bukan key (789 `review_id` terpasang di lebih dari satu order). Hitung review per order. |
| Orders → customers | 1 : 1 pada `customer_id`. Pelanggan sebenarnya = `customer_unique_id` (2.997 pelanggan membeli lebih dari sekali). |
| Items → products / sellers | N : 1. |
| Review per seller | Review melekat ke order, bukan ke seller. Order dengan banyak seller memberi skor yang sama ke semuanya. Sebutkan batasan ini saat menjawab. |
| Geolocation | Tabel mentah punya 1.000.163 baris untuk 19.015 zip. **Jangan di-join langsung.** Pakai `geo_per_zip`. 7 seller dan 278 pelanggan (157 zip) tidak punya koordinat. Analisis per kota/state tidak terpengaruh. |
| Kategori | 610 produk tanpa kategori dan 2 kategori tanpa terjemahan (`pc_gamer`, `portateis_cozinha_e_preparadores_de_alimentos`). Semuanya ditangani oleh kolom `category`. |
| Tanggal kurir | Tidak konsisten: 166 diambil kurir sebelum dibeli, 23 sampai sebelum diambil kurir, 1.359 diambil sebelum disetujui. **Jangan** membuat metrik waktu proses seller. |
| Pengiriman | Hanya order `delivered` dengan tanggal tiba terisi yang dihitung (8 order `delivered` tanpa tanggal tiba dibuang). 6 order `canceled` yang punya tanggal tiba bukan pengiriman. |
| Tanggal estimasi | Kolom ini hanya berisi tanggal (459 nilai unik), jadi keterlambatan dibandingkan per tanggal, bukan per jam. |
| Distribusi harga | Miring ke kanan (rata-rata 120,65, median 74,99). Untuk "harga tipikal" gunakan median. |
| Ongkir 0 | 383 item (0,3%). Dianggap sah (kemungkinan gratis ongkir). |

## 5. Aturan Menjawab untuk Agent

1. **Sebutkan definisi dan filter yang dipakai** di setiap jawaban, misalnya "Revenue = SUM(price), order delivered, 2017-01 sampai 2018-08".
2. **Sebutkan query SQL** yang dijalankan.
3. **Peringatkan jika periode yang ditanyakan di luar 2017-01 sampai 2018-08.** Data 2016 dan September-Oktober 2018 sangat tipis (misalnya 2016-12 hanya 1 order, 2016-11 kosong), dan perbandingan bulan ke bulan di awal 2017 mencerminkan pertumbuhan marketplace.
4. Jika pertanyaan ambigu (misalnya "penjualan" tanpa periode), nyatakan asumsi yang dipakai.
5. Jika tidak yakin hasilnya benar, katakan, dan jangan menebak angka.
6. Jangan menjawab berdasarkan ingatan. Semua angka harus dari hasil query.

## 6. Yang Tidak Bisa Dijawab dari Data Ini

- Laba, margin, atau komisi Olist (tidak ada data biaya atau komisi).
- Waktu proses seller (tanggal kurir tidak andal).
- Kejadian setelah 2018-10-17 atau perkiraan masa depan.
- Demografi pelanggan (hanya state, kota, dan zip).
- Analisis isi teks review (belum tersedia di view).
