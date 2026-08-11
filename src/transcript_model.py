from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, Qt


class TranscriptListModel(QAbstractListModel):
    """Expose semantic transcript rows without resetting unchanged delegates."""

    RowDataRole = int(Qt.ItemDataRole.UserRole) + 1

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._items: list[dict[str, Any]] = []

    def roleNames(self) -> dict[int, bytes]:  # noqa: N802 - Qt API
        # Keep the row payload distinct from QML's implicit modelData context value.
        return {self.RowDataRole: b"rowData"}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008, N802
        return 0 if parent.isValid() else len(self._items)

    def data(
        self,
        index: QModelIndex,
        role: int = int(Qt.ItemDataRole.DisplayRole),
    ) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        if role in {self.RowDataRole, int(Qt.ItemDataRole.DisplayRole)}:
            return self._items[index.row()]
        return None

    def replace(self, values: Iterable[dict[str, Any]], *, reset: bool = False) -> None:
        items = [dict(item) for item in values]
        if reset or not self._can_update_incrementally(items):
            self.beginResetModel()
            self._items = items
            self.endResetModel()
            return

        shared = min(len(self._items), len(items))
        for row in range(shared):
            if self._items[row] == items[row]:
                continue
            self._items[row] = items[row]
            model_index = self.index(row, 0)
            self.dataChanged.emit(model_index, model_index, [self.RowDataRole])

        if len(items) > shared:
            self.beginInsertRows(QModelIndex(), shared, len(items) - 1)
            self._items.extend(items[shared:])
            self.endInsertRows()
        elif len(self._items) > shared:
            self.beginRemoveRows(QModelIndex(), shared, len(self._items) - 1)
            del self._items[shared:]
            self.endRemoveRows()

    def snapshot(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._items]

    def _can_update_incrementally(self, items: list[dict[str, Any]]) -> bool:
        if not self._items or not items:
            return not self._items and not items

        shared = min(len(self._items), len(items))
        old_keys = [str(item.get("key") or "") for item in self._items[:shared]]
        new_keys = [str(item.get("key") or "") for item in items[:shared]]
        if old_keys == new_keys:
            return True

        # A completed generation replaces only the final live row with its durable
        # assistant row. Preserve every preceding delegate in that common case.
        return len(self._items) == len(items) and old_keys[:-1] == new_keys[:-1]
