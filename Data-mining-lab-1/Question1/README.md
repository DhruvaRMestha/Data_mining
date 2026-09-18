# Annapurna sales platform

Run from the `data` folder:

```powershell
python solution/loader.py
```

Start the PostgreSQL and MinIO services from the workspace root with
`docker compose up -d`.

The loader normalizes the three vendor dialects, trusts the business date in
filenames, deduplicates at `(bill_no, line_no)`, excludes `TAX` and `TENDER`,
retains `VOID` lines so cancelled bills net to zero, resolves products by
`product_code` plus the sale-date validity interval, and writes the analytical
fact table as Parquet under `artifacts/lake/store_id=Sxx/month=YYYY-MM/`.

The stack is:

- MinIO: object store on port 9000; upload `artifacts/lake` to bucket
  `annapurna` automatically. The MinIO API is `http://localhost:9000` and the
  console is `http://localhost:9001`; credentials are `minio` / `minio12345`.
- PostgreSQL: master data on port 5432, initialized from `data/masters.sql`.
- DuckDB: analytical engine used by the reproducible loader and federated query.

The partition key is `(store_id, month)`. A one-store/one-month query can
prune to that store-month partition instead of scanning all 4,457 source files.
`run.json` records exact row counts, checksums, partition files/bytes, and the
monthly reconciliation.

The star schema is `dim_store`, `dim_product`, `dim_date`, and `fact_revenue`.
Dimensions are joined by surrogate/interval keys; store and category attributes
are not repeated in the fact rows.

`queries.sql` contains the period-parameterized historical-price query and the
`EXPLAIN ANALYZE` evidence commands for the DuckDB/PostgreSQL federation.

## Visual dashboard

The dashboard is a separate local web page under `data/dashboard/`. Start it
from the workspace root with:

```powershell
Push-Location data/dashboard
python -m http.server 5173
Pop-Location
```

Open <http://localhost:5173/>. It provides filters for month, store, category,
and weekday, plus revenue metrics, a monthly trend graph, and breakdowns by
store, category, and weekday. It reads the generated `data.json` and
`finance.json` aggregates and does not expose database credentials to the
browser.

The complete submission narrative and evidence summary is in `OUTPUT.md`.

## Verification queries by subquestion

Run these from the `data` folder after `python solution/loader.py` has
completed. The artifact queries use DuckDB's CSV and JSON readers; the
federated queries are also reproduced in `queries.sql`.

### (a) Platform, partitioning, and pruning

```sql
SELECT * FROM read_csv_auto('artifacts/stats.csv');
SELECT * FROM read_json_auto('artifacts/run.json');
```

Confirm the physical layout and pruning inputs with:

```powershell
Get-ChildItem artifacts/lake -Recurse -Filter *.parquet |
  Select-Object FullName, Length
```

### (b) Idempotency and deduplication

```sql
SELECT run, row_count, checksum
FROM read_csv_auto('artifacts/idempotency.csv')
ORDER BY run;

SELECT COUNT(*) AS revenue_rows,
       COUNT(DISTINCT bill_no || ':' || line_no) AS distinct_business_lines
FROM read_parquet('artifacts/lake/**/*.parquet')
WHERE line_type IN ('SALE', 'RETURN', 'DISCOUNT', 'VOID');
```

All three idempotency rows must have the same count and checksum.

### (c) Dashboard model and revenue scope

```sql
SELECT line_type, COUNT(*) AS rows, SUM(printed_revenue) AS amount
FROM read_parquet('artifacts/lake/**/*.parquet')
GROUP BY line_type
ORDER BY line_type;

SELECT COUNT(*) AS fact_rows,
       COUNT(DISTINCT store_id) AS stores,
       COUNT(DISTINCT product_code) AS products
FROM read_parquet('artifacts/lake/**/*.parquet')
WHERE line_type IN ('SALE', 'RETURN', 'DISCOUNT', 'VOID');
```

The first query verifies that `TAX` and `TENDER` are outside revenue while
`VOID` remains visible.

### (d) Historical prices

```sql
ATTACH 'dbname=annapurna user=annapurna password=annapurna host=localhost port=5432'
  AS pg (TYPE POSTGRES, READ_ONLY);

SELECT p.product_code, p.product_name, pr.selling_price,
       pr.effective_from, pr.effective_to
FROM pg.products p
JOIN pg.price_revisions pr ON pr.product_sk = p.product_sk
WHERE p.product_code IN ('P100005', 'P100007', 'P100019')
  AND pr.effective_from <= DATE '2024-03-31'
  AND pr.effective_to > DATE '2024-03-01'
ORDER BY p.product_code;
```

### (e) Federated query and query plan

```sql
INSTALL postgres; LOAD postgres;
INSTALL httpfs; LOAD httpfs;
CREATE SECRET minio (TYPE S3, KEY_ID 'minio', SECRET 'minio12345',
  REGION 'us-east-1', ENDPOINT 'localhost:9000', URL_STYLE 'path', USE_SSL false);

EXPLAIN ANALYZE
SELECT COUNT(*) AS joined_rows,
       SUM(f.qty * pr.selling_price) AS revenue_at_period_price
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
  AND f.line_type IN ('SALE', 'RETURN', 'DISCOUNT', 'VOID');
```

### (f) Finance reconciliation

```sql
WITH pipeline AS (
  SELECT strftime(business_date, '%Y-%m') AS month,
         SUM(printed_revenue) AS pipeline_revenue
  FROM read_parquet('artifacts/lake/**/*.parquet')
  WHERE line_type IN ('SALE', 'RETURN', 'DISCOUNT', 'VOID')
  GROUP BY 1
), finance AS (
  SELECT month, revenue_inr AS finance_revenue
  FROM read_csv_auto('../finance_monthly.csv')
)
SELECT p.month, pipeline_revenue, finance_revenue,
       pipeline_revenue - finance_revenue AS difference
FROM pipeline p
JOIN finance f USING (month)
ORDER BY p.month;
```
