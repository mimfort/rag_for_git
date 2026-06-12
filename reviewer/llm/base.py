from typing import Protocol

# Реэкспорт для удобства: Finding живёт в vcs.base, но часто импортируется из llm.base.
from reviewer.vcs.base import Finding as Finding  # noqa: F401

class LLMProvider(Protocol):
    def chat_model(self): ...                 # -> BaseChatModel
    def chat_model_with_tools(self, tools: list): ...
