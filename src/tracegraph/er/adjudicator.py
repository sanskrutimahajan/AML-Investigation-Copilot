"""Step B: LLM adjudication of the review band (`make er-adjudicate`, ADR 007).

Pairs the full model scores in the review band (`er_review_threshold` <= probability <
`er_match_threshold`) go one at a time to the configured LLM (`get_provider`; local Ollama by
default), which answers `match`, `non_match` or `unsure`. Only `match` turns a pair into `SAME_AS`;
`non_match` drops it; `unsure` and replies that never validate (`failed`) stay `POSSIBLE_SAME_AS`
for an analyst.

The LLM sees the two records as entered (raw names, so transliteration and suffix detail survive)
and never the model score. Decisions are saved after every pair to `adjudications.parquet`, which
also records the thresholds, provider, model and prompt version; a rerun resumes and refuses to mix
runs. Reads staging and ER predictions only. Never reads ground truth.
"""

import sys
from pathlib import Path
from typing import Any, Literal

import polars as pl
from pydantic import BaseModel
from rapidfuzz import fuzz

from tracegraph.config import Settings, settings
from tracegraph.er.blocking import build_records
from tracegraph.er.normalize import normalize_name
from tracegraph.llm.base import LLMProvider
from tracegraph.llm.factory import get_provider
from tracegraph.llm.structured import StructuredOutputError, generate_json

PROMPT_VERSION = "v2"
ADJUDICATIONS_FILE = "adjudications.parquet"
RUN_COLUMNS = ("provider", "model", "prompt_version", "match_threshold", "review_threshold")

SYSTEM = """You review name-screening hits. Decide whether record A (a bank customer) and \
record B (a reference record) refer to the same real-world person or organization.

- Names of the same party often differ: typos, transliteration, word order, initials, a dropped or \
changed legal suffix (Ltd / Limited), or one of B's other names. These alone do not mean different \
parties. Titles (Mr, Mrs, Dr) and letter case carry no identity information.
- Different parties: a surname, or a distinctive word of an organization name, that is entirely \
different rather than a typo, transliteration, initial or reordering of the other. Sharing only a \
first name or generic words (Company, Bank, Trading, Joint Stock) is not a match.
- Matching names are weak evidence when the name is common or very short.
- Date of birth: the same date supports a match; a clearly different date means different people. \
A missing date is not evidence either way.
- Country is weak evidence: the same party is often listed under different countries (residence, \
nationality, registration). Never decide non_match from country alone.
- Answer unsure only when nothing conflicts but the evidence is thin, for example an identical \
common name with no date of birth; an analyst will review those.

Reply with JSON only: a short reason saying which name parts agree and which differ (and the date \
of birth, if both have one), then the decision."""


class Adjudication(BaseModel):
    reason: str
    decision: Literal["match", "non_match", "unsure"]


def review_band(predictions: pl.DataFrame, match: float, review: float) -> pl.DataFrame:
    return predictions.filter(
        (pl.col("probability") >= review) & (pl.col("probability") < match)
    ).sort("customer_id", "entity_id")


def _field(record: dict[str, Any], key: str) -> str:
    if key == "dob" and not record["dob"] and record["dob_year"]:
        return f"{record['dob_year']} (year only)"
    return str(record[key]) if record[key] else "unknown"


def render_pair(
    customer: dict[str, Any], source: dict[str, Any], aliases: list[str], max_aliases: int
) -> str:
    """The prompt for one pair; B's other names are ranked by similarity to A's name."""
    target = normalize_name(customer["name_raw"])
    ranked = sorted(
        dict.fromkeys(a for a in aliases if a and a != source["name_raw"]),
        key=lambda a: -fuzz.token_set_ratio(target, normalize_name(a)),
    )[:max_aliases]
    lines = []
    for title, record in (("A (bank customer)", customer), ("B (reference record)", source)):
        lines += [f"Record {title}", f"  name: {record['name_raw']}"]
        if record is source:
            lines.append(f"  other names: {'; '.join(ranked) if ranked else 'none'}")
        lines.append(f"  type: {record['kind']}")
        if record["kind"] == "person":
            lines.append(f"  date of birth: {_field(record, 'dob')}")
        lines.append(f"  country: {_field(record, 'country')}")
    return "\n".join(lines)


