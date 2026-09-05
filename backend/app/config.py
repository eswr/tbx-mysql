"""Configuration and environment variables."""

import os
from enum import Enum
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DebitSignConvention(str, Enum):
    """Debit amount sign convention."""
    POSITIVE = "positive"  # Debits are positive (schema doc default)
    NEGATIVE = "negative"  # Debits are negative (some datasets)


class UTRMode(str, Enum):
    """UTR searchability mode."""
    PLAINTEXT = "plaintext"  # UTR is plaintext, directly searchable
    OPAQUE = "opaque"  # UTR is encrypted/opaque, not searchable; UTR lookups refused


class ConversationStoreType(str, Enum):
    """Conversation state storage backend."""
    SQLITE = "sqlite"
    MEMORY = "memory"


class LLMProvider(str, Enum):
    """LLM provider for query understanding fallback."""
    RULES = "rules"
    OLLAMA = "ollama"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=True)
    
    # Database
    ARTHA_DATABASE_URL: str = "mysql://artha:artha@127.0.0.1:3306/artha"
    ARTHA_MYSQL_SESSION_TZ: str = "+05:30"
    
    # Semantics
    ARTHA_DEBIT_SIGN: DebitSignConvention = DebitSignConvention.POSITIVE
    ARTHA_UTR_MODE: UTRMode = UTRMode.PLAINTEXT
    
    # LLM
    ARTHA_LLM_PROVIDER: LLMProvider | None = None  # Auto-detect: rules or ollama
    ARTHA_OLLAMA_BASE_URL: str = "http://127.0.0.1:11434"
    ARTHA_OLLAMA_MODEL: str = "qwen3.5:0.8b"
    
    # Conversation
    ARTHA_CONVERSATION_STORE: ConversationStoreType = ConversationStoreType.SQLITE
    ARTHA_SQLITE_DB_PATH: str = "./backend/artha.db"
    
    # Frontend
    ARTHA_CORS_ORIGINS: str = "http://localhost:5173,http://localhost:3000"
    
    # Logging
    ARTHA_LOG_LEVEL: str = "INFO"
    
    def get_cors_origins(self) -> list[str]:
        """Parse CORS origins from comma-separated string."""
        return [origin.strip() for origin in self.ARTHA_CORS_ORIGINS.split(",") if origin.strip()]


def get_settings() -> Settings:
    """Get or create settings singleton."""
    return Settings()
