from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration from environment variables (and `.env` locally)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_version: str = "dev"
    google_cloud_project: str

    # local: password login to the compose database.
    # cloudsql: Cloud SQL Python Connector with IAM database authentication.
    db_mode: Literal["local", "cloudsql"] = "local"
    db_name: str = "wobot"
    db_user: str
    db_password: str | None = None
    db_host: str = "localhost"
    db_port: int = 5432
    instance_connection_name: str | None = None

    # Sized against Cloud SQL's connection budget (infra/README.md).
    db_pool_size: int = 2
    db_max_overflow: int = 2
    db_pool_timeout_seconds: float = 10
    db_connect_timeout_seconds: float = 10
    # The chat agent's checkpoints, on a psycopg pool of their own (agent/checkpoints.py).
    checkpoint_pool_size: int = 2

    # Needed only by commands that call OpenAI; in the cloud it comes from Secret Manager.
    openai_api_key: SecretStr | None = None
    openai_timeout_seconds: float = 60
    embedding_model: str = "text-embedding-3-small"
    # Reads the parts of list entries, such as a speech's title, during ingestion.
    extraction_model: str = "gpt-5.6-luna"
    # A source losing more than this share of its records holds the version for review.
    publish_max_drop: float = 0.2
    # Reads images and PDF pages during ingestion; chosen by the A7 evaluation (DEC-049).
    vision_model: str = "gpt-6.1-sol"
    # The chat agent: one model both picks tools and writes answers (DEC-054). A
    # placeholder until the phase B evaluation chooses one (DEC-049).
    agent_model: str = "gpt-5.6-luna"
    # Medium over low after two one-run comparisons on dev (B3, B7): low more often took
    # a search that missed for proof that nothing exists. "default" sends none, leaving
    # the provider's own default.
    agent_reasoning_effort: str = "medium"
    # Raw source bytes go to this bucket, or to the local directory when it is unset.
    knowledge_bucket: str | None = None
    knowledge_local_dir: Path = Path(".data/knowledge")
    # The catalog's Drive file ID. Set on the job only: it never enters the repository.
    product_catalog_file_id: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
