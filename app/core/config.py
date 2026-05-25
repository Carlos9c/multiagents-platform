from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    database_url: str
    app_env: str = "dev"

    llm_provider: str = "openai"
    openai_api_key: str | None = None
    openai_model: str = "gpt-5.1"
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-4-6"
    anthropic_max_tokens: int = 32768

    agents_projects_root: str = "E:/agents_projects"

    # Execution engine runtime configuration
    execution_engine_backend: str = "orchestrated"
    # Orchestrator + context-selection agent
    execution_engine_provider: str | None = None
    execution_engine_model: str | None = None
    # Code-change subagent (overrides execution_engine_* when set)
    code_agent_provider: str | None = None
    code_agent_model: str | None = None
    # Command-runner subagent (overrides execution_engine_* when set)
    command_agent_provider: str | None = None
    command_agent_model: str | None = None
    # Documentation agent
    documentation_agent_provider: str = "openai"
    documentation_agent_model: str = "gpt-5.2"
    # Test-builder subagent (overrides code_agent_* when set)
    test_agent_provider: str | None = None
    test_agent_model: str | None = None
    # Validation layer
    validator_provider: str | None = None
    validator_model: str | None = None
    execution_engine_max_steps: int = 8
    execution_engine_max_agent_calls: int = 8
    execution_engine_max_tool_calls: int = 12
    execution_engine_max_command_runs: int = 4
    execution_engine_max_repair_attempts: int = 2

    # Android Gradle wrapper — version seeded into source_dir during bootstrap
    gradle_wrapper_version: str = "8.7"

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
