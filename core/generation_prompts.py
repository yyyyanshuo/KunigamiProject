"""Localized, copyable generation instructions; source data remains verbatim."""
import json
from core.localization import tr


def relationship_prompt(target_name, records, current_graph, perspective, language, output_language):
    reverse = perspective == 'reverse'
    prefix = 'prompt.relation.reverse' if reverse else 'prompt.relation.normal'
    parts = [
        tr(prefix, language, name=target_name),
        tr('prompt.relation.schema', language),
        tr('prompt.relation.keys_reverse' if reverse else 'prompt.relation.keys_normal', language),
        tr('prompt.relation.languages_reverse', language) if reverse else tr('prompt.output_language', language, target=output_language),
        tr('prompt.relation.keep', language),
        tr('prompt.relation.allowed', language),
        json.dumps(records, ensure_ascii=False, indent=2),
        tr('prompt.relation.current', language),
        json.dumps(current_graph, ensure_ascii=False, indent=2),
    ]
    return '\n\n'.join(parts)
