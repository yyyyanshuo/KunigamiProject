import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
import blueprints.forum as forum


def test_two_continuations_keep_posts_and_reply_targets(tmp_path, monkeypatch):
    monkeypatch.setattr(forum, "FORUMS_DB", str(tmp_path / "forums.db"))
    forum.init_forums_db()
    root = [{"name":"root", "content":"start", "floor":4}]
    fid = forum._save_forum_to_db(7, {"posts":root}, "topic", "custom", "", "", None, None)["id"]
    gate = Barrier(2)

    def submit(author):
        batch = [{"floor":5, "replyTo":4, "name":author, "content":"first"},
                 {"floor":6, "replyTo":5, "name":author, "content":"second"}]
        gate.wait(timeout=5)
        return forum._save_forum_to_db(7, {"posts":batch}, "topic", "custom", "", "", root, fid)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(submit, ("A","B")))
    with sqlite3.connect(forum.FORUMS_DB) as conn:
        posts = json.loads(conn.execute("SELECT posts_json FROM forums WHERE id=?", (fid,)).fetchone()[0])
    assert len(posts) == 5
    assert sorted(len(r["posts"]) for r in results) == [3,5]
    assert [p["floor"] for p in posts] == [4,5,6,7,8]
    assert [p["replyTo"] for p in posts[1:]] == [4,5,4,7]
    assert {posts[1]["name"], posts[3]["name"]} == {"A","B"}
    with pytest.raises(ValueError):
        forum._save_forum_to_db(8, {"posts":root}, "topic","custom","","",root,fid)
    with sqlite3.connect(forum.FORUMS_DB) as conn:
        conn.execute("DELETE FROM forums WHERE id=?", (fid,))
    with pytest.raises(ValueError):
        forum._save_forum_to_db(7, {"posts":root}, "topic","custom","","",root,fid)
