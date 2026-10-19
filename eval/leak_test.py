"""Seed-status leak test on customer-only fields (`make leak-test`, ADR 006).

Can a classifier tell seeded customers apart using nothing but the Customer node's own fields?
If it can, ER and Q&A results would be inflated by artifacts of how the data was generated.

Features (all fitted inside each CV fold): name character 2-4-grams, address character 2-4-grams,
suffix type, token count, name length, digits/punctuation, capitalization style, date of birth and
incorporation date parts (with missing flags), country, customer type, IBM entity type.

Models: logistic regression (sparse features) and histogram gradient boosting (dense features with
SVD-reduced n-grams). 5-fold stratified CV ROC-AUC for:
- seeds vs plain customers
- seeds vs look-alikes
- seeds + look-alikes vs plain customers

Plain customers are subsampled to 12,000 negatives (fixed seed). The target is AUC <= 0.60.
Results go to eval/results/leak_test_<timestamp>.json.
"""

import json
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import numpy as np
import polars as pl
from scipy import sparse
from sklearn.decomposition import TruncatedSVD
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import StratifiedKFold
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from tracegraph.config import Settings, settings
from tracegraph.ingest.seed_links import ORG_SUFFIXES

TARGET_AUC = 0.60
MAX_NEGATIVES = 12_000
GROUPS = ("name_ngrams", "address_ngrams", "categorical", "numeric")
CATEGORICAL = ["country", "customer_type", "ibm_entity_type", "suffix_type", "caps_style"]


def _suffix(name: str) -> str:
    lowered = name.lower().rstrip(" .,")
    for suffix in sorted(ORG_SUFFIXES, key=len, reverse=True):
        if lowered.endswith(" " + suffix.rstrip(".")) or lowered.endswith(" " + suffix):
            return suffix.rstrip(".")
    return "none"


def _caps(name: str) -> str:
    letters = [c for c in name if c.isalpha()]
    if letters and all(c.isupper() for c in letters):
        return "upper"
    if all(t[:1].isupper() for t in name.split() if t[:1].isalpha()):
        return "title"
    return "mixed"


def load_customers(config: Settings) -> pl.DataFrame:
    customers = pl.read_parquet(config.staging_dir / "customers.parquet")
    truth = pl.read_parquet(config.seed_ground_truth_path).select("customer_id", "link_type")
    frame = customers.join(truth, on="customer_id", how="left").with_columns(
        pl.col("link_type").fill_null("plain")
    )
    return frame.with_columns(
        pl.col("name").map_elements(_suffix, return_dtype=pl.Utf8).alias("suffix_type"),
        pl.col("name").map_elements(_caps, return_dtype=pl.Utf8).alias("caps_style"),
        pl.col("name").str.split(" ").list.len().alias("token_count"),
        pl.col("name").str.len_chars().alias("name_length"),
        pl.col("name").str.count_matches(r"\d").alias("digit_count"),
        pl.col("name").str.count_matches(r"[^\w\s]").alias("punct_count"),
        pl.col("date_of_birth").dt.year().fill_null(0).alias("dob_year"),
        pl.col("date_of_birth").dt.month().fill_null(0).alias("dob_month"),
        pl.col("date_of_birth").dt.day().fill_null(0).alias("dob_day"),
        pl.col("date_of_birth").is_null().cast(pl.Int8).alias("dob_missing"),
        pl.col("incorporation_date").dt.year().fill_null(0).alias("inc_year"),
        pl.col("incorporation_date").is_null().cast(pl.Int8).alias("inc_missing"),
    )


NUMERIC = [
    "token_count",
    "name_length",
    "digit_count",
    "punct_count",
    "dob_year",
    "dob_month",
    "dob_day",
    "dob_missing",
    "inc_year",
    "inc_missing",
]


class Features:
    """Per-fold feature builder; every transformer is fitted on the training fold only."""

    def __init__(self, groups: tuple[str, ...] = GROUPS) -> None:
        self.groups = groups
        self.name = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), min_df=3)
        self.address = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), min_df=3)
        self.onehot = OneHotEncoder(handle_unknown="ignore", min_frequency=3)
        self.scaler = StandardScaler()

    def fit_transform(self, frame: pl.DataFrame) -> list[Any]:
        return self._blocks(frame, fit=True)

    def transform(self, frame: pl.DataFrame) -> list[Any]:
        return self._blocks(frame, fit=False)

    def _blocks(self, frame: pl.DataFrame, *, fit: bool) -> list[Any]:
        blocks: list[Any] = []

        def run(model: Any, data: Any) -> Any:
            return model.fit_transform(data) if fit else model.transform(data)

        if "name_ngrams" in self.groups:
            blocks.append(run(self.name, frame["name"].to_list()))
        if "address_ngrams" in self.groups:
            blocks.append(run(self.address, frame["address"].to_list()))
        if "categorical" in self.groups:
            blocks.append(run(self.onehot, frame.select(CATEGORICAL).to_numpy()))
        if "numeric" in self.groups:
            blocks.append(sparse.csr_matrix(run(self.scaler, frame.select(NUMERIC).to_numpy())))
        return blocks

    def feature_names(self) -> list[str]:
        names: list[str] = []
        if "name_ngrams" in self.groups:
            names += [f"name:{t!r}" for t in self.name.get_feature_names_out()]
        if "address_ngrams" in self.groups:
            names += [f"address:{t!r}" for t in self.address.get_feature_names_out()]
        if "categorical" in self.groups:
            names += [f"cat:{t}" for t in self.onehot.get_feature_names_out(CATEGORICAL)]
        if "numeric" in self.groups:
            names += [f"num:{t}" for t in NUMERIC]
        return names


