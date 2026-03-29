from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    database_url: str
    redis_url: str
    app_env: str = "dev"

    llm_provider: str = "openai"
    openai_api_key: str | None = None
    openai_model: str = "gpt-5.1"

    agents_projects_root: str = "E:/agents_projects"

    # Execution engine runtime configuration
    execution_engine_backend: str = "orchestrated"
    execution_engine_model: str | None = None
    execution_engine_max_steps: int = 8
    execution_engine_max_agent_calls: int = 6
    execution_engine_max_tool_calls: int = 12
    execution_engine_max_command_runs: int = 4
    execution_engine_max_repair_attempts: int = 2

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
