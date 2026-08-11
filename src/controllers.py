from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from shared.coercion import optional_float, optional_int, optional_string

from .context_utils import compact_messages, estimate_tokens
from .models import (
    DEFAULT_MODELS,
    DEFAULT_SYSTEM_PROMPT,
    Conversation,
    Message,
    ModelOption,
    RequestOptions,
    model_context_length,
)
from .storage import Storage


@dataclass(frozen=True, slots=True)
class PreparedGeneration:
    messages: list[dict[str, Any]]
    context_limit: int
    estimated_tokens: int
    removed_messages: int
    supported_parameters: frozenset[str]


@dataclass(slots=True)
class QueuedInput:
    text: str
    steered: bool = False


@dataclass(slots=True)
class GenerationState:
    conversation_id: str | None = None
    parent_message_id: int | None = None
    model: str = ""
    mode: str = "chat"
    run_id: str | None = None
    stream_chunks: list[str] = field(default_factory=list)
    cancel_requested: bool = False
    pending_inputs: deque[QueuedInput] = field(default_factory=deque)

    @property
    def stream_text(self) -> str:
        return "".join(self.stream_chunks)

    def clear_stream(self) -> None:
        self.stream_chunks.clear()


class ConversationController:
    """Unit-testable conversation mutations used by the desktop view."""

    def __init__(self, storage: Storage):
        self.storage = storage

    def create_new(
        self, current: Conversation | None, recent_models: list[str]
    ) -> Conversation:
        model = (
            current.model
            if current
            else (recent_models[0] if recent_models else DEFAULT_MODELS[0].id)
        )
        return self.storage.create_conversation(
            "New conversation", model, DEFAULT_SYSTEM_PROMPT
        )

    def add_user_turn(self, conversation_id: str, text: str, model: str) -> Message:
        self._require(conversation_id)
        self.storage.update_conversation(conversation_id, model=model)
        self.storage.record_recent_model(model)
        message = self.storage.add_message(conversation_id, "user", text)
        if not any(
            item.role == "user"
            for item in self.storage.list_all_messages(conversation_id)
            if item.id != message.id
        ):
            self.storage.update_conversation(conversation_id, title=_short_title(text))
        return message

    def fork(self, conversation_id: str) -> Conversation:
        source = self._require(conversation_id)
        fork = self.storage.create_conversation(
            f"{source.title} (fork)",
            source.model,
            source.system_prompt,
            prompt_preset_id=source.prompt_preset_id,
            prompt_preset_snapshot=source.prompt_preset_snapshot,
        )
        parent: int | None = None
        for message in self.storage.list_messages(conversation_id):
            copied = self.storage.add_message(
                fork.id,
                message.role,
                message.content,
                parent_message_id=parent,
                model_id=message.model_id,
                provider_id=message.provider_id,
                finish_reason=message.finish_reason,
                input_tokens=message.input_tokens,
                output_tokens=message.output_tokens,
                cached_tokens=message.cached_tokens,
                reasoning_tokens=message.reasoning_tokens,
                total_tokens=message.total_tokens,
                cost=message.cost,
                time_to_first_token=message.time_to_first_token,
                elapsed_seconds=message.elapsed_seconds,
                tokens_per_second=message.tokens_per_second,
            )
            parent = copied.id
        return fork

    def branch_target(self, message_id: int, direction: int) -> str | None:
        message = self.storage.get_message(message_id)
        if message is None:
            return None
        siblings = self.storage.siblings(message_id)
        ids = [item.id for item in siblings]
        if message_id not in ids:
            return None
        index = ids.index(message_id) + direction
        if not 0 <= index < len(ids) or ids[index] is None:
            return None
        self.storage.activate_branch_from(int(ids[index]))
        return message.conversation_id

    def branch_view(
        self, conversation_id: str
    ) -> tuple[list[Message], dict[int, tuple[int, int]]]:
        messages = self.storage.list_messages(conversation_id)
        positions = {
            message.id: self.storage.branch_position(message.id)
            for message in messages
            if message.id is not None
        }
        return messages, positions

    def _require(self, conversation_id: str) -> Conversation:
        conversation = self.storage.get_conversation(conversation_id)
        if conversation is None:
            raise ValueError("Conversation does not exist")
        return conversation


