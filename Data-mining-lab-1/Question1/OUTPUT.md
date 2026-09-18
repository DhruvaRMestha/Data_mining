# Annapurna Stores Output

## How to reproduce

From the workspace root:

```powershell
docker compose up -d
cd data
python solution/loader.py
```

The loader writes its evidence to `data/artifacts/`. The key files are
`stats.csv` for row counts, `idempotency.csv` for the three-run proof,
`pipeline_monthly.csv` for reconciliation, and `run.json` for the combined
machine-readable result. Run `solution/queries.sql` in DuckDB after the
services are up for the live MinIO/PostgreSQL federation query.

The local service endpoints are MinIO at `http://localhost:9000`, the MinIO
console at `http://localhost:9001`, and PostgreSQL at `localhost:5432`.
The Compose credentials are `minio` / `minio12345` for MinIO and
`annapurna` / `annapurna` for PostgreSQL.

## (a) Platform and layout

The platform definition is in `docker-compose.yml`: PostgreSQL for the supplied
masters, MinIO as the object store, and DuckDB as the analytical engine.
PostgreSQL and MinIO are running in Docker Desktop. The Compose initializer
creates the `annapurna` bucket and uploads the lake under the `lake/` prefix.

The loader still produced the object-store-ready lake at
`artifacts/lake/store_id=Sxx/month=YYYY-MM/`. The partition key is the stable
business dimensions most likely to be filtered together: store and business
month. The source file name, not transaction timestamp, supplies the business
date.

Observed layout evidence for S01 October 2024:

| layout | files potentially opened | bytes potentially opened |
|---|---:|---:|
| store/month Parquet partition | 1 | 161,924 |
| one-folder raw source | 4,457 | 68,706,877 |

The generated lake has 144 Parquet files and 15,249,332 bytes. A query for one
store-month can be pruned to one partition instead of scanning every source
file.

## (b) Idempotency

`artifacts/idempotency.csv` is the captured three-run proof:

| run | row count | checksum |
|---:|---:|---|
| 1 | 789,516 | `fb08fdc42bb2e68ec61c634d4547a577` |
| 2 | 789,516 | `fb08fdc42bb2e68ec61c634d4547a577` |
| 3 | 789,516 | `fb08fdc42bb2e68ec61c634d4547a577` |

The loader reads all 4,457 files, including 68 resends, and keeps one row per
`(bill_no, line_no)` using a windowed deduplication step. It does not replace a
complete day with a resend, because resends can be partial.

## (c) Dashboard model

The analytical model is a small star schema:

- `dim_store` references `stores` once per store.
- `dim_product` references `products` once per real product and joins to
  `product_categories`.
- `dim_date` provides business date, month, and day of week.
- `fact_revenue` stores the line grain and foreign keys, not repeated store or
  category attributes.

Only `SALE`, `RETURN`, `DISCOUNT`, and `VOID` enter revenue. `TAX` and `TENDER`
are excluded. `VOID` lines are retained so cancelled bills net to zero. Product
identity uses the product-code validity interval, not product code alone:
`business_date >= valid_from AND business_date < valid_to`.

The source counts are 1,137,585 raw rows, 1,120,924 deduplicated rows, and
789,516 revenue rows. This prevents the common near-double October result from
counting `TENDER` as another sale total.

## (d) Historical prices

`solution/queries.sql` uses one period-parameterized query. The price join is:

```sql
pr.effective_from <= period_end
AND pr.effective_to > period_start
```

The same DuckDB logic returned these authoritative price examples:

| product | March 2024 | October 2024 |
|---|---:|---:|
| Thums Up Mango Juice 250g (`P100005`) | 103.45 | 114.37 |
| Vim Dishwash Bar 1kg (`P100007`) | 129.42 | 155.06 |
| Sunfeast Cookies 150g (`P100019`) | 62.95 | 74.65 |

No query-code change is required beyond the reporting-period literals.

## (e) Federated query

`solution/queries.sql` joins MinIO Parquet to `pg.products` and
`pg.price_revisions` without copying either side. The live March query returned
70,409 joined rows and ₹47,173,156.27 at March-effective prices. `EXPLAIN
ANALYZE` showed `READ_PARQUET` against `s3://annapurna/lake` with 188 S3 GETs
and 144 files read; the dimension and price data appeared as table scans from
the PostgreSQL attachment. DuckDB applied the business-date and line-type
filters in the Parquet scan, and the product-validity and price-period joins
against PostgreSQL data.

## (f) Reconciliation

The pipeline matches finance in January, February, April, May, June, August,
September, October, and November.

| month | pipeline | finance | difference | cause and action |
|---|---:|---:|---:|---|
| 2024-03 | 41,971,649.09 | 42,457,899.09 | -486,250.00 | Definition/source scope: finance includes the institutional invoice outside the tills. Take the finance number back as a documented non-till adjustment. |
| 2024-07 | 40,295,160.11 | 40,527,291.81 | -232,131.70 | Source data: S07 exports for July 9-11 are missing. Take the finance number back, tagged as a manually supplied source gap. |
| 2024-12 | 50,745,259.48 | 50,745,209.00 | +50.48 | Definition: finance rounds each bill to whole rupees before summing. Take finance's closed number for the finance view and label the pipeline as line-precision revenue. |

These are not pipeline bugs. October reconciles exactly at 56,359,195.92.
