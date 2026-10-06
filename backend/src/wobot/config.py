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
    # The chat graph: each model node has its own model and reasoning effort, chosen by
    # that node's evaluation (DV11); the rest are placeholders until theirs. An effort of
    # "default" sends none, leaving the provider's own.
    # Jev routes (DEC-059): 23 of 24 dev cases at 0.26s p50, against 24 at 1.40s for the
    # best OpenAI model. An OpenAI model can still be named here; Jev has no effort.
    classify_model: str = "jev-latest"
    classify_effort: str = "none"
    # A route that takes longer has stalled: give up soon, and try once more at most.
    classify_timeout_seconds: float = 15
    chat_model: str = "gpt-5.6-luna"
    chat_effort: str = "low"
    # For Jev; in the cloud it comes from Secret Manager.
    typesafe_api_key: SecretStr | None = None
    # Raw source bytes go to this bucket, or to the local directory when it is unset.
    knowledge_bucket: str | None = None
    knowledge_local_dir: Path = Path(".data/knowledge")
    # The catalog's Drive file ID. Set on the job only: it never enters the repository.
    product_catalog_file_id: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
