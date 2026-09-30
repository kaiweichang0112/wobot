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
    # Where local ingestion keeps the raw bytes of each snapshot (gitignored).
    knowledge_local_dir: Path = Path(".data/knowledge")


@lru_cache
def get_settings() -> Settings:
    return Settings()
