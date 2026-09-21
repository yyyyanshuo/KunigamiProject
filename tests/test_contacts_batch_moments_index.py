from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_contacts_multiselect_can_queue_moments_index_updates():
    contacts = (ROOT / "templates" / "contacts.html").read_text(encoding="utf-8")

    assert 'data-action="moments_index"' in contacts
    assert 'id="momentsIndexModal"' in contacts
    assert 'min="0.1" max="10" step="0.1"' in contacts
    assert "!input.checkValidity()" in contacts
    assert "pendingOps.push({ action: 'moments_index', value, items: items })" in contacts
    assert "JSON.stringify({ moments_index: op.value })" in contacts


def test_contacts_moments_index_batch_only_targets_single_chats():
    contacts = (ROOT / "templates" / "contacts.html").read_text(encoding="utf-8")

    action_block = contacts.split("if (action === 'moments_index')", 1)[1]
    action_block = action_block.split("if (action === 'deep_sleep_time')", 1)[0]

    assert "if (chars.length === 0)" in action_block
    assert "window._pendingMomentsIndexItems = chars" in action_block
    assert "groups" not in action_block
