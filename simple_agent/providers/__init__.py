"""Provider registry.

Adding a backend is one file plus one line here — never a change to the loop.
"""

from __future__ import annotations

from .base import Provider, Response, ToolCall

_REGISTRY: dict[str, str] = {
    "anthropic": "simple_agent.providers.anthropic:AnthropicProvider",
    "bedrock": "simple_agent.providers.bedrock:BedrockProvider",
    "gemini": "simple_agent.providers.gemini:GeminiProvider",
    "openai": "simple_agent.providers.openai_responses:OpenAIResponsesProvider",
}


def get_provider(name: str, **kwargs) -> Provider:
    import importlib

    try:
        target = _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY))
        raise RuntimeError(f"Unknown provider {name!r}. Known providers: {known}") from None
    module_path, class_name = target.split(":")
    module = importlib.import_module(module_path)
    return getattr(module, class_name)(**kwargs)


__all__ = ["Provider", "Response", "ToolCall", "get_provider"]
