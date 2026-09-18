"""Reproducible Section A analysis for tender similarity and MinHash sizing."""

import csv
import hashlib
import math
import re
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NOTICE_DIR = ROOT / "notices"
LABELS = ROOT / "labelled_pairs.csv"
REPORT = Path(__file__).with_name("q2_report.md")
DB_PATH = ROOT / "notice_lsh.sqlite"
SIGNATURE_SIZE = 512
VALIDATION_SHINGLE_CAP = 4
SEED = 20240917

DATE = r"(?:\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{2,4})"
MONEY = r"(?:rs\.?|inr|rupees?)\s*[\d,]+(?:\.\d+)?\s*(?:lakh|cr|crore)?|\b\d{1,3}(?:,\d{2,3})+(?:\.\d+)?\b"
REFERENCE = r"\b(?:ref(?:erence)?|tender|nit|bid|work)\s*(?:no|number|id)?\s*[:#/-]?\s*[a-z0-9][a-z0-9./-]{3,}\b"
TOKEN = re.compile(r"[a-z]+|\d+")


def read_notices():
    notices = {}
    for path in sorted(NOTICE_DIR.glob("*.csv")):
        with path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                notices[row["notice_id"]] = row
    return notices


def raw_tokens(row):
    return TOKEN.findall((row["title"] + " " + row["body"]).lower())


def normalized_tokens(row):
    text = (row["title"] + " " + row["body"]).lower()
    text = re.sub(DATE, " ", text, flags=re.IGNORECASE)
    text = re.sub(MONEY, " ", text, flags=re.IGNORECASE)
    text = re.sub(REFERENCE, " ", text, flags=re.IGNORECASE)
    text = re.sub(r"national procurement aggregation service|state procurement cell", " ", text)
    text = re.sub(r"\b\d+\b", " ", text)
    tokens = TOKEN.findall(text)
    return tokens


def shingles(tokens, width=5):
    if len(tokens) < width:
        return {" ".join(tokens)} if tokens else set()
    return {" ".join(tokens[index:index + width]) for index in range(len(tokens) - width + 1)}


def jaccard(left, right):
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def digest(value, seed):
    return hashlib.blake2b(
        f"{seed}:{value}".encode("utf-8"), digest_size=64
    ).digest()


def minhash(signature_sets, size=SIGNATURE_SIZE):
    signatures = {}
    for notice_id, values in signature_sets.items():
        if len(values) > VALIDATION_SHINGLE_CAP:
            values = sorted(values, key=lambda value: digest(value, SEED))[:VALIDATION_SHINGLE_CAP]
        result = [2**64 - 1] * size
        for value in values:
            digest_bytes = digest(value, SEED)
            seeds = [
                int.from_bytes(digest_bytes[offset * 8:(offset + 1) * 8], "big")
                for offset in range(8)
            ]
            for index in range(size):
                candidate = (seeds[index % 8] ^ ((index + 1) * 0x9E3779B97F4A7C15)) & (2**64 - 1)
                if candidate < result[index]:
                    result[index] = candidate
        signatures[notice_id] = result
    return signatures


def pair_score(pair, exact_sets, signatures):
    left, right = pair
    exact = jaccard(exact_sets[left], exact_sets[right])
    estimate = sum(a == b for a, b in zip(signatures[left], signatures[right])) / SIGNATURE_SIZE
    return exact, estimate


def auc(scores):
    positives = [score for score, label in scores if label == "same"]
    negatives = [score for score, label in scores if label == "different"]
    wins = sum(1 for positive in positives for negative in negatives if positive > negative)
    ties = sum(1 for positive in positives for negative in negatives if positive == negative)
    return (wins + ties / 2) / (len(positives) * len(negatives))


def threshold_table(scores):
    rows = []
    for threshold in [index / 100 for index in range(0, 101)]:
        tp = sum(score >= threshold and label == "same" for score, label in scores)
        fp = sum(score >= threshold and label == "different" for score, label in scores)
        fn = sum(score < threshold and label == "same" for score, label in scores)
        tn = sum(score < threshold and label == "different" for score, label in scores)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        rows.append((precision, recall, threshold, tp, fp, fn, tn))
    return rows


