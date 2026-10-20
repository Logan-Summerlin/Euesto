import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.import_export import (
    ImportExportError,
    export_json,
    export_markdown,
    import_json,
    import_markdown,
)
from src.storage import Storage


def _branched_conversation(storage: Storage) -> str:
    conversation = storage.create_conversation("Tree", "model/original", "System")
    user = storage.add_message(conversation.id, "user", "Question")
    first = storage.add_message(
        conversation.id,
        "assistant",
        "First",
        parent_message_id=user.id,
        model_id="model/one",
        provider_id="provider-a",
        input_tokens=10,
        output_tokens=4,
        cached_tokens=2,
        reasoning_tokens=1,
        cost=0.25,
    )
    storage.add_message(
        conversation.id,
        "assistant",
        "Second",
        parent_message_id=user.id,
        model_id="model/two",
        input_tokens=10,
        output_tokens=5,
        cost=0.5,
    )
    storage.set_active_leaf(conversation.id, first.id)
    return conversation.id


def test_json_round_trip_preserves_tree_active_branch_and_usage(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "source.sqlite3")
    conversation_id = _branched_conversation(storage)
    exported = export_json(storage, conversation_id)

    target = Storage(tmp_path / "target.sqlite3")
    imported_id = import_json(target, exported)
    all_messages = target.list_all_messages(imported_id)
    assert [message.content for message in all_messages] == ["Question", "First", "Second"]
    assert all_messages[1].parent_message_id == all_messages[0].id
    assert all_messages[2].parent_message_id == all_messages[0].id
    assert [message.content for message in target.list_messages(imported_id)] == [
        "Question",
        "First",
    ]
    assert target.usage_summary(imported_id) == {
        "input_tokens": 20,
        "output_tokens": 9,
        "cached_tokens": 2,
        "reasoning_tokens": 1,
        "cost": 0.75,
    }
    storage.close()
    target.close()


def test_json_round_trip_preserves_v05_compactions_and_run_events(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "source.sqlite3")
    conversation_id = _branched_conversation(storage)
    leaf = storage.get_conversation(conversation_id).active_leaf_id
    storage.save_compaction(conversation_id, leaf, [1], "important summary", "model/one")
    storage.save_run_event(
        conversation_id,
        SimpleNamespace(
            run_id="run",
            event_id=1,
            type="tool.started",
            payload={"request_id": "call", "tool": "read_file"},
            created_at="2026-01-01",
        ),
    )
    exported = export_json(storage, conversation_id)
    target = Storage(tmp_path / "target.sqlite3")
    imported_id = import_json(target, exported)
    assert target.list_compactions(imported_id)[0]["summary"] == "important summary"
    assert target.list_run_events(imported_id)[0]["payload"]["tool"] == "read_file"
    storage.close()
    target.close()


def test_markdown_round_trip_preserves_active_branch_usage(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "source.sqlite3")
    conversation_id = _branched_conversation(storage)
    exported = export_markdown(storage, conversation_id)
    assert "## You" in exported and "## Assistant" in exported

    target = Storage(tmp_path / "target.sqlite3")
    imported_id = import_markdown(target, exported)
    assert [message.content for message in target.list_messages(imported_id)] == [
        "Question",
        "First",
    ]
    assert target.usage_summary(imported_id)["cost"] == 0.25
    storage.close()
    target.close()


def _export(messages: list[dict], **conversation: object) -> str:
    return json.dumps({
        "format": "local-openrouter-chat",
        "schema_version": 3,
        "conversation": {"title": "Imported", **conversation},
        "messages": messages,
    })


def test_json_import_inserts_children_listed_before_their_parents(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "chat.sqlite3")
    source = _export(
        [
            {"id": 9, "role": "assistant", "content": "Answer", "parent_message_id": 4},
            {"id": 4, "role": "user", "content": "Question", "parent_message_id": None},
        ],
        active_leaf_id=9,
    )

    imported_id = import_json(storage, source)

    messages = storage.list_messages(imported_id)
    assert [message.content for message in messages] == ["Question", "Answer"]
    assert messages[1].parent_message_id == messages[0].id
    storage.close()


@pytest.mark.parametrize(
    ("messages", "error"),
    [
        ([{"id": 1, "role": "user", "parent_message_id": 7}], "missing parent"),
        (
            [
                {"id": 1, "role": "user", "parent_message_id": 2},
                {"id": 2, "role": "assistant", "parent_message_id": 1},
            ],
            "cycle",
        ),
        ([{"id": 1, "role": "user"}, {"id": 1, "role": "user"}], "unique"),
        ([{"id": 1, "role": "tool"}], "invalid role"),
    ],
)
def test_json_import_rejects_malformed_trees_before_writing(
    tmp_path: Path, messages: list[dict], error: str
) -> None:
    storage = Storage(tmp_path / "chat.sqlite3")

    with pytest.raises(ImportExportError, match=error):
        import_json(storage, _export(messages))

    assert storage.list_conversations() == []
    storage.close()
