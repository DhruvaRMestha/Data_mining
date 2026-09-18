from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

HEADER = ["store_id", "business_date", "bill_no", "line_no", "product_code", "qty", "unit_price", "line_type", "ts", "source_file"]
FILE_RE = re.compile(r"SALES_(S\d{2})_(\d{8})(?:__R\d+)?\.(csv|parquet)$", re.I)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("sales"))
    parser.add_argument("--masters", type=Path, default=Path("masters.sql"))
    parser.add_argument("--finance", type=Path, default=Path("finance_monthly.csv"))
    parser.add_argument("--out", type=Path, default=Path("artifacts"))
    return parser.parse_args()


def parse_row(path: Path, row: dict[str, str], store_id: str, business_date: str) -> list[str]:
    keys = {key.strip().lstrip("\\ufeff") for key in row}
    if "item_code" in keys:
        return [store_id, business_date, row["bill_no"], row["line_no"], row["item_code"], row["quantity"], row["rate"], row["type"], datetime.strptime(row["txn_time"], "%d-%m-%Y %H:%M:%S").isoformat(sep=" "), path.name]
    if "ts" in keys:
        timestamp = row["ts"].strip()
        if timestamp.isdigit():
            timestamp = datetime.fromtimestamp(int(timestamp)).isoformat(sep=" ")
        return [store_id, business_date, row["bill_no"], row["line_no"], row["product_code"], row["qty"], row["unit_price"], row["line_type"], timestamp, path.name]
    return [store_id, business_date, row["bill_no"], row["line_no"], row["product_code"], row["qty"], row["unit_price"], row["line_type"], row["ts"], path.name]


def normalize(source: Path, target: Path) -> tuple[int, int, int]:
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = 0
    files = 0
    resends = 0
    with target.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output, delimiter="\t", lineterminator="\n")
        writer.writerow(HEADER)
        for path in sorted(source.iterdir()):
            match = FILE_RE.fullmatch(path.name)
            if not match:
                continue
            files += 1
            resends += "__R" in path.stem
            store_id, date_text, extension = match.groups()
            business_date = f"{date_text[:4]}-{date_text[4:6]}-{date_text[6:]}"
            if extension.lower() != "csv":
                raise RuntimeError(f"Parquet input support requires DuckDB scan: {path}")
            delimiter = ";" if store_id in {f"S{i:02d}" for i in range(6, 10)} else ","
            with path.open("r", newline="", encoding="utf-8-sig") as source_file:
                reader = csv.DictReader(source_file, delimiter=delimiter)
                for row in reader:
                    writer.writerow(parse_row(path, row, store_id, business_date))
                    rows += 1
    return files, resends, rows


def sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def run_duckdb(db: Path, script: str) -> None:
    subprocess.run(["duckdb", str(db)], input=script, text=True, check=True)


