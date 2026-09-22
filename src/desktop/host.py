from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .conversations import ConversationService
    from .generation import GenerationService
    from .preferences import SettingsService
    from .runtime import RuntimeService
    from .staging_publication import StagingPublicationService


class SignalLike(Protocol):
    def emit(self, *args: Any) -> None: ...


class BridgeHost(Protocol):
    """What services may use from the QML adapter that composes them.

    Services report to QML only through the adapter's signals and status line, reach sibling
    services through the named attributes, and ask the user through ``confirm``. Tests supply a
    small fake with the same attributes.
    """

    stateChanged: SignalLike
    settingsChanged: SignalLike
    conversationsChanged: SignalLike
    transcriptChanged: SignalLike
    modelsChanged: SignalLike
    permissionsChanged: SignalLike
    commandsChanged: SignalLike
    skillsChanged: SignalLike
    presetsChanged: SignalLike
    focusComposerRequested: SignalLike
    infoRequested: SignalLike
    errorRequested: SignalLike
    approvalRequested: SignalLike
    runtimeSetupStarted: SignalLike
    runtimeSetupFinished: SignalLike
    fileExported: SignalLike
    fileImported: SignalLike

    status_text: str
    runtime: RuntimeService
    settings: SettingsService
    history: ConversationService
    generation: GenerationService
    staging: StagingPublicationService

    def set_status(self, text: str) -> None: ...

    def confirm(self, token: str, title: str, body: str, on_accept: Callable[[], None], on_decline: Callable[[], None] | None = None) -> None: ...

    def reload_models(self) -> None: ...

    def loadPermissionRules(self) -> None: ...
