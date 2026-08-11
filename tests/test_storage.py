import sqlite3
from pathlib import Path

from src.migrations import CURRENT_SCHEMA_VERSION
from src.models import ModelOption
from src.storage import Storage


def test_conversation_and_message_lifecycle(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "nested" / "chat.sqlite3")
    conversation = storage.create_conversation("First", "openrouter/auto", "Be useful")
    user = storage.add_message(conversation.id, "user", "Hello")
    assistant = storage.add_message(
        conversation.id,
        "assistant",
        "Hi",
        input_tokens=12,
        output_tokens=4,
        cached_tokens=3,
        reasoning_tokens=2,
        cost=0.0001,
    )

    assert user.id is not None
    assert assistant.cost == 0.0001
    assert assistant.total_tokens == 16
    assert [item.content for item in storage.list_messages(conversation.id)] == [
        "Hello",
        "Hi",
    ]

    storage.update_conversation(
        conversation.id,
        title="Renamed",
        model="custom/model",
        system_prompt="New prompt",
    )
    updated = storage.get_conversation(conversation.id)
    assert updated is not None
    assert (updated.title, updated.model, updated.system_prompt) == (
        "Renamed",
        "custom/model",
        "New prompt",
    )
    assert storage.usage_summary(conversation.id) == {
        "input_tokens": 12,
        "output_tokens": 4,
        "cached_tokens": 3,
        "reasoning_tokens": 2,
        "cost": 0.0001,
    }

    storage.delete_conversation(conversation.id)
    assert storage.get_conversation(conversation.id) is None
    assert storage.list_messages(conversation.id) == []
    storage.close()


def test_v01_flat_schema_migrates_without_losing_messages(tmp_path: Path) -> None:
    path = tmp_path / "v01.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE conversations (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, model TEXT NOT NULL,
            system_prompt TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL,
            input_tokens INTEGER, output_tokens INTEGER, cost REAL
        );
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO conversations VALUES
            ('old', 'Old chat', 'openrouter/auto', 'System', '2026-01-01', '2026-01-02');
        INSERT INTO messages(conversation_id, role, content, created_at) VALUES
            ('old', 'user', 'one', '2026-01-01'),
            ('old', 'assistant', 'two', '2026-01-02'),
            ('old', 'user', 'three', '2026-01-03');
        """
    )
    connection.commit()
    connection.close()

    storage = Storage(path)
    messages = storage.list_messages("old")
    assert [message.content for message in messages] == ["one", "two", "three"]
    assert [message.parent_message_id for message in messages] == [None, 1, 2]
    conversation = storage.get_conversation("old")
    assert conversation is not None and conversation.active_leaf_id == 3
    version = storage._connection.execute("PRAGMA user_version").fetchone()[0]
    assert version == CURRENT_SCHEMA_VERSION
    storage.close()


def test_edit_regenerate_and_branch_navigation_preserve_old_paths(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "chat.sqlite3")
    conversation = storage.create_conversation("Branches", "model/a", "")
    user = storage.add_message(conversation.id, "user", "Original")
    first_answer = storage.add_message(
        conversation.id, "assistant", "First", model_id="model/a"
    )

    edited = storage.edit_user_message(user.id or 0, "Edited")
    edited_answer = storage.add_message(
        conversation.id, "assistant", "Edited answer", model_id="model/a"
    )
    assert edited.parent_message_id is None
    assert edited_answer.parent_message_id == edited.id
    assert [message.content for message in storage.list_messages(conversation.id)] == [
        "Edited",
        "Edited answer",
    ]

    storage.activate_branch_from(first_answer.id or 0)
    regenerated = storage.add_message(
        conversation.id,
        "assistant",
        "Second",
        parent_message_id=user.id,
        model_id="model/b",
    )
    assert regenerated.parent_message_id == first_answer.parent_message_id
    assert [message.content for message in storage.list_all_messages(conversation.id)] == [
        "Original",
        "First",
        "Edited",
        "Edited answer",
        "Second",
    ]
    assert storage.branch_position(regenerated.id or 0) == (2, 2)
    storage.close()


def test_search_pin_archive_models_aliases_and_presets(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "chat.sqlite3")
    first = storage.create_conversation("Alpha", "model/a", "")
    second = storage.create_conversation("Beta", "model/b", "")
    storage.add_message(first.id, "user", "a very distinctive phrase")
    storage.pin_conversation(second.id, True)
    assert [item.id for item in storage.list_conversations()] == [second.id, first.id]
    assert [item.id for item in storage.list_conversations(query="distinctive")] == [first.id]
    storage.archive_conversation(first.id, True)
    assert first.id not in [item.id for item in storage.list_conversations()]
    assert [item.id for item in storage.list_conversations(archived=True)] == [first.id]

    models = [ModelOption("vendor/model", "Model", fetched_at="2026-01-01T00:00:00+00:00")]
    storage.replace_model_catalog(models, "2026-01-01T00:00:00+00:00")
    storage.set_model_favorite("vendor/model", True)
    storage.record_recent_model("vendor/model")
    storage.save_model_alias("cheap", "vendor/model")
    assert storage.list_catalog_models()[0].id == "vendor/model"
    assert storage.favorite_model_ids() == ["vendor/model"]
    assert storage.recent_model_ids() == ["vendor/model"]
    assert storage.resolve_model_id("CHEAP") == "vendor/model"

    preset = storage.save_prompt_preset("Concise", "Answer briefly")
    storage.update_conversation(
        second.id,
        system_prompt=preset.content,
        prompt_preset_id=preset.id,
        prompt_preset_snapshot=preset.content,
    )
    assert storage.list_prompt_presets() == [preset]
    storage.close()


def test_settings_round_trip(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "chat.sqlite3")
    assert storage.get_setting("theme", "dark") == "dark"
    storage.set_setting("theme", "light")
    storage.set_setting("theme", "dark")
    assert storage.get_setting("theme") == "dark"
    storage.close()
