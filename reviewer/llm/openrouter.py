from __future__ import annotations
from langchain_openai import ChatOpenAI

from reviewer.config.settings import Settings

class OpenRouterProvider:
    BASE_URL = "https://openrouter.ai/api/v1"

    def __init__(self, settings: Settings):
        self.s = settings

    def _extra_body(self) -> dict:
        eb: dict = {"provider": self.s.openrouter_provider_block()}
        models = self.s.openrouter_models_list()
        if models:
            eb["models"] = models
        return eb

    def _headers(self) -> dict:
        h = {}
        if self.s.openrouter_app_url:
            h["HTTP-Referer"] = self.s.openrouter_app_url
        if self.s.openrouter_app_title:
            h["X-Title"] = self.s.openrouter_app_title
        return h

    def chat_model(self, model: str | None = None) -> ChatOpenAI:
        """Вернуть ChatOpenAI для OpenRouter.

        Args:
            model: переопределение модели для этапа (например, дешёвая для verify).
                   Если не задано — используется ``settings.openrouter_model``.
        """
        return ChatOpenAI(
            base_url=self.BASE_URL,
            api_key=self.s.openrouter_api_key,
            model=model or self.s.openrouter_model,
            temperature=0,
            default_headers=self._headers() or None,
            extra_body=self._extra_body(),
        )

    def chat_model_with_tools(self, tools: list, model: str | None = None):
        """Вернуть ChatOpenAI с привязанными инструментами.

        Args:
            tools: список инструментов для bind_tools.
            model: переопределение модели для этапа (например, дешёвая для verify).
        """
        return self.chat_model(model=model).bind_tools(tools)
