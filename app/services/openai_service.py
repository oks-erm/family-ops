from app.config import Settings


class OpenAIService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(self.settings.openai_api_key)

    @property
    def model(self) -> str:
        return self.settings.openai_model
