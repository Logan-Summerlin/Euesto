from __future__ import annotations

from src.transcript_model import TranscriptListModel


def _row(key: str, content: str = "text") -> dict[str, object]:
    return {"key": key, "content": content, "role": "assistant"}


def test_transcript_model_appends_and_updates_without_resetting() -> None:
    model = TranscriptListModel()
    assert model.roleNames()[model.RowDataRole] == b"rowData"
    resets: list[None] = []
    inserts: list[tuple[int, int]] = []
    changes: list[tuple[int, int]] = []
    model.modelReset.connect(lambda: resets.append(None))
    model.rowsInserted.connect(
        lambda _parent, first, last: inserts.append((first, last))
    )
    model.dataChanged.connect(
        lambda first, last, _roles: changes.append((first.row(), last.row()))
    )

    model.replace([_row("one"), _row("stream")], reset=True)
    resets.clear()

    model.replace([_row("one"), _row("stream"), _row("three")])
    assert inserts == [(2, 2)]
    assert resets == []

    model.replace([_row("one"), _row("stream", "updated"), _row("three")])
    assert changes == [(1, 1)]
    assert resets == []


def test_transcript_model_replaces_only_the_completed_live_row() -> None:
    model = TranscriptListModel()
    changes: list[tuple[int, int]] = []
    resets: list[None] = []
    model.dataChanged.connect(
        lambda first, last, _roles: changes.append((first.row(), last.row()))
    )
    model.modelReset.connect(lambda: resets.append(None))
    model.replace([_row("one"), _row("stream")], reset=True)
    resets.clear()

    model.replace([_row("one"), _row("message-2", "done")])

    assert changes == [(1, 1)]
    assert resets == []
    assert model.snapshot()[-1]["key"] == "message-2"


def test_transcript_model_resets_for_an_unrelated_branch() -> None:
    model = TranscriptListModel()
    resets: list[None] = []
    model.modelReset.connect(lambda: resets.append(None))
    model.replace([_row("one"), _row("two")], reset=True)
    resets.clear()

    model.replace([_row("other-one"), _row("other-two")])

    assert resets == [None]
    assert [item["key"] for item in model.snapshot()] == ["other-one", "other-two"]
