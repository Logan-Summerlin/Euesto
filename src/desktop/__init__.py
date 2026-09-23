"""Desktop services composed by the QML adapter (``src/qml_backend.py``).

Each service owns one job and its state; ``DesktopBridge`` only exposes them to QML:

- ``RuntimeService`` — selected workspace, local runtime lifecycle, gateway health and token.
- ``SettingsService`` — preferences, model catalog, commands, presets, skills, workspace
  configuration, and saved permission rules.
- ``ConversationService`` — conversation list, selection, branch/transcript view, import/export,
  and context inspection.
- ``GenerationService`` — Chat/Plan/Agent runs, the session approval tier, tool approvals, and
  the queued-input loop.
- ``StagingPublicationService`` — staged-change review/discard and reviewed, batched host
  publication.
"""
from .conversations import ConversationService
from .generation import GenerationService
from .host import BridgeHost
from .preferences import SettingsService
from .runtime import RuntimeService
from .staging_publication import StagingPublicationService

__all__ = [
    "BridgeHost",
    "ConversationService",
    "GenerationService",
    "RuntimeService",
    "SettingsService",
    "StagingPublicationService",
]
