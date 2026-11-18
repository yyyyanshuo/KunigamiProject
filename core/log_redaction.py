"""Redact secrets from diagnostic text before persistence or printing."""
import os
import re


def redact_sensitive(value):
    text = str(value or '')
    for key, secret in os.environ.items():
        if any(part in key.upper() for part in ('KEY', 'TOKEN', 'PASSWORD', 'SECRET')) and len(secret) >= 8:
            text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'(?i)(bearer\s+)[\w.\-]+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)((?:api[_-]?key|access[_-]?token|password|secret)["\s]*[:=]["\s]*)[^\s,"}\]&]+', r'\1[REDACTED]', text)
    text = re.sub(r'\b(?:sk-[\w-]{8,}|AIza[\w-]{20,})\b', '[REDACTED]', text)
    return text