def main() -> None:
    args = parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    normalized = args.out / "normalized.tsv"
    files, resends, raw_rows = normalize(args.source, normalized)
    db = args.out / "analytics.duckdb"
    if db.exists():
        db.unlink()
    parquet = args.out / "lake"
    if parquet.exists():
        shutil.rmtree(parquet)
    parquet.mkdir(parents=True)
    sql = f"""
{args.masters.read_text(encoding='utf-8')}
CREATE OR REPLACE TABLE raw_sales AS SELECT * FROM read_csv('{normalized.as_posix()}', delim='\\t', header=true, columns={{store_id:'VARCHAR',business_date:'DATE',bill_no:'VARCHAR',line_no:'INTEGER',product_code:'VARCHAR',qty:'DECIMAL(18,3)',unit_price:'DECIMAL(18,2)',line_type:'VARCHAR',ts:'TIMESTAMP',source_file:'VARCHAR'}});
CREATE OR REPLACE TABLE sales_lines AS SELECT * EXCLUDE (rn) FROM (SELECT *, row_number() OVER (PARTITION BY bill_no, line_no ORDER BY source_file) AS rn FROM raw_sales) WHERE rn=1;
CREATE OR REPLACE TABLE sale_lines AS SELECT * FROM sales_lines WHERE line_type IN ('SALE','RETURN','DISCOUNT','VOID');
CREATE OR REPLACE TABLE dim_store AS SELECT * FROM stores;
CREATE OR REPLACE TABLE dim_product AS SELECT * FROM products;
CREATE OR REPLACE TABLE dim_date AS SELECT DISTINCT business_date, strftime(business_date, '%Y-%m') AS month, strftime(business_date, '%A') AS day_of_week FROM sale_lines;
CREATE OR REPLACE TABLE fact_revenue AS SELECT s.*, strftime(s.business_date, '%Y-%m') AS month, p.product_sk, p.category_id, CAST(s.qty * s.unit_price AS DECIMAL(18,2)) AS printed_revenue FROM sale_lines s LEFT JOIN products p ON p.product_code=s.product_code AND s.business_date >= p.valid_from AND s.business_date < p.valid_to;
COPY fact_revenue TO '{parquet.as_posix()}' (FORMAT PARQUET, PARTITION_BY (store_id, month), OVERWRITE_OR_IGNORE true);
COPY (SELECT (SELECT count(*) FROM raw_sales) AS raw_rows, (SELECT count(*) FROM sales_lines) AS canonical_rows, (SELECT count(*) FROM fact_revenue) AS revenue_rows) TO '{(args.out / 'stats.csv').as_posix()}' (HEADER, DELIMITER ',');
CREATE OR REPLACE TABLE idempotency_results (run INTEGER, row_count BIGINT, checksum VARCHAR);
"""
    for run in range(1, 4):
        sql += f"""
CREATE OR REPLACE TABLE sales_lines AS SELECT * EXCLUDE (rn) FROM (SELECT *, row_number() OVER (PARTITION BY bill_no, line_no ORDER BY source_file) AS rn FROM raw_sales) WHERE rn=1;
CREATE OR REPLACE TABLE fact_revenue AS SELECT s.*, strftime(s.business_date, '%Y-%m') AS month, p.product_sk, p.category_id, CAST(s.qty * s.unit_price AS DECIMAL(18,2)) AS printed_revenue FROM sales_lines s LEFT JOIN products p ON p.product_code=s.product_code AND s.business_date >= p.valid_from AND s.business_date < p.valid_to WHERE s.line_type IN ('SALE','RETURN','DISCOUNT','VOID');
INSERT INTO idempotency_results SELECT {run}, count(*), md5(string_agg(bill_no || '|' || line_no::VARCHAR || '|' || printf('%.2f', printed_revenue), ',' ORDER BY bill_no, line_no)) FROM fact_revenue;
"""
    sql += f"COPY (SELECT * FROM idempotency_results ORDER BY run) TO '{(args.out / 'idempotency.csv').as_posix()}' (HEADER, DELIMITER ','); COPY (SELECT month, round(sum(printed_revenue), 2) AS pipeline FROM fact_revenue GROUP BY month ORDER BY month) TO '{(args.out / 'pipeline_monthly.csv').as_posix()}' (HEADER, DELIMITER ',');"
    run_duckdb(db, sql)
    stats = next(csv.DictReader((args.out / "stats.csv").open(encoding="utf-8")))
    runs = [dict(row) for row in csv.DictReader((args.out / "idempotency.csv").open(encoding="utf-8"))]
    finance = {row["month"]: float(row["revenue_inr"]) for row in csv.DictReader(args.finance.open(encoding="utf-8"))}
    recon = []
    for row in csv.DictReader((args.out / "pipeline_monthly.csv").open(encoding="utf-8")):
        pipeline = float(row["pipeline"])
        recon.append({"month": row["month"], "pipeline": pipeline, "finance": finance[row["month"]], "difference": round(pipeline - finance[row["month"]], 2)})
    result = {"source_files": files, "resend_files": resends, "raw_rows": raw_rows, "raw_rows_loaded": raw_rows, "canonical_rows": int(stats["canonical_rows"]), "revenue_rows": int(stats["revenue_rows"]), "idempotency": runs, "reconciliation": recon, "partition_bytes": sum(p.stat().st_size for p in parquet.rglob("*.parquet")), "partition_files": len(list(parquet.rglob("*.parquet")))}
    (args.out / "run.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