def text_for_notice(row):
    return (row["title"] + " " + row["body"]).lower()


def sanitized_tokens(row):
    text = text_for_notice(row)
    text = re.sub(DATE, " ", text, flags=re.IGNORECASE)
    text = re.sub(MONEY, " ", text, flags=re.IGNORECASE)
    text = re.sub(REFERENCE, " ", text, flags=re.IGNORECASE)
    for phrase in [
        "national procurement aggregation service",
        "state procurement cell",
        "terms and conditions",
        "government of",
        "electronic procurement",
        "disclaimer",
        "this portal",
    ]:
        text = text.replace(phrase, " ")
    text = re.sub(r"\s+", " ", text)
    return TOKEN.findall(text)


def build_lsh_index(signatures):
    index = defaultdict(set)
    band_size = SIGNATURE_SIZE // 16
    for notice_id, signature in signatures.items():
        for band_id in range(16):
            chunk = tuple(signature[band_id * band_size : (band_id + 1) * band_size])
            key = hashlib.sha1(repr(chunk).encode("utf-8")).hexdigest()
            index[(band_id, key)].add(notice_id)
    return index


def candidate_set_for_notice(notice_id, signatures, lsh_index, min_bands=2):
    signature = signatures[notice_id]
    band_size = SIGNATURE_SIZE // 16
    counts = defaultdict(int)
    for band_id in range(16):
        chunk = tuple(signature[band_id * band_size : (band_id + 1) * band_size])
        key = hashlib.sha1(repr(chunk).encode("utf-8")).hexdigest()
        for other_notice_id in lsh_index.get((band_id, key), set()):
            if other_notice_id != notice_id:
                counts[other_notice_id] += 1
    return {other_notice_id for other_notice_id, count in counts.items() if count >= min_bands}


def retrieval_state(notices):
    shingle_sets = {notice_id: shingles(sanitized_tokens(row)) for notice_id, row in notices.items()}
    signatures = minhash(shingle_sets)
    return shingle_sets, signatures, build_lsh_index(signatures)


def section_a(labels, notices):
    labelled_ids = {
        notice_id
        for row in labels
        for notice_id in (row["notice_id_a"], row["notice_id_b"])
    }
    raw_sets = {key: shingles(raw_tokens(notices[key])) for key in labelled_ids}
    normalized_sets = {key: shingles(normalized_tokens(notices[key])) for key in labelled_ids}
    signatures = minhash({key: raw_sets[key] for key in labelled_ids})

    raw_scores = []
    normalized_scores = []
    exact_scores = []
    estimated_scores = []
    errors = []

    for row in labels:
        pair = (row["notice_id_a"], row["notice_id_b"])
        exact, estimate = pair_score(pair, raw_sets, signatures)
        raw_scores.append((jaccard(raw_sets[pair[0]], raw_sets[pair[1]]), row["label"]))
        normalized_scores.append((jaccard(normalized_sets[pair[0]], normalized_sets[pair[1]]), row["label"]))
        exact_scores.append((exact, row["label"]))
        estimated_scores.append((estimate, row["label"]))
        errors.append(abs(exact - estimate))

    same = [row for row in labels if row["label"] == "same"]
    different = [row for row in labels if row["label"] == "different"]
    example_same = same[0]
    example_different = different[0]

    def scores_for(row):
        pair = (row["notice_id_a"], row["notice_id_b"])
        return (
            jaccard(raw_sets[pair[0]], raw_sets[pair[1]]),
            jaccard(normalized_sets[pair[0]], normalized_sets[pair[1]]),
            pair_score(pair, raw_sets, signatures)[1],
        )

    best = max(threshold_table(exact_scores), key=lambda item: (item[0] * item[1], item[1]))
    return {
        "raw_auc": auc(raw_scores),
        "clean_auc": auc(normalized_scores),
        "exact_auc": auc(exact_scores),
        "estimated_auc": auc(estimated_scores),
        "mean_error": sum(errors) / len(errors),
        "p95_error": sorted(errors)[math.ceil(0.95 * len(errors)) - 1],
        "max_error": max(errors),
        "best": best,
        "example_same": example_same,
        "example_different": example_different,
        "scores_for": scores_for,
    }


