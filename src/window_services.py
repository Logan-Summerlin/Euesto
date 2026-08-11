from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, QTimer
from PySide6.QtGui import QWindow
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon


class TrayService:
    """Keep tray lifetime and menu wiring out of the window's application logic."""

    def __init__(
        self,
        parent: QObject,
        *,
        show_quick_chat: Callable[[], None],
        toggle_visibility: Callable[[], None],
        close: Callable[[], None],
    ):
        self.icon: QSystemTrayIcon | None = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.icon = QSystemTrayIcon(QApplication.windowIcon(), parent)
        menu = QMenu()
        menu.addAction("Quick chat", show_quick_chat)
        menu.addAction("Show / hide", toggle_visibility)
        menu.addSeparator()
        menu.addAction("Quit", close)
        self.icon.setContextMenu(menu)
        self.icon.setToolTip("Local OpenRouter Chat · Ctrl+Alt+Space")
        self.icon.activated.connect(
            lambda reason: self._activated(reason, toggle_visibility)
        )
        self.icon.show()

    @staticmethod
    def _activated(
        reason: QSystemTrayIcon.ActivationReason, toggle_visibility: Callable[[], None]
    ) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            toggle_visibility()

    def close(self) -> None:
        if self.icon:
            self.icon.hide()


class GlobalQuickChatHotkey(QAbstractNativeEventFilter):
    HOTKEY_ID = 0x4C43

    def __init__(self, window: QWindow, callback: Callable[[], None]):
        super().__init__()
        self.window = window
        self.callback = callback
        self.registered = False

    def register(self) -> None:
        if sys.platform != "win32" or self.registered:
            return
        try:
            import ctypes

            modifiers = 0x0001 | 0x0002 | 0x4000  # ALT | CTRL | NOREPEAT
            self.registered = bool(
                ctypes.windll.user32.RegisterHotKey(  # type: ignore[attr-defined]
                    int(self.window.winId()), self.HOTKEY_ID, modifiers, 0x20
                )
            )
        except (AttributeError, OSError, TypeError):
            self.registered = False

    def nativeEventFilter(self, event_type: Any, message: Any) -> tuple[bool, int]:  # noqa: N802
        if sys.platform != "win32":
            return False, 0
        try:
            from ctypes import wintypes

            msg = wintypes.MSG.from_address(int(message))
            if msg.message == 0x0312 and msg.wParam == self.HOTKEY_ID:
                QTimer.singleShot(0, self.callback)
                return True, 0
        except (ValueError, TypeError):
            pass
        return False, 0

    def unregister(self) -> None:
        if sys.platform != "win32" or not self.registered:
            return
        try:
            import ctypes

            ctypes.windll.user32.UnregisterHotKey(  # type: ignore[attr-defined]
                int(self.window.winId()), self.HOTKEY_ID
            )
        except (AttributeError, OSError, TypeError):
            pass
        finally:
            self.registered = False