def adjudicate(
    band: pl.DataFrame,
    customers: pl.DataFrame,
    sources: pl.DataFrame,
    aliases: dict[str, list[str]],
    provider: LLMProvider,
    config: Settings,
    out_path: Path,
) -> pl.DataFrame:
    """Adjudicate every band pair not already in `out_path`, saving after each pair."""
    run = {
        "provider": provider.settings.provider,
        "model": provider.settings.model,
        "prompt_version": PROMPT_VERSION,
        "match_threshold": config.er_match_threshold,
        "review_threshold": config.er_review_threshold,
    }
    rows: list[dict[str, Any]] = []
    if out_path.exists():
        previous = pl.read_parquet(out_path)
        mixed = previous.select(RUN_COLUMNS).unique().to_dicts()
        if mixed != [run]:
            raise RuntimeError(
                f"{out_path} was made with {mixed}, not {run}. Delete it to start a new run."
            )
        rows = previous.to_dicts()
    done = {(r["customer_id"], r["entity_id"]) for r in rows}
    by_id = {
        "customer": {r["unique_id"]: r for r in customers.iter_rows(named=True)},
        "source": {r["unique_id"]: r for r in sources.iter_rows(named=True)},
    }
    for pair in band.iter_rows(named=True):
        key = (pair["customer_id"], pair["entity_id"])
        if key in done:
            continue
        prompt = render_pair(
            by_id["customer"][key[0]],
            by_id["source"][key[1]],
            aliases.get(key[1], []),
            config.er_llm_max_aliases,
        )
        decision: str
        latency: float | None
        try:
            result = generate_json(
                provider, prompt, Adjudication, system=SYSTEM, retries=config.llm_json_retries
            )
            decision, reason = result.value.decision, result.value.reason
            attempts, latency = result.attempts, result.latency_seconds
        except StructuredOutputError as exc:
            decision, reason = "failed", str(exc)[:300]
            attempts, latency = config.llm_json_retries + 1, None
        rows.append(
            {
                "customer_id": key[0],
                "entity_id": key[1],
                "probability": float(pair["probability"]),
                "decision": decision,
                "reason": reason,
                "attempts": attempts,
                "latency_seconds": None if latency is None else round(latency, 3),
                **run,
            }
        )
        pl.DataFrame(rows, infer_schema_length=None).write_parquet(out_path)
    return pl.DataFrame(rows, infer_schema_length=None)


def final_edges(
    predictions: pl.DataFrame,
    match: float,
    review: float,
    adjudications: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """Each scored pair's ER edge: `SAME_AS`, `POSSIBLE_SAME_AS` or null.

    Without adjudications this is Step A (score bands only). With them, a band pair the LLM called
    `match` becomes `SAME_AS`, `non_match` drops, and `unsure` / `failed` stay queued.
    """
    frame = predictions.with_columns(pl.lit(None, dtype=pl.Utf8).alias("decision"))
    if adjudications is not None:
        frame = frame.drop("decision").join(
            adjudications.select("customer_id", "entity_id", "decision", "reason"),
            on=["customer_id", "entity_id"],
            how="left",
        )
    in_band = (pl.col("probability") >= review) & (pl.col("probability") < match)
    return frame.with_columns(
        pl.when(pl.col("probability") >= match)
        .then(pl.lit("SAME_AS"))
        .when(in_band & (pl.col("decision") == "match"))
        .then(pl.lit("SAME_AS"))
        .when(in_band & (pl.col("decision") == "non_match"))
        .then(pl.lit(None))
        .when(in_band)
        .then(pl.lit("POSSIBLE_SAME_AS"))
        .otherwise(pl.lit(None))
        .alias("edge"),
        (in_band & pl.col("decision").is_in(["match", "non_match"]).fill_null(False)).alias(
            "llm_decided"
        ),
    )


def load_adjudications(config: Settings) -> pl.DataFrame | None:
    """Saved decisions, checked against the configured thresholds; None if Step B has not run."""
    path = config.er_dir / ADJUDICATIONS_FILE
    if not path.exists():
        return None
    frame = pl.read_parquet(path)
    thresholds = frame.select("match_threshold", "review_threshold").unique().rows()
    if thresholds != [(config.er_match_threshold, config.er_review_threshold)]:
        raise RuntimeError(
            f"{path} was made with thresholds {thresholds}, but config has "
            f"({config.er_match_threshold}, {config.er_review_threshold})."
        )
    return frame


def main(config: Settings = settings) -> int:
    customers, sources = build_records(config.staging_dir, config.er_block_max_token_frequency)
    aliases = {
        row["entity_id"]: list(row["aliases"])
        for row in pl.read_parquet(config.staging_dir / "opensanctions_entities.parquet")
        .select("entity_id", "aliases")
        .iter_rows(named=True)
    }
    predictions = pl.read_parquet(config.er_dir / "predictions_splink_full.parquet")
    band = review_band(predictions, config.er_match_threshold, config.er_review_threshold)
    provider = get_provider(config)
    print(f"{band.height} review-band pairs -> {provider.settings.describe()}")
    result = adjudicate(
        band, customers, sources, aliases, provider, config, config.er_dir / ADJUDICATIONS_FILE
    )
    summary = result.group_by("decision").len().sort("decision")
    print(dict(summary.iter_rows()))
    latency = result["latency_seconds"].drop_nulls()
    if latency.len():
        print(f"mean latency {float(latency.mean()):.2f}s (failed replies excluded)")  # type: ignore[arg-type]
    return 0


if __name__ == "__main__":
    sys.exit(main())
