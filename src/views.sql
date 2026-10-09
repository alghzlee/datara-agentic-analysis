-- Views exposed to the agent. The contract for each view is in docs/schema.md.
-- Keep every statement separated by a semicolon at its end, and do not put
-- semicolons inside comments (build_views.py splits the file on semicolons).

-- 1. order_items_enriched: 1 row per order item (all order statuses)
-- LEFT JOINs everywhere so the row count always equals order_items (112,650).
CREATE OR REPLACE VIEW order_items_enriched AS
SELECT
    i.order_id,
    i.order_item_id,
    o.order_status,
    o.order_purchase_timestamp AS purchase_ts,
    c.customer_unique_id,
    c.customer_state,
    i.seller_id,
    s.seller_state,
    i.product_id,
    COALESCE(t.product_category_name_english, p.product_category_name, 'unknown') AS category,
    i.price,
    i.freight_value,
    i.price + i.freight_value AS item_total
FROM order_items i
LEFT JOIN orders o ON o.order_id = i.order_id
LEFT JOIN customers c ON c.customer_id = o.customer_id
LEFT JOIN sellers s ON s.seller_id = i.seller_id
LEFT JOIN products p ON p.product_id = i.product_id
LEFT JOIN product_category_name_translation t ON t.product_category_name = p.product_category_name;

-- 2. orders_clean: 1 row per order (all 99,441 orders)
-- Items, payments and reviews are aggregated to 1 row per order BEFORE joining,
-- so no join can multiply rows. Carrier dates are intentionally not exposed.
CREATE OR REPLACE VIEW orders_clean AS
WITH item_agg AS (
    SELECT
        order_id,
        COUNT(*) AS n_items,
        SUM(price) AS items_revenue,
        SUM(freight_value) AS freight_total
    FROM order_items
    GROUP BY order_id
),
pay_agg AS (
    SELECT
        order_id,
        SUM(payment_value) AS paid_total
    FROM order_payments
    GROUP BY order_id
),
review_latest AS (
    SELECT order_id, review_score
    FROM order_reviews
    QUALIFY ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY review_answer_timestamp DESC) = 1
)
SELECT
    o.order_id,
    c.customer_unique_id,
    c.customer_state,
    o.order_status,
    o.order_purchase_timestamp AS purchase_ts,
    o.order_delivered_customer_date AS delivered_customer_ts,
    CAST(o.order_estimated_delivery_date AS DATE) AS estimated_delivery_date,
    COALESCE(ia.n_items, 0) AS n_items,
    ia.items_revenue,
    ia.freight_total,
    pa.paid_total,
    rl.review_score,
    CASE
        WHEN o.order_status = 'delivered' AND o.order_delivered_customer_date IS NOT NULL
        THEN date_diff('second', o.order_purchase_timestamp, o.order_delivered_customer_date) / 86400.0
    END AS delivery_days,
    CASE
        WHEN o.order_status = 'delivered' AND o.order_delivered_customer_date IS NOT NULL
        THEN CAST(o.order_delivered_customer_date AS DATE) > CAST(o.order_estimated_delivery_date AS DATE)
    END AS is_late
FROM orders o
LEFT JOIN customers c ON c.customer_id = o.customer_id
LEFT JOIN item_agg ia ON ia.order_id = o.order_id
LEFT JOIN pay_agg pa ON pa.order_id = o.order_id
LEFT JOIN review_latest rl ON rl.order_id = o.order_id;

-- 3. payments_clean: 1 row per payment record, without payment_type not_defined
CREATE OR REPLACE VIEW payments_clean AS
SELECT
    order_id,
    payment_sequential,
    payment_type,
    payment_installments,
    payment_value
FROM order_payments
WHERE payment_type <> 'not_defined';

-- 4. geo_per_zip: 1 row per zip code prefix
-- Coordinates: median of the rows inside a rough Brazil bounding box
-- (lat -34 to 6, lng -74 to -28). State: most frequent state for the zip.
CREATE OR REPLACE VIEW geo_per_zip AS
WITH valid AS (
    SELECT *
    FROM geolocation
    WHERE geolocation_lat BETWEEN -34 AND 6
      AND geolocation_lng BETWEEN -74 AND -28
),
coords AS (
    SELECT
        geolocation_zip_code_prefix AS zip_code_prefix,
        median(geolocation_lat) AS lat,
        median(geolocation_lng) AS lng
    FROM valid
    GROUP BY 1
),
state_counts AS (
    SELECT
        geolocation_zip_code_prefix AS zip_code_prefix,
        geolocation_state AS state,
        COUNT(*) AS n
    FROM geolocation
    GROUP BY 1, 2
),
state_mode AS (
    SELECT
        zip_code_prefix,
        arg_max(state, n) AS state,
        COUNT(*) AS n_states
    FROM state_counts
    GROUP BY 1
)
SELECT
    sm.zip_code_prefix,
    c.lat,
    c.lng,
    sm.state,
    sm.n_states > 1 AS multi_state
FROM state_mode sm
LEFT JOIN coords c ON c.zip_code_prefix = sm.zip_code_prefix;