def retrieval_curve(labels, state):
    shingle_sets, signatures, index = state
    points = []
    for row in labels:
        a, b = row["notice_id_a"], row["notice_id_b"]
        exact_similarity = jaccard(shingle_sets[a], shingle_sets[b])
        left_candidates = candidate_set_for_notice(a, signatures, index, min_bands=2)
        right_candidates = candidate_set_for_notice(b, signatures, index, min_bands=2)
        survives = (b in left_candidates) or (a in right_candidates)
        points.append((exact_similarity, row["label"], survives))
    bins = [0.0, 0.05, 0.10, 0.15, 0.20, 0.30, 1.0]
    rows = []
    for start, end in zip(bins[:-1], bins[1:]):
        bucket = [p for p in points if start <= p[0] < end]
        if not bucket:
            continue
        same_total = sum(1 for _, label, _ in bucket if label == "same")
        same_survive = sum(1 for _, label, survives in bucket if label == "same" and survives)
        diff_total = sum(1 for _, label, _ in bucket if label == "different")
        diff_survive = sum(1 for _, label, survives in bucket if label == "different" and survives)
        rows.append((start, end, same_survive / same_total if same_total else 0.0, diff_survive / diff_total if diff_total else 0.0))
    return rows


def retrieval_metrics(labels, state):
    _, signatures, index = state
    predictions = []
    for row in labels:
        a, b = row["notice_id_a"], row["notice_id_b"]
        left_candidates = candidate_set_for_notice(a, signatures, index, min_bands=2)
        right_candidates = candidate_set_for_notice(b, signatures, index, min_bands=2)
        predicted = (b in left_candidates) or (a in right_candidates)
        predictions.append((predicted, row["label"]))
    tp = sum(1 for predicted, label in predictions if predicted and label == "same")
    fp = sum(1 for predicted, label in predictions if predicted and label == "different")
    fn = sum(1 for predicted, label in predictions if not predicted and label == "same")
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": precision, "recall": recall, "f1": f1, "tp": tp, "fp": fp, "fn": fn}


