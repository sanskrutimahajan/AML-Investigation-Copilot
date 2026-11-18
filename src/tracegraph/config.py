"""Validated application settings sourced from environment variables."""

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration; credentials must be supplied through the environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
    )

    # Services
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_username: str = "neo4j"
    neo4j_password: str = ""
    neo4j_database: str = "neo4j"
    # Throwaway integration-test database (docker compose --profile test); never the real graph.
    neo4j_test_uri: str = "bolt://localhost:7688"
    qdrant_test_url: str = "http://localhost:6335"
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "aml_investigation_copilot_entities"
    health_check_timeout_seconds: float = Field(default=3.0, gt=0)

    # LLM, configured from .env only (ADR 008). The default is Ollama (local, free). Any
    # OpenAI-compatible endpoint (OpenAI, Groq, Together, vLLM, LM Studio) or Anthropic works with
    # no code change. A remote provider needs LLM_API_KEY, and setting it is the explicit opt-in.
    llm_provider: Literal["ollama", "openai_compatible", "anthropic"] = "ollama"
    llm_base_url: str = "http://localhost:11434/v1"
    llm_api_key: str = ""
    llm_model: str = "qwen2.5:7b-instruct"
    llm_max_tokens: int = Field(default=1024, ge=1)
    llm_temperature: float = Field(default=0.0, ge=0, le=2)
    llm_timeout_seconds: float = Field(default=180.0, gt=0)
    llm_json_retries: int = Field(default=1, ge=0, le=3)
    embedding_model: str = "BAAI/bge-small-en-v1.5"

    # Entity resolution (Phase 2; ADR 007). Thresholds are tuned on the dev split only.
    # The Splink variant whose scores make SAME_AS decisions, feed the review band and are scored
    # on test. The full variant saturates on dev (correlated name features; worklog 2026-11-17).
    er_decision_variant: Literal["splink_no_embeddings", "splink_full"] = "splink_no_embeddings"
    # Whether Step B's LLM decisions change edges. Off: the local LLM lowered dev F1 (0.649 to
    # 0.608; worklog 2026-11-18), so band pairs stay POSSIBLE_SAME_AS for analyst review.
    er_use_llm_adjudication: bool = False
    # Tuned on the dev split (eval/results/er_test_final.json, ADR 007): best-F1 match threshold and
    # the lowest review threshold whose band holds at most `er_review_queue_cap` dev pairs.
    er_match_threshold: float = Field(default=0.999779, ge=0, le=1)
    er_review_threshold: float = Field(default=0.991478, ge=0, le=1)
    er_block_max_token_frequency: int = Field(default=300, ge=1)
    er_ann_top_k: int = Field(default=10, ge=1)
    er_embedding_batch_size: int = Field(default=256, ge=1)
    er_u_max_pairs: float = Field(default=3e7, gt=0)
    # Assumed recall of "same kind + identical normalized name"; only scales the prior.
    er_deterministic_recall: float = Field(default=0.5, gt=0, le=1)
    er_prediction_floor: float = Field(default=0.001, ge=0, le=1)
    # Review band: the top-N pairs below the match threshold on dev (also bounds Step B LLM cost).
    er_review_queue_cap: int = Field(default=1000, ge=0)
    er_bootstrap_samples: int = Field(default=1000, ge=100)
    # Step B: other names of the reference record shown to the LLM, most similar first.
    er_llm_max_aliases: int = Field(default=10, ge=0)

    # Retrieval and Q&A (Phase 3). Every graph query is read-only, row-capped and time-limited.
    qdrant_names_collection: str = "aml_investigation_copilot_names"
    qa_max_rows: int = Field(default=50, ge=1, le=1000)
    qa_max_hops: int = Field(default=4, ge=1, le=6)
    qa_query_timeout_seconds: float = Field(default=10.0, gt=0)
    qa_link_top_k: int = Field(default=5, ge=1)
    qa_rrf_k: int = Field(default=60, ge=1)  # reciprocal rank fusion constant
    # A name found only by vector search needs this cosine similarity (nonsense scores ~0.83 with
    # bge-small; set from a few examples, checked by the QA check set).
    qa_link_min_vector_score: float = Field(default=0.9, ge=0, le=1)
    qa_context_max_items: int = Field(default=40, ge=1)
    qa_check_per_family: int = Field(default=10, ge=2)  # eval/qa_check.py questions per family

    # Typology detection (Phase 4; ADR 010). Thresholds set from the dev seed split
    # (eval/results/detection_dev_20261115T065128Z.json); the test split is scored once.
    detect_fan_in_min_senders: int = Field(default=6, ge=2)
    detect_fan_in_window_hours: float = Field(default=168.0, gt=0)
    detect_fan_out_min_receivers: int = Field(default=7, ge=2)
    detect_fan_out_window_hours: float = Field(default=96.0, gt=0)
    detect_cycle_max_hops: int = Field(default=12, ge=2)
    detect_cycle_window_hours: float = Field(default=96.0, gt=0)
    detect_sg_min_intermediates: int = Field(default=3, ge=2)
    detect_sg_window_hours: float = Field(default=96.0, gt=0)
    # Dev tuning grid.
    detect_grid_fan_k: list[int] = [3, 4, 5, 6, 7, 8, 10]
    detect_grid_cycle_hops: list[int] = [4, 6, 8, 12]
    detect_grid_sg_k: list[int] = [2, 3, 4, 5, 6, 7]
    detect_grid_window_hours: list[float] = [24.0, 48.0, 96.0, 168.0]
    detect_bootstrap_samples: int = Field(default=1000, ge=100)
    # Phase 4 detectors (ADR 015), chosen on dev by `make detect-tune-ext`
    # (eval/results/detection_ext_dev_20261118T135055Z.json).
    detect_layer_min_hops: int = Field(default=3, ge=2)
    detect_layer_window_hours: float = Field(default=48.0, gt=0)
    detect_mule_min_senders: int = Field(default=3, ge=2)
    detect_mule_window_hours: float = Field(default=168.0, gt=0)
    detect_rapid_min_pass_throughs: int = Field(default=1, ge=1)
    detect_rapid_window_hours: float = Field(default=168.0, gt=0)
    detect_rapid_min_ratio: float = Field(default=0.8, gt=0, le=1)  # fixed, not tuned
    # Structuring is fixed, not tuned: its only ground truth is injected by our own generator.
    # 10,000 USD is the US currency-transaction-report threshold.
    detect_struct_min_deposits: int = Field(default=3, ge=2)
    detect_struct_window_hours: float = Field(default=72.0, gt=0)
    detect_struct_threshold: float = Field(default=10_000.0, gt=0)
    detect_struct_band: float = Field(default=0.1, gt=0, lt=1)
    detect_struct_currency: str = "US Dollar"
    detect_grid_layer_hops: list[int] = [3, 4, 5, 6, 8]
    detect_grid_mule_k: list[int] = [2, 3, 4, 5, 6, 8]
    detect_grid_rapid_n: list[int] = [1, 2, 3, 5, 8]
    # Rapid movement has no labels: dev picks the grid point with the largest precision gain over
    # the degree baseline among points that flag at least this many dev seed accounts.
    detect_rapid_min_flagged: int = Field(default=50, ge=1)
    detect_struct_injected_cases: int = Field(default=40, ge=10)
    sar_check_per_typology: int = Field(default=5, ge=1)  # eval/sar_check.py alerts sampled

    # Streamlit UI (Phase 5): a thin client of the API; it never touches the databases or LLM.
    ui_api_url: str = "http://127.0.0.1:8000"
    ui_request_timeout_seconds: float = Field(default=120.0, gt=0)
    # API logs: JSON lines with request IDs (`text` for local reading); see observability.py.
    log_format: Literal["json", "text"] = "json"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # SAR drafting on the local model: mean 90 s in `make sar-check`, but one live draft of a
    # 16-payment alert took 309 s on a busy laptop.
    ui_draft_timeout_seconds: float = Field(default=900.0, gt=0)

    # Data locations
    data_dir: Path = Path("data")
    random_seed: int = 42

    # IBM AML HI-Small sampling (ADR 003 §4)
    ibm_benign_seed_accounts: int = Field(default=8_000, ge=0)
    ibm_max_txns_per_account: int = Field(default=50, ge=1)
    ibm_max_transactions: int = Field(default=400_000, ge=1)
    ibm_drop_self_transfers: bool = True

    # OpenSanctions (ADR 003 §5)
    opensanctions_datasets: list[str] = ["us_ofac_sdn", "eu_fsf", "un_sc_sanctions"]
    opensanctions_entity_sample_size: int = Field(default=25_000, ge=1)

    # ICIJ Offshore Leaks (ADR 003 §6)
    icij_source_ids: list[str] = ["Panama Papers"]
    icij_seed_entity_count: int = Field(default=3_000, ge=1)
    icij_node_sample_size: int = Field(default=22_000, ge=1)
    icij_relationship_sample_size: int = Field(default=80_000, ge=1)

    # Synthetic customers and seeded links (ADR 003 §7-8)
    faker_locales: list[str] = [
        "en_US",
        "en_GB",
        "de_DE",
        "fr_FR",
        "es_ES",
        "it_IT",
        "nl_NL",
        "pt_BR",
    ]
    seeded_link_rate: float = Field(default=0.03, ge=0, le=1)
    seed_opensanctions_share: float = Field(default=0.5, ge=0, le=1)
    seed_laundering_share: float = Field(default=0.5, ge=0, le=1)
    seed_alias_rate: float = Field(default=0.15, ge=0, le=1)
    hard_negative_rate: float = Field(default=0.01, ge=0, le=1)
    # Exact source name with a conflicting DOB/country, labelled non-match (screening false alarm).
    homonym_rate: float = Field(default=0.01, ge=0, le=1)
    # Applied identically to every customer, seeded or not (ADR 006).
    customer_source_country_rate: float = Field(default=0.5, ge=0, le=1)
    customer_uppercase_rate: float = Field(default=0.3, ge=0, le=1)
    person_comma_format_rate: float = Field(default=0.1, ge=0, le=1)
    # P(0, 1, 2 data-entry edits) for every customer's name, whatever its seed status.
    name_edit_count_weights: list[float] = [0.3, 0.45, 0.25]

    graph_batch_size: int = Field(default=2_000, ge=1)

    @model_validator(mode="after")
    def validate_er_thresholds(self) -> "Settings":
        if self.er_review_threshold > self.er_match_threshold:
            raise ValueError("er_review_threshold must not exceed er_match_threshold")
        return self

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"

    @property
    def staging_dir(self) -> Path:
        """The only directory graph loaders may read."""
        return self.processed_dir / "staging"

    @property
    def ibm_pattern_ground_truth_path(self) -> Path:
        return self.processed_dir / "ibm_pattern_ground_truth.parquet"

    @property
    def ibm_sample_seeds_path(self) -> Path:
        """Which accounts seeded the IBM sample; eval-only (needed for seed-only comparisons)."""
        return self.processed_dir / "ibm_sample_seeds.parquet"

    @property
    def customer_name_edits_path(self) -> Path:
        """Data-entry edits per customer; eval-only (checks edits are role-independent)."""
        return self.processed_dir / "customer_name_edits.parquet"

    @property
    def er_dir(self) -> Path:
        """ER run outputs (scored pairs); not ground truth."""
        return self.processed_dir / "er"

    @property
    def er_decision_predictions_path(self) -> Path:
        """Scored pairs of the decision variant, written by `make er`."""
        return self.er_dir / f"predictions_{self.er_decision_variant}.parquet"

    @property
    def er_split_path(self) -> Path:
        """Eval-only dev/test assignment of customers (ADR 007)."""
        return self.processed_dir / "er_split.parquet"

    @property
    def detection_split_path(self) -> Path:
        """Eval-only dev/test assignment of seed accounts (ADR 010)."""
        return self.processed_dir / "detection_split.parquet"

    @property
    def seed_ground_truth_path(self) -> Path:
        return self.processed_dir / "seed_ground_truth.parquet"


settings = Settings()