class GenerationController:
    """Own generation state, request preparation, queueing, and run persistence."""

    def __init__(self, storage: Storage):
        self.storage = storage
        self.state = GenerationState()

    def prepare(
        self,
        conversation: Conversation,
        context_messages: list[Message],
        model: str,
        catalog: list[ModelOption],
    ) -> PreparedGeneration:
        raw: list[dict[str, Any]] = []
        if conversation.system_prompt:
            raw.append({"role": "system", "content": conversation.system_prompt})
        raw.extend(
            {"role": item.role, "content": item.content, "_message_id": item.id}
            for item in context_messages
        )
        context_limit = int(model_context_length(model, catalog) * 0.8)
        messages, inspection, covered = compact_messages(raw, context_limit)
        if covered:
            existing = self.storage.list_compactions(conversation.id)
            if not existing or existing[-1].get("covered_message_ids") != covered:
                self.storage.save_compaction(
                    conversation.id,
                    conversation.active_leaf_id,
                    covered,
                    inspection.summary,
                    model,
                )
        model_metadata = next((item for item in catalog if item.id == model), None)
        return PreparedGeneration(
            messages=messages,
            context_limit=context_limit,
            estimated_tokens=sum(estimate_tokens(item["content"]) for item in messages),
            removed_messages=inspection.compacted_messages,
            supported_parameters=(
                model_metadata.supported_parameters if model_metadata else frozenset()
            ),
        )

    def begin(
        self, conversation_id: str, parent_message_id: int | None, model: str, mode: str
    ) -> None:
        pending = self.state.pending_inputs
        self.state = GenerationState(
            conversation_id=conversation_id,
            parent_message_id=parent_message_id,
            model=model,
            mode=mode,
            pending_inputs=pending,
        )

    def start_run(self, run_id: str) -> None:
        state = self.state
        if not state.conversation_id or not run_id:
            return
        state.run_id = run_id
        self.storage.start_generation_run(
            run_id,
            state.conversation_id,
            state.parent_message_id,
            state.mode,
            state.model,
        )

    def save_event(self, event: object) -> None:
        if self.state.conversation_id:
            self.storage.save_run_event(self.state.conversation_id, event)

    def append(self, text: str) -> None:
        self.state.stream_chunks.append(text)

    def save_assistant(
        self,
        usage: dict[str, Any],
        *,
        finish_reason: str | None = None,
        status: str = "completed",
    ) -> int | None:
        state = self.state
        content = state.stream_text
        if not state.conversation_id or not content:
            return None
        message = self.storage.add_message(
            state.conversation_id,
            "assistant",
            content,
            parent_message_id=state.parent_message_id,
            model_id=str(usage.get("actual_model") or state.model),
            provider_id=optional_string(usage.get("provider")),
            finish_reason=finish_reason or optional_string(usage.get("finish_reason")),
            input_tokens=optional_int(usage.get("prompt_tokens")),
            output_tokens=optional_int(usage.get("completion_tokens")),
            cached_tokens=optional_int(usage.get("cached_tokens")),
            reasoning_tokens=optional_int(usage.get("reasoning_tokens")),
            total_tokens=optional_int(usage.get("total_tokens")),
            cost=optional_float(usage.get("cost")),
            time_to_first_token=optional_float(usage.get("time_to_first_token")),
            elapsed_seconds=optional_float(usage.get("elapsed_seconds")),
            tokens_per_second=optional_float(usage.get("tokens_per_second")),
        )
        if state.run_id:
            self.storage.finish_generation_run(
                state.run_id,
                status,
                assistant_message_id=message.id,
            )
        return message.id

    def finish_without_message(self, status: str, error: str | None = None) -> None:
        if self.state.run_id:
            self.storage.finish_generation_run(self.state.run_id, status, error=error)

    def request_cancel(self) -> None:
        self.state.cancel_requested = True

    def enqueue(self, text: str, steered: bool = False, *, front: bool = False) -> int:
        item = QueuedInput(text, steered)
        if front:
            self.state.pending_inputs.appendleft(item)
        else:
            self.state.pending_inputs.append(item)
        return len(self.state.pending_inputs)

    def next_input(self) -> QueuedInput | None:
        return self.state.pending_inputs.popleft() if self.state.pending_inputs else None

    @property
    def pending_count(self) -> int:
        return len(self.state.pending_inputs)

    @staticmethod
    def request_options(storage: Storage, model_id: str) -> RequestOptions:
        raw = storage.get_setting(f"model_options:{model_id}")
        if not raw:
            return RequestOptions()
        try:
            data = json.loads(raw)
            return RequestOptions(
                max_tokens=optional_int(data.get("max_tokens")),
                reasoning_effort=optional_string(data.get("reasoning_effort")),
                temperature=optional_float(data.get("temperature")),
                top_p=optional_float(data.get("top_p")),
                stop=[str(item) for item in data.get("stop", [])],
                data_collection=("allow" if data.get("data_collection") == "allow" else "deny"),
                zero_data_retention=bool(data.get("zero_data_retention", False)),
            )
        except (json.JSONDecodeError, TypeError, AttributeError):
            return RequestOptions()

    @staticmethod
    def format_usage(usage: dict[str, object], cancelled: bool) -> str:
        prompt = optional_int(usage.get("prompt_tokens")) or 0
        completion = optional_int(usage.get("completion_tokens")) or 0
        cached = optional_int(usage.get("cached_tokens")) or 0
        reasoning = optional_int(usage.get("reasoning_tokens")) or 0
        cost = optional_float(usage.get("cost"))
        parts = ["Stopped" if cancelled else "Done"]
        if prompt or completion:
            parts.append(f"{prompt + completion:,} tokens")
        if cached:
            parts.append(f"{cached:,} cached")
        if reasoning:
            parts.append(f"{reasoning:,} reasoning")
        if cost is not None:
            parts.append(f"${cost:.6f}")
        return " · ".join(parts)


def _short_title(text: str) -> str:
    title = " ".join(text.split())[:54]
    return title + ("…" if len(text) > 54 else "")
