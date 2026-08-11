from pathlib import Path
from types import SimpleNamespace

from src.controllers import ConversationController, GenerationController
from src.models import ModelOption
from src.storage import Storage


def test_conversation_controller_keeps_branch_and_turn_rules_out_of_the_window(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "chat.sqlite3")
    controller = ConversationController(storage)
    conversation = controller.create_new(None, [])

    user = controller.add_user_turn(conversation.id, "A useful first question", "model/a")
    assert user.parent_message_id is None
    assert storage.get_conversation(conversation.id).title == "A useful first question"

    answer = storage.add_message(conversation.id, "assistant", "First", model_id="model/a")
    edited = storage.edit_user_message(user.id or 0, "A revised question")
    storage.add_message(conversation.id, "assistant", "Second", parent_message_id=edited.id)
    assert controller.branch_target(answer.id or 0, 1) is None
    assert controller.branch_target(edited.id or 0, -1) == conversation.id
    storage.close()


def test_generation_controller_persists_compaction_and_assistant_link(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "chat.sqlite3")
    conversation = storage.create_conversation("Chat", "model/a", "System")
    user = storage.add_message(conversation.id, "user", "Question")
    controller = GenerationController(storage)
    controller.begin(conversation.id, user.id, "model/a", "chat")
    controller.start_run("run-1")
    controller.save_event(
        SimpleNamespace(
            run_id="run-1",
            event_id=1,
            type="run.created",
            payload={"mode": "chat"},
            created_at="2026-01-01T00:00:00+00:00",
        )
    )
    controller.append("Answer")
    assistant_id = controller.save_assistant(
        {"prompt_tokens": 3, "completion_tokens": 2, "actual_model": "model/a"}
    )

    run = storage.generation_run("run-1")
    assert run is not None
    assert run["status"] == "completed"
    assert run["parent_message_id"] == user.id
    assert run["assistant_message_id"] == assistant_id
    assert storage.list_run_events(conversation.id)[0]["run_id"] == "run-1"
    storage.close()


def test_generation_request_preparation_has_one_compaction_boundary(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "chat.sqlite3")
    conversation = storage.create_conversation("Chat", "model/a", "System")
    for text in ("one " * 2_000, "two " * 2_000, "three"):
        storage.add_message(conversation.id, "user", text)
    controller = GenerationController(storage)
    prepared = controller.prepare(
        conversation,
        storage.list_messages(conversation.id),
        "model/a",
        [ModelOption("model/a", "A", context_length=4_000)],
    )
    assert prepared.removed_messages > 0
    assert len(storage.list_compactions(conversation.id)) == 1
    storage.close()


def test_generation_queue_survives_each_new_run(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "chat.sqlite3")
    controller = GenerationController(storage)
    controller.enqueue("first")
    controller.enqueue("second")

    controller.begin("conversation", 1, "model/a", "chat")
    assert controller.next_input().text == "first"  # type: ignore[union-attr]
    controller.begin("conversation", 2, "model/a", "chat")
    assert controller.next_input().text == "second"  # type: ignore[union-attr]
    storage.close()
