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
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "aml_investigation_copilot_entities"
    health_check_timeout_seconds: float = Field(default=3.0, gt=0)

    # LLM (disabled unless explicitly enabled by the user)
    llm_provider: Literal["anthropic", "openai_compat"] = "anthropic"
    llm_enabled: bool = False
    llm_max_tokens: int = Field(default=2048, ge=1)
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5-5"
    openai_compat_api_key: str = ""
    openai_compat_base_url: str = "http://localhost:8001/v1"
    openai_compat_model: str = "Qwen/Qwen2.5-7B-Instruct"
    embedding_model: str = "BAAI/bge-small-en-v1.5"

    # Entity resolution
    er_match_threshold: float = Field(default=0.92, ge=0, le=1)
    er_review_threshold: float = Field(default=0.75, ge=0, le=1)

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
    icij_node_sample_size: int = Field(default=20_000, ge=1)
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
    seed_max_perturbations: int = Field(default=2, ge=1)
    hard_negative_rate: float = Field(default=0.01, ge=0, le=1)

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
    def seed_ground_truth_path(self) -> Path:
        return self.processed_dir / "seed_ground_truth.parquet"


settings = Settings()
