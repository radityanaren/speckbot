"""
User custom provider template - create your own provider class.

To use:
1. Copy this file and rename it (e.g., my_provider.py)
2. Create a class that inherits from LLMProvider
3. Add provider to config.json with "type": "my_provider"

Example config:
{
  "providers": [
    {
      "name": "my_custom",
      "type": "my_provider",  <- matches filename (without .py)
      "apiKey": "...",
      "apiBase": "...",
      "model": "my-model"
    }
  ]
}
"""

from typing import Any

from speckbot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class MyCustomProvider(LLMProvider):
    """
    Custom provider example.

    Override the chat() method to implement your own API logic.
    """

    def __init__(
        self,
        api_key: str = "no-key",
        api_base: str = "http://localhost:8000/v1",
        default_model: str = "default",
        extra_headers: dict[str, str] | None = None,
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model

        # Initialize your API client here
        # Example: self._client = YourAPIClient(api_key, api_base)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        """
        Implement your custom chat logic here.

        Return an LLMResponse with:
        - content: str - the text response
        - tool_calls: list[ToolCallRequest] - any tool calls (optional)
        - finish_reason: str - "stop", "length", "error", etc.
        - usage: dict - token usage info (optional)
        """

        # Your custom API logic here!
        # Example:
        # response = await self._client.chat(
        #     model=model or self.default_model,
        #     messages=messages,
        #     ...
        # )

        # Return the response in LLMResponse format
        return LLMResponse(
            content="Hello from custom provider!",
            finish_reason="stop",
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        )

    def get_default_model(self) -> str:
        return self.default_model


# That's it! The provider system will automatically find and use this class
# when you specify "type": "my_custom_provider" (filename without .py) in config
