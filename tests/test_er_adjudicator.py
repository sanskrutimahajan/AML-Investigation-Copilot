"""Step B adjudicator with a scripted provider: band selection, prompt, resume, final edges."""

from pathlib import Path

import polars as pl
import pytest
from test_llm import ScriptedProvider

from tracegraph.config import Settings
from tracegraph.er import adjudicator as adj

CONFIG = Settings(_env_file=None, er_match_threshold=0.9, er_review_threshold=0.5)

PREDICTIONS = pl.DataFrame(
    {
        "customer_id": ["c1", "c2", "c3", "c4"],
        "entity_id": ["e1", "e2", "e3", "e4"],
        "probability": [0.95, 0.70, 0.5, 0.2],
    }
)


def _record(uid: str, kind: str, name: str, dob: str | None = None, year: int | None = None):
    return {"unique_id": uid, "kind": kind, "name_raw": name, "dob": dob, "dob_year": year,
            "country": "GB"}  # fmt: skip


CUSTOMERS = pl.DataFrame(
    [_record(f"c{i}", "person", f"Mr Anna Schmidt {i}", "1970-01-02", 1970) for i in range(1, 5)]
)
SOURCES = pl.DataFrame([_record(f"e{i}", "person", f"SCHMIDT, Anna {i}") for i in range(1, 5)])


def _reply(decision: str) -> str:
    return f'{{"reason": "names and DOB agree", "decision": "{decision}"}}'


def test_review_band_is_half_open() -> None:
    band = adj.review_band(PREDICTIONS, 0.9, 0.5)
    assert band["customer_id"].to_list() == ["c2", "c3"]


def test_prompt_shows_raw_records_ranked_aliases_and_no_score() -> None:
    customer = _record("c", "person", "Mr Anna Schmidt", "1970-01-02", 1970)
    source = _record("e", "person", "SCHMIDT, Anna", None, 1970)
    aliases = ["Omar Khan", "Anna Shmidt", "SCHMIDT, Anna", "A. Schmidt"]
    prompt = adj.render_pair(customer, source, aliases, max_aliases=2)
    assert "name: Mr Anna Schmidt" in prompt and "name: SCHMIDT, Anna" in prompt
    shown = next(line for line in prompt.splitlines() if "other names:" in line)
    # The two most similar; the dissimilar alias and B's own name are left out.
    assert set(shown.split(": ", 1)[1].split("; ")) == {"Anna Shmidt", "A. Schmidt"}
    assert "1970 (year only)" in prompt and "1970-01-02" in prompt
    assert "probab" not in prompt.lower() and "score" not in prompt.lower()
    org = _record("o", "organization", "Formia Ltd")
    assert "date of birth" not in adj.render_pair(org, org, [], 10)


def test_adjudicate_sends_only_band_pairs_saves_and_resumes(tmp_path: Path) -> None:
    out = tmp_path / adj.ADJUDICATIONS_FILE
    band = adj.review_band(PREDICTIONS, CONFIG.er_match_threshold, CONFIG.er_review_threshold)
    provider = ScriptedProvider([_reply("match"), "not json", "still not json"])
    result = adj.adjudicate(band, CUSTOMERS, SOURCES, {}, provider, CONFIG, out)
    assert len(provider.prompts) == 3  # c2 once, c3 twice (one retry), never c1 or c4
    assert result["decision"].to_list() == ["match", "failed"]
    assert result["latency_seconds"][1] is None
    assert pl.read_parquet(out).height == 2
    assert set(result["model"]) == {"m"} and set(result["match_threshold"]) == {0.9}

    again = ScriptedProvider([])
    assert adj.adjudicate(band, CUSTOMERS, SOURCES, {}, again, CONFIG, out).height == 2
    assert again.prompts == []  # everything already decided

    other = Settings(_env_file=None, er_match_threshold=0.9, er_review_threshold=0.4)
    with pytest.raises(RuntimeError, match="Delete it"):
        adj.adjudicate(band, CUSTOMERS, SOURCES, {}, again, other, out)


def test_final_edges_follow_llm_only_inside_the_band() -> None:
    predictions = pl.DataFrame(
        {
            "customer_id": ["a", "b", "c", "d", "e", "f", "g"],
            "entity_id": ["x"] * 7,
            "probability": [0.95, 0.7, 0.7, 0.7, 0.7, 0.7, 0.2],
        }
    )
    adjudications = pl.DataFrame(
        {
            "customer_id": ["a", "b", "c", "d", "e", "g"],
            "entity_id": ["x"] * 6,
            "decision": ["non_match", "match", "non_match", "unsure", "failed", "match"],
            "reason": ["r"] * 6,
        }
    )
    edges = adj.final_edges(predictions, 0.9, 0.5, adjudications).sort("customer_id")
    assert edges["edge"].to_list() == [
        "SAME_AS",  # above the match threshold: the LLM never overrides it
        "SAME_AS",
        None,
        "POSSIBLE_SAME_AS",
        "POSSIBLE_SAME_AS",
        "POSSIBLE_SAME_AS",  # no decision yet
        None,  # below the band
    ]
    assert edges["llm_decided"].to_list() == [False, True, True, False, False, False, False]
    step_a = adj.final_edges(predictions, 0.9, 0.5)
    assert step_a["edge"].to_list() == ["SAME_AS"] + ["POSSIBLE_SAME_AS"] * 5 + [None]


def test_load_adjudications_rejects_other_thresholds(tmp_path: Path) -> None:
    config = Settings(
        _env_file=None, data_dir=tmp_path, er_match_threshold=0.9, er_review_threshold=0.75
    )
    assert adj.load_adjudications(config) is None
    config.er_dir.mkdir(parents=True)
    pl.DataFrame({"match_threshold": [0.8], "review_threshold": [0.75]}).write_parquet(
        config.er_dir / adj.ADJUDICATIONS_FILE
    )
    with pytest.raises(RuntimeError, match="thresholds"):
        adj.load_adjudications(config)