def _logistic(train: list[Any], test: list[Any], y: np.ndarray) -> tuple[np.ndarray, Any]:
    model = LogisticRegression(max_iter=3000, class_weight="balanced", C=1.0)
    model.fit(sparse.hstack(train).tocsr(), y)
    return model.predict_proba(sparse.hstack(test).tocsr())[:, 1], model


def _boosting(train: list[Any], test: list[Any], y: np.ndarray) -> tuple[np.ndarray, Any]:
    dense_train, dense_test = [], []
    for block_train, block_test in zip(train, test, strict=True):
        if block_train.shape[1] > 64:  # reduce n-gram blocks
            svd = TruncatedSVD(n_components=48, random_state=0)
            dense_train.append(svd.fit_transform(block_train))
            dense_test.append(svd.transform(block_test))
        else:
            dense_train.append(block_train.toarray())
            dense_test.append(block_test.toarray())
    model = HistGradientBoostingClassifier(
        max_iter=300, learning_rate=0.05, class_weight="balanced", random_state=0
    )
    model.fit(np.hstack(dense_train), y)
    return model.predict_proba(np.hstack(dense_test))[:, 1], model


MODELS: dict[str, Callable[[list[Any], list[Any], np.ndarray], tuple[np.ndarray, Any]]] = {
    "logistic_regression": _logistic,
    "gradient_boosting": _boosting,
}


def cv_auc(
    frame: pl.DataFrame, y: np.ndarray, model: str, groups: tuple[str, ...] = GROUPS
) -> tuple[float, float, list[tuple[str, float]]]:
    """(mean AUC, std, top LR features by |coef| from the last fold)."""
    aucs, top = [], []
    for train_idx, test_idx in StratifiedKFold(5, shuffle=True, random_state=42).split(frame, y):
        features = Features(groups)
        train = features.fit_transform(frame[train_idx])
        test = features.transform(frame[test_idx])
        scores, fitted = MODELS[model](train, test, y[train_idx])
        aucs.append(roc_auc_score(y[test_idx], scores))
        if model == "logistic_regression":
            coefs = fitted.coef_[0]
            names = features.feature_names()
            order = np.argsort(-np.abs(coefs))[:15]
            top = [(names[i], round(float(coefs[i]), 3)) for i in order]
    return float(np.mean(aucs)), float(np.std(aucs)), top


COMPARISONS: dict[str, tuple[set[str], set[str]]] = {
    "seeds_vs_plain": ({"seeded_match"}, {"plain"}),
    "seeds_vs_lookalikes": ({"seeded_match"}, {"hard_negative"}),
    "seeds_and_lookalikes_vs_plain": ({"seeded_match", "hard_negative"}, {"plain"}),
}


def run(config: Settings) -> dict[str, Any]:
    customers = load_customers(config)
    results: dict[str, Any] = {"target_auc": TARGET_AUC, "comparisons": {}}
    for comparison, (positive, negative) in COMPARISONS.items():
        pos = customers.filter(pl.col("link_type").is_in(list(positive)))
        neg = customers.filter(pl.col("link_type").is_in(list(negative)))
        if neg.height > MAX_NEGATIVES:  # ROC-AUC is invariant to class ratio; this bounds runtime
            neg = neg.sample(MAX_NEGATIVES, seed=config.random_seed)
        frame = pl.concat([pos, neg])
        y = frame["link_type"].is_in(list(positive)).to_numpy().astype(int)
        entry: dict[str, Any] = {"n_positive": int(y.sum()), "n_negative": int(len(y) - y.sum())}
        for model in MODELS:
            mean, std, top = cv_auc(frame, y, model)
            entry[model] = {"auc": round(mean, 4), "auc_std": round(std, 4)}
            if top:
                entry[model]["top_features"] = top
        # Which feature groups carry signal on their own (logistic regression, one group at a time).
        entry["auc_by_feature_group"] = {
            group: round(cv_auc(frame, y, "logistic_regression", (group,))[0], 4)
            for group in GROUPS
        }
        results["comparisons"][comparison] = entry
    return results


def main() -> int:
    results = run(settings)
    results["run_utc"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    worst = 0.0
    for comparison, entry in results["comparisons"].items():
        print(f"\n{comparison}  (positives {entry['n_positive']}, negatives {entry['n_negative']})")
        for model in MODELS:
            auc = entry[model]["auc"]
            worst = max(worst, auc)
            print(f"  {model:<20} ROC-AUC {auc:.3f} ± {entry[model]['auc_std']:.3f}")
        print("  single-group AUC (LR):", entry["auc_by_feature_group"])
        if entry["logistic_regression"].get("top_features"):
            print("  top LR features:", entry["logistic_regression"]["top_features"][:10])
    verdict = "PASS" if worst <= TARGET_AUC else "FAIL"
    results["max_auc"], results["verdict"] = worst, verdict
    print(f"\nmax AUC {worst:.3f} vs target {TARGET_AUC} -> {verdict}")
    out_dir = settings.data_dir.parent / "eval" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = re.sub(r"[-:]", "", results["run_utc"])
    path = out_dir / f"leak_test_{stamp}.json"
    path.write_text(json.dumps(results, indent=2) + "\n")
    print(f"written to {path}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
