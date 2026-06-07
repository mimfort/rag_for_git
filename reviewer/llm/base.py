from typing import Protocol

class LLMProvider(Protocol):
    def chat_model(self): ...                 # -> BaseChatModel
    def chat_model_with_tools(self, tools: list): ...
