from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer, QUrl

from ..context_utils import compact_messages, context_source, estimate_tokens
from ..controllers import ConversationController
from ..import_export import ImportExportError, export_to_file, import_from_file
from ..markdown_renderer import render_markdown
from ..models import DEFAULT_MODELS, Conversation, model_context_length
from ..storage import Storage
from ..transcript import (
    ACTIVITY_EVENT_TYPES,
    ACTIVITY_PAYLOAD_KEYS,
    assemble_activities,
    assemble_transcript,
)
from ..transcript_model import TranscriptListModel
from .host import BridgeHost


class ConversationService(QObject):
    """Conversation list and selection, the active branch's transcript, conversation
    housekeeping, import/export, and context inspection.

    Every mutation is refused while a generation is running so a stream never lands in a
    conversation the user has since changed.
    """

    def __init__(self, host: BridgeHost, storage: Storage):
        super().__init__(host)  # type: ignore[arg-type]
        self.host = host
        self.storage = storage
        self.controller = ConversationController(storage)
        self.current_id: str | None = None
        self.show_archived = False
        self.search_query = ""
        self.conversations: list[dict[str, Any]] = []
        self.transcript: list[dict[str, Any]] = []
        self.transcript_model = TranscriptListModel(host)  # type: ignore[arg-type]
        self._html_cache: dict[tuple[str, str], str] = {}
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(50)
        self._refresh_timer.timeout.connect(self.refresh_transcript)

    @property
    def _busy(self) -> bool:
        return self.host.generation.running

    def current(self) -> Conversation | None:
        return self.storage.get_conversation(self.current_id) if self.current_id else None

    def current_model(self) -> str:
        conversation = self.current()
        return conversation.model if conversation else DEFAULT_MODELS[0].id

    # -- list and selection ------------------------------------------------------------

    def load(self, select_id: str | None = None) -> None:
        previous_id = self.current_id
        conversations = self.storage.list_conversations(query=self.search_query, archived=self.show_archived)
        self.conversations = [
            {
                "id": item.id,
                "title": item.title,
                "pinned": bool(item.pinned_at),
                "archived": bool(item.archived_at),
                "model": item.model,
                "updatedAt": item.updated_at,
            }
            for item in conversations
        ]
        if not conversations and not self.search_query and not self.show_archived:
            conversation = self.controller.create_new(self.current(), self.storage.recent_model_ids(1))
            self.load(conversation.id)
            return
        ids = {item.id for item in conversations}
        wanted = select_id or self.current_id
        self.current_id = wanted if wanted in ids else (conversations[0].id if conversations else None)
        self.host.conversationsChanged.emit()
        self.refresh_transcript(force_reset=self.current_id != previous_id)
        self.host.stateChanged.emit()
        self.host.settingsChanged.emit()

    def new_conversation(self) -> None:
        if self._busy:
            return
        conversation = self.controller.create_new(self.current(), self.storage.recent_model_ids(1))
        self.current_id = conversation.id
        self.show_archived = False
        self.search_query = ""
        self.load(conversation.id)
        self.host.focusComposerRequested.emit()

    def select(self, conversation_id: str) -> None:
        if self._busy or not self.storage.get_conversation(conversation_id):
            return
        self.current_id = conversation_id
        self.refresh_transcript(force_reset=True)
        self.host.stateChanged.emit()
        self.host.settingsChanged.emit()

    def set_search(self, query: str) -> None:
        self.search_query = query.strip()
        self.load()

    def toggle_archived_view(self) -> None:
        if self._busy:
            return
        self.show_archived = not self.show_archived
        self.current_id = None
        self.load()
        self.host.set_status("Archived conversations" if self.show_archived else "Ready")

    # -- housekeeping ------------------------------------------------------------------

    def rename(self, title: str) -> None:
        conversation = self.current()
        title = title.strip()
        if conversation and title and not self._busy:
            self.storage.update_conversation(conversation.id, title=title[:200])
            self.load(conversation.id)

    def toggle_pin(self) -> None:
        conversation = self.current()
        if conversation and not self._busy:
            self.storage.pin_conversation(conversation.id, not bool(conversation.pinned_at))
            self.load(conversation.id)

    def toggle_archive(self) -> None:
        conversation = self.current()
        if conversation and not self._busy:
            self.storage.archive_conversation(conversation.id, not bool(conversation.archived_at))
            self.current_id = None
            self.load()

    def request_delete(self) -> None:
        conversation = self.current()
        if not conversation or self._busy:
            return

        def delete() -> None:
            self.storage.delete_conversation(conversation.id)
            self.current_id = None
            self.load()

        self.host.confirm(f"delete:{conversation.id}", "Delete conversation?", f"Delete “{conversation.title}”? This cannot be undone.", delete)

    def fork(self) -> None:
        conversation = self.current()
        if conversation and not self._busy:
            fork = self.controller.fork(conversation.id)
            self.current_id = fork.id
            self.load(fork.id)
            self.host.set_status("Conversation forked")

    def select_model(self, model_id: str) -> None:
        conversation = self.current()
        model_id = self.storage.resolve_model_id(model_id.strip())
        if not conversation or not model_id or self._busy:
            return
        self.storage.update_conversation(conversation.id, model=model_id)
        self.storage.record_recent_model(model_id)
        self.host.reload_models()
        self.host.stateChanged.emit()
        self.host.settingsChanged.emit()

    def save_system_prompt(self, prompt: str) -> None:
        conversation = self.current()
        if conversation and not self._busy:
            self.storage.update_conversation(conversation.id, system_prompt=prompt[:128_000])
            self.host.settingsChanged.emit()
            self.host.set_status("System prompt saved")

    def apply_prompt_preset(self, preset_id: str) -> None:
        conversation = self.current()
        preset = self.storage.get_prompt_preset(preset_id)
        if conversation and preset:
            self.storage.update_conversation(conversation.id, system_prompt=preset.content, prompt_preset_id=preset.id, prompt_preset_snapshot=preset.content)
            self.host.settingsChanged.emit()

    def navigate_branch(self, message_id: int, direction: int) -> None:
        if not self._busy and self.controller.branch_target(message_id, direction):
            self.refresh_transcript(force_reset=True)

    # -- import and export -------------------------------------------------------------

    def import_file(self, value: str) -> None:
        path = local_path(value)
        if not path or self._busy:
            return
        try:
            conversation_id = import_from_file(self.storage, path)
        except (OSError, UnicodeError, ImportExportError) as exc:
            self.host.errorRequested.emit("Import failed", str(exc))
            return
        self.current_id = conversation_id
        self.show_archived = False
        self.search_query = ""
        self.load(conversation_id)
        self.host.fileImported.emit(path.name)

    def export_file(self, value: str, format_name: str = "json") -> None:
        conversation = self.current()
        path = local_path(value)
        if not conversation or not path or self._busy:
            return
        if not path.suffix:
            path = path.with_suffix(".md" if format_name == "markdown" else ".json")
        try:
            export_to_file(self.storage, conversation.id, path)
        except (OSError, ImportExportError) as exc:
            self.host.errorRequested.emit("Export failed", str(exc))
            return
        self.host.fileExported.emit(path.name)

    # -- context and usage -------------------------------------------------------------

    def compact_context(self) -> None:
        conversation = self.current()
        if not conversation:
            return
        limit = max(4_000, int(model_context_length(conversation.model, self.host.settings.catalog.models()) * 0.4))
        _context, summary, covered = compact_messages(context_source(conversation.system_prompt, self.storage.list_messages(conversation.id)), limit)
        if not covered:
            self.host.infoRequested.emit("Context", "The active branch is already below the compaction target.")
            return
        self.storage.save_compaction(conversation.id, conversation.active_leaf_id, covered, summary, conversation.model)
        self.host.set_status(f"Compacted {len(covered)} older message(s)")

    def inspect_context(self) -> None:
        conversation = self.current()
        if not conversation:
            return
        messages = self.storage.list_messages(conversation.id)
        estimate = sum(estimate_tokens(item.content) for item in messages)
        limit = int(model_context_length(conversation.model, self.host.settings.catalog.models()) * 0.8)
        compactions = self.storage.list_compactions(conversation.id)
        latest = compactions[-1]["summary"] if compactions else "No saved compaction."
        self.host.infoRequested.emit(
            "Context inspection",
            f"Active branch: {len(messages)} messages\n"
            f"Estimated text tokens: {estimate:,}\n"
            f"Submission limit: {limit:,}\n"
            f"Saved compactions: {len(compactions)}\n\n"
            f"{str(latest)[:6000]}",
        )

    def show_usage(self) -> None:
        total = self.storage.usage_summary()
        current = self.storage.usage_summary(self.current_id)
        self.host.infoRequested.emit("Usage", f"Current conversation\n{usage_text(current)}\n\nAll conversations\n{usage_text(total)}")

    # -- transcript --------------------------------------------------------------------

    def schedule_transcript_refresh(self) -> None:
        self._refresh_timer.start()

    def refresh_transcript(self, *, force_reset: bool = False) -> None:
        self._refresh_timer.stop()
        conversation = self.current()
        if not conversation:
            self.transcript = []
            self._html_cache = {}
        else:
            generation = self.host.generation
            activities = assemble_activities(
                self.storage.list_generation_runs(conversation.id),
                self.storage.list_run_events(conversation.id, event_types=ACTIVITY_EVENT_TYPES, payload_keys=ACTIVITY_PAYLOAD_KEYS),
            )
            values = assemble_transcript(
                self.storage.list_messages(conversation.id),
                activities,
                live_text="",
                live_events=(generation.live_events if generation.running else ()),
            )
            html_cache: dict[tuple[str, str], str] = {}
            for item in values:
                content = str(item.get("content") or "")
                cache_key = (str(item.get("key") or ""), content)
                html = self._html_cache.get(cache_key)
                if html is None:
                    html = render_markdown(content)
                item["html"] = html
                html_cache[cache_key] = html
            self._html_cache = html_cache
            self.transcript = values
            summary = self.storage.usage_summary(conversation.id)
            if not generation.running and (summary["input_tokens"] or summary["output_tokens"] or summary["cost"]):
                self.host.status_text = usage_text(summary)
        self.transcript_model.replace(self.transcript, reset=force_reset)
        self.host.transcriptChanged.emit()
        self.host.stateChanged.emit()


def local_path(value: str) -> Path | None:
    local = QUrl(value).toLocalFile() if value.startswith("file:") else value
    return Path(local) if local else None


def usage_text(usage: dict[str, Any]) -> str:
    prompt = int(usage.get("input_tokens") or 0)
    completion = int(usage.get("output_tokens") or 0)
    cached = int(usage.get("cached_tokens") or 0)
    reasoning = int(usage.get("reasoning_tokens") or 0)
    cost = float(usage.get("cost") or 0)
    parts = [f"{prompt + completion:,} billed tokens", f"USD {cost:.6f}"]
    if cached:
        parts.append(f"{cached:,} cached")
    if reasoning:
        parts.append(f"{reasoning:,} reasoning")
    return " · ".join(parts)
