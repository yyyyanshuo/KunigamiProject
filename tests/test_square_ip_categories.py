import sqlite3

from flask import Flask

import blueprints.square as square


def test_square_ip_categories_hide_ips_without_characters(tmp_path, monkeypatch):
    square_db = tmp_path / "square.db"
    monkeypatch.setattr(square, "SQUARE_DB", str(square_db))
    monkeypatch.setattr(square, "SQUARE_AVATARS_DIR", str(tmp_path / "avatars"))
    square.init_square_db()

    with sqlite3.connect(square_db) as conn:
        conn.executemany(
            "INSERT INTO ips (name, heat, character_count) VALUES (?, ?, ?)",
            [
                ("空分类", 100, 0),
                ("有角色", 50, 2),
            ],
        )

    app = Flask(__name__)
    with app.test_request_context("/api/square/ips"):
        response = square.api_square_ips()

    assert response.status_code == 200
    assert response.get_json() == [{"name": "有角色", "heat": 50, "count": 2}]


def test_square_ip_search_still_allows_reusing_empty_ip(tmp_path, monkeypatch):
    square_db = tmp_path / "square.db"
    monkeypatch.setattr(square, "SQUARE_DB", str(square_db))
    monkeypatch.setattr(square, "SQUARE_AVATARS_DIR", str(tmp_path / "avatars"))
    square.init_square_db()

    with sqlite3.connect(square_db) as conn:
        conn.execute(
            "INSERT INTO ips (name, heat, character_count) VALUES (?, ?, ?)",
            ("可复用IP", 0, 0),
        )

    app = Flask(__name__)
    with app.test_request_context("/api/square/search_ip?q=可复用"):
        response = square.api_square_search_ip()

    assert response.status_code == 200
    assert response.get_json() == [{"name": "可复用IP", "count": 0}]
