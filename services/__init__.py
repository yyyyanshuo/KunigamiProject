"""Services package with lazy compatibility re-exports.

Keeping these imports lazy lets data-only tools use a small service module
without requiring the whole Flask/AI runtime to be installed first.
"""

from importlib import import_module


_EXPORTS = {
    "call_gemini": ("services.ai_client", "call_gemini"),
    "call_openrouter": ("services.ai_client", "call_openrouter"),
    "get_model_config": ("services.ai_client", "get_model_config"),
    "get_relay_provider": ("services.ai_client", "get_relay_provider"),
    "build_system_prompt_v2": ("services.prompt_builder", "build_system_prompt_v2"),
    "build_system_prompt": ("services.prompt_builder", "build_system_prompt"),
    "call_ai_to_summarize": ("services.memory", "call_ai_to_summarize"),
    "generate_long_memory_for_week": ("services.memory", "generate_long_memory_for_week"),
    "generate_medium_memory_for_date": ("services.memory", "generate_medium_memory_for_date"),
    "update_short_memory_for_date": ("services.memory", "update_short_memory_for_date"),
}

__all__ = list(_EXPORTS)


def __getattr__(name):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
