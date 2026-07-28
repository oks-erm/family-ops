from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = Field(default="local", alias="APP_ENV")
    database_url: str = Field(
        default="postgresql+asyncpg://family:family@localhost:5432/family_copilot",
        alias="DATABASE_URL",
    )
    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-5-mini", alias="OPENAI_MODEL")
    gemini_api_key: str | None = Field(default=None, alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-3.1-flash-lite", alias="GEMINI_MODEL")
    ollama_base_url: str = Field(default="http://ollama:11434", alias="OLLAMA_BASE_URL")
    ollama_model: str = Field(default="llama3.2:3b", alias="OLLAMA_MODEL")
    ai_light_provider: str = Field(default="deterministic", alias="AI_LIGHT_PROVIDER")
    ai_heavy_provider: str = Field(default="openai", alias="AI_HEAVY_PROVIDER")
    ai_request_timeout_seconds: float = Field(default=8.0, alias="AI_REQUEST_TIMEOUT_SECONDS")
    monthly_summary_time: str = Field(default="20:00", alias="MONTHLY_SUMMARY_TIME")
    planning_evening_time: str = Field(default="21:00", alias="PLANNING_EVENING_TIME")
    morning_plan_time: str = Field(default="07:30", alias="MORNING_PLAN_TIME")
    evening_review_time: str = Field(default="21:30", alias="EVENING_REVIEW_TIME")
    weekly_recommendation_time: str = Field(default="08:00", alias="WEEKLY_RECOMMENDATION_TIME")
    default_timezone: str = Field(default="Europe/Lisbon", alias="DEFAULT_TIMEZONE")
    public_base_url: str = Field(default="http://localhost:8000", alias="PUBLIC_BASE_URL")
    scheduling_public_base_url: str | None = Field(
        default=None,
        alias="SCHEDULING_PUBLIC_BASE_URL",
    )
    google_client_id: str | None = Field(default=None, alias="GOOGLE_CLIENT_ID")
    google_client_secret: str | None = Field(default=None, alias="GOOGLE_CLIENT_SECRET")
    google_redirect_uri: str | None = Field(default=None, alias="GOOGLE_REDIRECT_URI")
    google_calendar_id: str = Field(default="primary", alias="GOOGLE_CALENDAR_ID")
    dashboard_google_redirect_uri: str | None = Field(
        default=None,
        alias="DASHBOARD_GOOGLE_REDIRECT_URI",
    )
    dashboard_session_secret: str = Field(
        default="local-dev-change-me",
        alias="DASHBOARD_SESSION_SECRET",
    )
    session_cookie_name: str = Field(
        default="family_copilot_session",
        alias="SESSION_COOKIE_NAME",
    )
    session_cookie_domain: str | None = Field(
        default=None,
        alias="SESSION_COOKIE_DOMAIN",
    )
    scheduling_feedback_smtp_username: str | None = Field(
        default=None,
        alias="SCHEDULING_FEEDBACK_SMTP_USERNAME",
    )
    scheduling_feedback_smtp_app_password: str | None = Field(
        default=None,
        alias="SCHEDULING_FEEDBACK_SMTP_APP_PASSWORD",
    )
    scheduling_feedback_to_email: str | None = Field(
        default=None,
        alias="SCHEDULING_FEEDBACK_TO_EMAIL",
    )
    turnstile_site_key: str | None = Field(default=None, alias="TURNSTILE_SITE_KEY")
    turnstile_secret_key: str | None = Field(default=None, alias="TURNSTILE_SECRET_KEY")
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
