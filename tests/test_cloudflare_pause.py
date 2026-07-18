"""Test Cloudflare global pause mechanism."""
import sys
sys.path.insert(0, "D:\\program\\KunigamiChat\\KunigamiProject")

from core.circuit_breaker import (
    check_relay_global_pause, mark_relay_global_pause, _is_cloudflare_block
)

# Test HTML detection
cf_html = "<!DOCTYPE html>\n<html><head><title>Cloudflare</title></head></html>"
print("CF block detected:", _is_cloudflare_block(cf_html))

cf_html2 = '<html class="no-js"><head><title>Attention Required! | Cloudflare</title>'
print("CF block detected (no DOCTYPE):", _is_cloudflare_block(cf_html2))

json_resp = '{"error":{"message":"Forbidden"}}'
print("Normal JSON not CF:", not _is_cloudflare_block(json_resp))

plain_text = "Some text"
print("Plain text not CF:", not _is_cloudflare_block(plain_text))

empty = ""
print("Empty not CF:", not _is_cloudflare_block(empty))

none = None
print("None not CF:", not _is_cloudflare_block(none or ""))

# Test pause mechanism
print("\nBefore pause:", check_relay_global_pause())
mark_relay_global_pause(3)  # 3 seconds for testing
result = check_relay_global_pause()
print("During pause:", result["type"], "remaining:", result.get("remaining_seconds", 0))

import time
time.sleep(4)
print("After 4s (expired):", check_relay_global_pause())

print("\nAll Cloudflare pause tests passed!")
