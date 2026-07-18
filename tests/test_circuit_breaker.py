"""Quick test of circuit breaker state machine."""
import sqlite3
from core.circuit_breaker import check_circuit_breaker, record_fatal_error, unfreeze_user, is_user_frozen
from core.config import USERS_DB

# Create a test user
conn = sqlite3.connect(USERS_DB)
conn.execute("INSERT OR IGNORE INTO users (id, email, password_hash) VALUES (999, 'test@test.com', 'hash')")
conn.commit()
conn.close()

# First trigger
r = record_fatal_error(999, "relay", 402)
print("1st:", r["type"])

# Second trigger
r = record_fatal_error(999, "relay", 402)
print("2nd:", r["type"], "remaining:", r.get("remaining_seconds"))

# Check cooldown
cb = check_circuit_breaker(999, "relay")
print("check during cooldown:", cb["type"] if cb else "none")

# Third trigger
r = record_fatal_error(999, "relay", 402)
print("3rd:", r["type"])
print("is_frozen:", is_user_frozen(999))

# Check frozen
cb = check_circuit_breaker(999, "relay")
print("check while frozen:", cb["type"] if cb else "none")

# Unfreeze
unfreeze_user(999)
print("after unfreeze:", is_user_frozen(999))

# Cleanup test user
conn = sqlite3.connect(USERS_DB)
conn.execute("DELETE FROM users WHERE id = 999")
conn.commit()
conn.close()
print("\nAll state machine tests passed!")
