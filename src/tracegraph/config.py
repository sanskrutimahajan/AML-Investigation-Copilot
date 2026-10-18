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

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_username: str = "neo4j"
    neo4j_password: str = ""
    neo4j_database: str = "neo4j"
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "aml_investigation_copilot_entities"
    llm_provider: Literal["anthropic", "openai_compat"] = "anthropic"
    llm_enabled: bool = False
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-3-5-sonnet-latest"
    openai_compat_api_key: str = ""
    openai_compat_base_url: str = "http://localhost:8001/v1"
    openai_compat_model: str = "Qwen/Qwen2.5-7B-Instruct"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    data_dir: Path = Path("data")
    er_match_threshold: float = Field(default=0.92, ge=0, le=1)
    er_review_threshold: float = Field(default=0.75, ge=0, le=1)
    synthetic_profile_count: int = Field(default=1000, ge=0)
    seeded_link_rate: float = Field(default=0.03, ge=0, le=1)
    random_seed: int = 42
    ibm_transaction_sample_size: int = Field(default=100_000, ge=1)
    opensanctions_entity_sample_size: int = Field(default=5_000, ge=1)
    opensanctions_relationship_sample_size: int = Field(default=15_000, ge=0)
    icij_node_sample_size: int = Field(default=2_000, ge=1)
    icij_relationship_sample_size: int = Field(default=6_000, ge=0)
    graph_batch_size: int = Field(default=2_000, ge=1)

    @model_validator(mode="after")
    def validate_er_thresholds(self) -> "Settings":
        if self.er_review_threshold > self.er_match_threshold:
            raise ValueError("er_review_threshold must not exceed er_match_threshold")
        return self


settings = Settings()
