"""Private mode rules must not leak into any group prompt caller."""
import json

import pytest

from core import config
from services import prompt_builder


@pytest.mark.parametrize('include_global_format', [True, False])
@pytest.mark.parametrize('language', ['zh', 'ja', 'en'])
@pytest.mark.parametrize('group_mode,private_mode', [('online', 'offline'), ('offline', 'online')])
def test_group_prompt_uses_group_mode_without_private_instructions(tmp_path, monkeypatch, language, group_mode, private_mode, include_global_format):
    prompts = tmp_path / 'prompts'
    prompts.mkdir()
    characters = tmp_path / 'characters.json'
    characters.write_text(json.dumps({'hero': {'name': 'Hero', 'chat_mode': private_mode}}))
    groups = tmp_path / 'groups.json'
    groups.write_text(json.dumps({'g': {'members': ['hero'], 'group_chat_mode': group_mode}}))
    monkeypatch.setattr(prompt_builder, '_get_characters_config_file', lambda **_: str(characters))
    monkeypatch.setattr(prompt_builder, '_get_groups_config_file', lambda **_: str(groups))
    monkeypatch.setattr(prompt_builder, 'get_paths', lambda *a, **k: ('', str(prompts)))
    monkeypatch.setattr(prompt_builder, 'get_ai_language', lambda *a, **k: language)
    monkeypatch.setattr(prompt_builder, 'get_user_age', lambda **_: None)
    monkeypatch.setattr(prompt_builder, '_get_username_for_user', lambda **_: 'User')
    monkeypatch.setattr(prompt_builder, '_get_settings_for_user', lambda **_: {})
    monkeypatch.setattr(prompt_builder, 'load_character_positions', lambda **_: {})
    monkeypatch.setattr(prompt_builder, 'normalize_map_state', lambda **_: ({}, {}, {'locations': []}))

    def build(group_id):
        return prompt_builder.build_system_prompt_v2('hero', group_id=group_id, user_id=1,
            include_long_memory=False, include_recent_messages=False, read_only=True,
            include_global_format=include_global_format)

    prompt = build('g')
    assert config.get_mode_context(language, private_mode) not in prompt
    assert config.get_mode_context(language, group_mode) not in prompt
    assert '[SET_CHAT_MODE:' not in prompt
    assert '[CALL_USER]' not in prompt
    assert '[TRANSFER:' not in prompt
    assert '[VIDEO_CALL]' not in prompt
    assert ('[MUSIC]' in prompt) == (include_global_format and group_mode == 'online')
    assert ('[FILE]' in prompt) == (include_global_format and group_mode == 'online')
    assert '[SAFETY_ALERT]' in prompt
    assert '[SET_EMOTION:' in prompt
    state = prompt_builder.build_agent_current_state_section('hero', str(prompts), group_id='g', user_id=1)
    assert group_mode in state and private_mode not in state
    assert ('【Media Capability' in prompt) == (include_global_format and group_mode == 'online')
    assert ('【Sticker / 表情】' in prompt) == (include_global_format and group_mode == 'online')

    # A different private setting must not alter the same group's outgoing prompt.
    characters.write_text(json.dumps({'hero': {'name': 'Hero', 'chat_mode': group_mode}}))
    updated = build('g')
    assert ('【Media Capability' in updated) == (include_global_format and group_mode == 'online')
    assert '[SET_CHAT_MODE:' not in updated
    # Private conversations retain their own mode-specific behavior.
    private = build(None)
    assert config.get_mode_context(language, group_mode) in private
    assert '[SET_CHAT_MODE:' in private
