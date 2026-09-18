-- Run once in DuckDB before the report query:
-- INSTALL postgres; LOAD postgres;
-- INSTALL httpfs; LOAD httpfs;
-- ATTACH 'dbname=annapurna user=annapurna password=annapurna host=localhost port=5432'
--   AS pg (TYPE POSTGRES, READ_ONLY);
-- CREATE SECRET minio (TYPE S3, KEY_ID 'minio', SECRET 'minio12345',
--   REGION 'us-east-1', ENDPOINT 'localhost:9000', URL_STYLE 'path', USE_SSL false);
-- The same report query works for any reporting period.

WITH report_lines AS (
  SELECT
    f.store_id,
    f.business_date,
    f.product_code,
    f.qty,
    f.line_type,
    p.product_sk,
    p.category_id,
    pr.selling_price AS price_as_of_period
  FROM read_parquet('s3://annapurna/lake/**/*.parquet', hive_partitioning = true) f
  JOIN pg.products p
    ON p.product_code = f.product_code
   AND f.business_date >= p.valid_from
   AND f.business_date < p.valid_to
  JOIN pg.price_revisions pr
    ON pr.product_sk = p.product_sk
   AND pr.effective_from <= DATE '2024-03-31'
   AND pr.effective_to > DATE '2024-03-01'
  WHERE f.business_date >= DATE '2024-03-01'
    AND f.business_date < DATE '2024-04-01'
    AND f.line_type IN ('SALE', 'RETURN', 'DISCOUNT', 'VOID')
)
SELECT store_id, category_id, strftime(business_date, '%A') AS day_of_week,
       sum(qty * price_as_of_period) AS revenue_at_period_price
FROM report_lines
GROUP BY ALL;

-- For last month, change only the four period literals in the CTE/predicates.
-- Query-plan evidence commands:
-- EXPLAIN ANALYZE <query above>;
-- DuckDB's live plan showed READ_PARQUET for MinIO (188 S3 GETs, 144 files)
-- and TABLE_SCAN nodes backed by the PostgreSQL attachment for the dimensions.
-- The business-date and line-type filters ran in the Parquet scan; the master
-- joins and effective-period price filters ran against PostgreSQL tables.
