"""Exercise the circuit breaker against an isolated database."""
import sqlite3

import core.context as context
import core.circuit_breaker as breaker


def test_circuit_breaker_state_machine(tmp_path, monkeypatch):
    database = str(tmp_path / "users.db")
    monkeypatch.setattr(context, "USERS_DB", database)
    monkeypatch.setattr(breaker, "USERS_DB", database)
    context.init_users_db()
    with sqlite3.connect(database) as conn:
        conn.execute(
            "INSERT INTO users (id, email, password_hash) VALUES (?, ?, ?)",
            (999, "test@example.com", "test-hash"),
        )

    assert breaker.record_fatal_error(999, "relay", 402)["type"] == "warning"
    assert breaker.record_fatal_error(999, "relay", 402)["type"] == "cooldown"
    assert breaker.check_circuit_breaker(999, "relay")["type"] == "cooldown"
    assert breaker.record_fatal_error(999, "relay", 402)["type"] == "route_disabled"
    assert breaker.check_circuit_breaker(999, "relay")["type"] == "route_disabled"
    assert not breaker.is_user_frozen(999)
    breaker.unfreeze_user(999)
    assert breaker.check_circuit_breaker(999, "relay") is None
