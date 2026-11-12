"""Configuration management using Pydantic Settings."""

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Telegram Bot
    telegram_bot_token: str = Field(..., description="Token do @BotFather")
    allowed_chat_ids: list[int] = Field(
        default_factory=list, description="Chats autorizados (lista de IDs)"
    )

    # Extraction
    extractor: Literal["auto", "cloud", "local"] = Field(
        default="auto", description="Modo de extração: auto, cloud ou local"
    )
    cloud_extraction_consent: bool = Field(
        default=False, description="Consentimento para enviar documentos à nuvem"
    )
    gemini_api_key: str | None = Field(default=None, description="Chave da API do Gemini")
    ollama_host: str = Field(
        default="http://ollama:11434", description="Host do Ollama para modelo local"
    )
    llm_vision_model: str = Field(
        default="qwen2.5-vl", description="Modelo de visão no Ollama"
    )

    # Alerts
    default_timezone: str = Field(
        default="America/Fortaleza", description="Fuso horário padrão"
    )
    alert_time: str = Field(default="07:30", description="Horário dos alertas (HH:MM)")
    lead_days: list[int] = Field(
        default_factory=lambda: [3, 1, 0], description="Antecedências dos alertas"
    )
    auto_confirm: bool = Field(
        default=False, description="Ativar faturas válidas automaticamente"
    )

    # Optional IMAP
    imap_enabled: bool = Field(default=False, description="Habilitar monitoramento IMAP")
    imap_host: str = Field(default="imap.gmail.com", description="Host IMAP")
    imap_user: str | None = Field(default=None, description="Usuário IMAP")
    imap_password: str | None = Field(default=None, description="Senha IMAP")
    allowed_senders: list[str] = Field(
        default_factory=list, description="Remetentes permitidos (opcional)"
    )

    # Storage
    db_path: Path = Field(
        default=Path("/app/data/autobill.db"), description="Caminho do banco SQLite"
    )
    file_retention_days: int = Field(
        default=90, description="Retenção de arquivos originais (dias)"
    )
    data_dir: Path = Field(default=Path("/app/data"), description="Diretório de dados")

    # Logging
    log_level: str = Field(default="INFO", description="Nível de log")
    log_json: bool = Field(default=True, description="Logs em formato JSON")

    @field_validator("allowed_chat_ids", mode="before")
    @classmethod
    def parse_chat_ids(cls, v: str | list[int]) -> list[int]:
        if isinstance(v, str):
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        return v

    @field_validator("lead_days", mode="before")
    @classmethod
    def parse_lead_days(cls, v: str | list[int]) -> list[int]:
        if isinstance(v, str):
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        return v

    @field_validator("allowed_senders", mode="before")
    @classmethod
    def parse_allowed_senders(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

    @field_validator("data_dir", mode="before")
    @classmethod
    def ensure_data_dir(cls, v: str | Path) -> Path:
        path = Path(v)
        path.mkdir(parents=True, exist_ok=True)
        return path


# Global settings instance
settings = Settings()
