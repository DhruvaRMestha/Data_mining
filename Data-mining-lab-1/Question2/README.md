# Question 2 solution

Run from the `data_2` folder:

```powershell
python solution/q2_analysis.py
```

The script reads `notices/` and `labelled_pairs.csv`, computes the labelled
similarity measurements, builds the LSH candidate index, benchmarks the
SQLite lookup path, and rewrites `solution/q2_report.md`. It also creates
`notice_lsh.sqlite` in this folder.

The executable measurements use the notices participating in the 900 labelled
pairs so the verification run is bounded and reproducible. Production ingestion
applies the same signature and LSH functions to all 12,000 notices.

## Verification queries by subquestion

### (A) Similarity and reduced representation

The labelled-pair verification is run with:

```powershell
python solution/q2_analysis.py
```

The report records raw and normalized 5-word-shingle AUC, exact versus
512-component reduced-signature AUC, mean/p95 error, and the labelled F1
threshold. To inspect the adjudication balance directly:

```powershell
Import-Csv labelled_pairs.csv |
  Group-Object label |
  Select-Object Name, Count
```

### (B1) Sublinear candidate retrieval

The script verifies candidate survival by Jaccard bin and applies the LSH rule:

```python
candidate = (
    other_notice_id in candidate_set_for_notice(
        notice_id, signatures, lsh_index, min_bands=2
    )
)
```

The resulting survival curve and precision/recall/F1 are written to
`q2_report.md`. This verifies that candidate retrieval happens before any
expensive pairwise decision.

### (B2) Database-backed access path

Inspect the persisted lookup table with SQLite:

```sql
.tables
.schema notice_lsh_band
SELECT band_id, band_key, COUNT(*) AS notices
FROM notice_lsh_band
GROUP BY band_id, band_key
ORDER BY notices DESC
LIMIT 10;
```

The indexed verification query is:

```sql
SELECT DISTINCT notice_id
FROM notice_lsh_band
WHERE band_id = ? AND band_key = ?;
```

The script also runs:

```sql
EXPLAIN QUERY PLAN
SELECT DISTINCT notice_id
FROM notice_lsh_band
WHERE band_id = ? AND band_key = ?;
```

and compares it with the same predicate against an unindexed copy named
`notice_lsh_band_scan`.

### (B3) Workload skew and mitigation

The workload query used by the script is equivalent to:

```sql
SELECT notice_id, COUNT(*) AS candidate_count
FROM notice_lsh_band
GROUP BY notice_id
ORDER BY candidate_count DESC;
```

The report aggregates that result into the top 1% and top 5% shares. The
candidate-stage input strips repeated nodal boilerplate, dates, money values,
and portal reference numbers before shingling. Compare the measured
concentration in `q2_report.md` with `portal_profiles.md`, which documents the
P001-P006 boilerplate source.

## Report

The complete similarity, retrieval, database, and workload evidence is in
`q2_report.md`.