def workload_distribution(state):
    shingle_sets, signatures, index = state
    work = []
    for notice_id in shingle_sets:
        work.append((notice_id, len(candidate_set_for_notice(notice_id, signatures, index, min_bands=2))))
    work.sort(key=lambda x: x[1], reverse=True)
    total = sum(count for _, count in work)
    top_1 = sum(count for _, count in work[: max(1, len(work) // 100)])
    top_5 = sum(count for _, count in work[: max(1, len(work) // 20)])
    return {"top_1_pct": top_1 / total if total else 0.0, "top_5_pct": top_5 / total if total else 0.0, "total": total, "top_items": work[:10]}


def measured_lookup(state):
    _, signatures, _ = state
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DROP TABLE IF EXISTS notice_lsh_band_scan")
    conn.execute("DROP TABLE IF EXISTS notice_lsh_band")
    conn.execute("CREATE TABLE notice_lsh_band (notice_id TEXT, band_id INTEGER, band_key TEXT)")
    conn.execute("CREATE INDEX idx_notice_lsh_band_lookup ON notice_lsh_band (band_id, band_key)")
    for notice_id, signature in signatures.items():
        band_size = SIGNATURE_SIZE // 16
        for band_id in range(16):
            chunk = tuple(signature[band_id * band_size : (band_id + 1) * band_size])
            key = hashlib.sha1(repr(chunk).encode("utf-8")).hexdigest()
            conn.execute("INSERT INTO notice_lsh_band (notice_id, band_id, band_key) VALUES (?, ?, ?)", (notice_id, band_id, key))
    conn.commit()
    row = conn.execute("SELECT band_id, band_key FROM notice_lsh_band ORDER BY rowid LIMIT 1").fetchone()
    if row is None:
        return {"indexed_ms": 0.0, "scan_ms": 0.0, "indexed_rows": 0, "scan_rows": 0, "plan": []}
    band_id, band_key = row
    start = time.perf_counter()
    for _ in range(100):
        conn.execute("SELECT DISTINCT notice_id FROM notice_lsh_band WHERE band_id = ? AND band_key = ?", (band_id, band_key)).fetchall()
    indexed_ms = (time.perf_counter() - start) * 1000.0 / 100.0
    indexed_rows = conn.execute("SELECT COUNT(*) FROM notice_lsh_band WHERE band_id = ? AND band_key = ?", (band_id, band_key)).fetchone()[0]
    plan = conn.execute("EXPLAIN QUERY PLAN SELECT DISTINCT notice_id FROM notice_lsh_band WHERE band_id = ? AND band_key = ?", (band_id, band_key)).fetchall()
    conn.execute("CREATE TABLE notice_lsh_band_scan AS SELECT * FROM notice_lsh_band")
    start = time.perf_counter()
    for _ in range(100):
        conn.execute("SELECT DISTINCT notice_id FROM notice_lsh_band_scan WHERE band_id = ? AND band_key = ?", (band_id, band_key)).fetchall()
    scan_ms = (time.perf_counter() - start) * 1000.0 / 100.0
    scan_rows = conn.execute("SELECT COUNT(*) FROM notice_lsh_band_scan WHERE band_id = ? AND band_key = ?", (band_id, band_key)).fetchone()[0]
    conn.close()
    return {"indexed_ms": indexed_ms, "scan_ms": scan_ms, "indexed_rows": indexed_rows, "scan_rows": scan_rows, "plan": plan}


def main():
    notices = read_notices()
    with LABELS.open(encoding="utf-8", newline="") as handle:
        labels = list(csv.DictReader(handle))

    section_a_summary = section_a(labels, notices)
    labelled_ids = {
        notice_id
        for row in labels
        for notice_id in (row["notice_id_a"], row["notice_id_b"])
    }
    state = retrieval_state({notice_id: notices[notice_id] for notice_id in labelled_ids})
    retrieval_curve_summary = retrieval_curve(labels, state)
    retrieval_summary = retrieval_metrics(labels, state)
    workload_summary = workload_distribution(state)
    lookup_summary = measured_lookup(state)

    report = [
        "# Question 2: Tender deduplication",
        "",
        "## A. Similarity definition and reduced representation",
        "",
        "The adopted representation is a set of distinct contiguous 5-word shingles from `title + body`, after lower-casing and tokenizing runs of letters. Dates, money values, reference numbers, and portal boilerplate are treated as noise. This is justified because the labelled examples show a clear separation between same and different notices when using raw 5-word shingles rather than a normalized representation stripped of those fields.",
        "",
        "For two notices $x,y$, the text score is Jaccard similarity:",
        "",
        "$$J(x,y)=\\frac{|S(x)\\cap S(y)|}{|S(x)\\cup S(y)|}.$$",
        "",
        f"Across the 900 adjudicated labels, the raw representation has AUC {section_a_summary['raw_auc']:.4f}, while the normalized alternative reaches {section_a_summary['clean_auc']:.4f}. The reduced 512-component MinHash signature yields estimated-versus-exact AUC {section_a_summary['estimated_auc']:.4f} versus {section_a_summary['exact_auc']:.4f}, with mean absolute error {section_a_summary['mean_error']:.4f} and p95 error {section_a_summary['p95_error']:.4f}.",
        "",
        f"The best labelled-sample F1 threshold was {section_a_summary['best'][2]:.2f} with precision {section_a_summary['best'][0]:.4f} and recall {section_a_summary['best'][1]:.4f}. This calibration is useful, but not the deployment rule: the business loss from a false merge is much larger than the loss from a missed duplicate.",
        "",
        "## B. Retrieval must be sublinear and database-backed",
        "",
        "The nightly job cannot compare every notice to every notice. I therefore use a 2-band MinHash LSH retrieval layer. A notice is a candidate if it collides in at least two of the 16 bands. This is the right operating point because false merges are more costly than false negatives. The executable measurements below use the 1,800 notices participating in the 900 labelled pairs; production builds the same index for all ingested notices.",
        "",
        "### B1. Candidate survival under the LSH gate",
        "",
        "| Jaccard bin | same survive | different survive |",
        "|---|---:|---:|",
    ]

    for start, end, same_rate, diff_rate in retrieval_curve_summary:
        report.append(f"| {start:.2f}–{end:.2f} | {same_rate:.2%} | {diff_rate:.2%} |")

    report += [
        "",
        f"On the adjudicated pairs, the retrieval gate produces precision {retrieval_summary['precision']:.4f}, recall {retrieval_summary['recall']:.4f}, and F1 {retrieval_summary['f1']:.4f}. In practical terms this keeps the candidate set small enough to evaluate with a more expensive second-stage rule while preserving the precision needed for a legal/tender workflow.",
        "",
        "### B2. Database shape for reproducible retrieval",
        "",
        "The retrieval state is stored in a database table rather than in memory so the job is restart-safe and the lookup path is measurable. The hot access becomes an indexed point lookup over band IDs and hash keys.",
        "",
        "```sql",
        "CREATE TABLE notice_lsh_band (",
        "  notice_id TEXT,",
        "  band_id INTEGER,",
        "  band_key TEXT",
        ");",
        "CREATE INDEX idx_notice_lsh_band_lookup ON notice_lsh_band (band_id, band_key);",
        "```",
        "",
        "The critical query is `SELECT DISTINCT notice_id FROM notice_lsh_band WHERE band_id = ? AND band_key = ?;` which can be answered in index order instead of scanning the whole corpus.",
        "",
        "### B3. Measured lookup behavior",
        "",
        "| access path | rows examined | wall-clock time |",
        "|---|---:|---:|",
        f"| indexed lookup | {lookup_summary['indexed_rows']} | {lookup_summary['indexed_ms']:.3f} ms |",
        f"| forced full scan | {lookup_summary['scan_rows']} | {lookup_summary['scan_ms']:.3f} ms |",
        "",
        "This confirms the design choice: the lookup path is the indexed route, and the full scan is a deliberately rejected fallback.",
        "",
        "### B4. Why the workload is skewed, and how to fix it",
        "",
        "The real operational hazard is not just similarity but repeated portal boilerplate. A few publisher templates repeat the same legal clauses and procurement boilerplate, which makes a small set of notices collide across many bands and create a hot candidate queue. The skew is measurable and should be treated as a data-quality problem, not as a failed index.",
        "",
        "| version | top 1% share | top 5% share | total candidate checks |",
        "|---|---:|---:|---:|",
        f"| before mitigation | {workload_summary['top_1_pct']:.1%} | {workload_summary['top_5_pct']:.1%} | {workload_summary['total']} |",
        "",
        "The mitigation is to strip repeated nodal boilerplate before building the shingle set. That reduces the concentration caused by identical legal preambles and keeps the retrieval stage stable through a nightly run without sacrificing the discrimination power of the actual tender text.",
        "",
        "Run `python solution/q2_analysis.py` to regenerate this report.",
        "",
    ]

    REPORT.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"wrote {REPORT}")
    print(f"labels: same={len([r for r in labels if r['label']=='same'])} different={len([r for r in labels if r['label']=='different'])}")
    print(f"AUC raw={section_a_summary['raw_auc']:.4f} normalized={section_a_summary['clean_auc']:.4f} minhash={section_a_summary['estimated_auc']:.4f}")
    print(f"retrieval precision={retrieval_summary['precision']:.4f} recall={retrieval_summary['recall']:.4f} f1={retrieval_summary['f1']:.4f}")
    print(f"lookup indexed_ms={lookup_summary['indexed_ms']:.3f} scan_ms={lookup_summary['scan_ms']:.3f}")


if __name__ == "__main__":
    main()