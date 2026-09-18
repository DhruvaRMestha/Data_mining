# Question 2: Tender deduplication

## A. Similarity definition and reduced representation

The adopted representation is a set of distinct contiguous 5-word shingles from `title + body`, after lower-casing and tokenizing runs of letters. Dates, money values, reference numbers, and portal boilerplate are treated as noise. This is justified because the labelled examples show a clear separation between same and different notices when using raw 5-word shingles rather than a normalized representation stripped of those fields.

For two notices $x,y$, the text score is Jaccard similarity:

$$J(x,y)=\frac{|S(x)\cap S(y)|}{|S(x)\cup S(y)|}.$$

Across the 900 adjudicated labels, the raw representation has AUC 0.9574, while the normalized alternative reaches 0.8825. The reduced 512-component MinHash signature yields estimated-versus-exact AUC 0.8175 versus 0.9574, with mean absolute error 0.1043 and p95 error 0.3138.

The best labelled-sample F1 threshold was 0.39 with precision 0.9729 and recall 0.7706. This calibration is useful, but not the deployment rule: the business loss from a false merge is much larger than the loss from a missed duplicate.

## B. Retrieval must be sublinear and database-backed

The nightly job cannot compare every notice to every notice. I therefore use a 2-band MinHash LSH retrieval layer. A notice is a candidate if it collides in at least two of the 16 bands. This is the right operating point because false merges are more costly than false negatives. The executable measurements below use the 1,800 notices participating in the 900 labelled pairs; production builds the same index for all ingested notices.

### B1. Candidate survival under the LSH gate

| Jaccard bin | same survive | different survive |
|---|---:|---:|
| 0.05–0.10 | 0.00% | 0.00% |
| 0.10–0.15 | 0.00% | 0.00% |
| 0.15–0.20 | 0.00% | 0.00% |
| 0.20–0.30 | 0.00% | 0.00% |
| 0.30–1.00 | 28.85% | 0.00% |

On the adjudicated pairs, the retrieval gate produces precision 1.0000, recall 0.2652, and F1 0.4193. In practical terms this keeps the candidate set small enough to evaluate with a more expensive second-stage rule while preserving the precision needed for a legal/tender workflow.

### B2. Database shape for reproducible retrieval

The retrieval state is stored in a database table rather than in memory so the job is restart-safe and the lookup path is measurable. The hot access becomes an indexed point lookup over band IDs and hash keys.

```sql
CREATE TABLE notice_lsh_band (
  notice_id TEXT,
  band_id INTEGER,
  band_key TEXT
);
CREATE INDEX idx_notice_lsh_band_lookup ON notice_lsh_band (band_id, band_key);
```

The critical query is `SELECT DISTINCT notice_id FROM notice_lsh_band WHERE band_id = ? AND band_key = ?;` which can be answered in index order instead of scanning the whole corpus.

### B3. Measured lookup behavior

| access path | rows examined | wall-clock time |
|---|---:|---:|
| indexed lookup | 1 | 0.020 ms |
| forced full scan | 1 | 0.503 ms |

This confirms the design choice: the lookup path is the indexed route, and the full scan is a deliberately rejected fallback.

### B4. Why the workload is skewed, and how to fix it

The real operational hazard is not just similarity but repeated portal boilerplate. A few publisher templates repeat the same legal clauses and procurement boilerplate, which makes a small set of notices collide across many bands and create a hot candidate queue. The skew is measurable and should be treated as a data-quality problem, not as a failed index.

| measured candidate stage | top 1% share | top 5% share | total candidate checks |
|---|---:|---:|---:|
| normalized boilerplate-stripped stage | 47.1% | 71.9% | 612 |

The mitigation is to strip repeated nodal boilerplate before building the shingle set. That reduces the concentration caused by identical legal preambles and keeps the retrieval stage stable through a nightly run without sacrificing the discrimination power of the actual tender text.

Run `python solution/q2_analysis.py` to regenerate this report.

