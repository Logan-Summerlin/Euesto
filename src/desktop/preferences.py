from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Slot

from shared.coercion import optional_float, optional_int, optional_string
from shared.requests import DEFAULT_INVESTIGATION_MODEL, WORKSPACE_CONFIG_KEYS

from ..extensions import available_skills
from ..gateway_client import DEFAULT_GATEWAY_URL, GatewayClient, GatewayError
from ..model_catalog import ModelCatalog, agent_capable, model_warnings, price_text
from ..models import RequestOptions
from ..runtime_manager import (
    DEFAULT_GIT_HISTORY_DEPTH,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_RUNNER_PIDS,
    RuntimeErrorMessage,
    RuntimeSettings,
    detect_runner_target,
)
from ..settings import app_data_dir, get_api_key, save_api_key
from ..storage import Storage
from ..workers import CatalogWorker
from .host import BridgeHost

SERVER_TOOLS = ("web_search", "web_fetch", "datetime")
REASONING_EFFORTS = frozenset({"minimal", "low", "medium", "high"})
BUILTIN_COMMANDS = (
    ("new", "Start a new conversation"),
    ("mode", "Switch Chat, Plan, or Agent mode"),
    ("fork", "Fork the active branch"),
    ("compact", "Compact older context"),
    ("context", "Inspect submitted context"),
    ("cost", "Show token and cost usage"),
    ("status", "Show local gateway status"),
    ("stop", "Stop the active run"),
    ("pause", "Pause the agent at a safe boundary"),
)
RUNTIME_SETTINGS_KEY = "runtime_settings"
# What the runner settings form saves; network mode and policy fields are applied per start.
RUNNER_SETTING_FIELDS = ("runner_target", "cpus", "memory_gb", "pids", "git_history_depth")
MAX_UNREDACTED_PATHS = 50
RETENTION_DAYS_KEY = "staged_session_retention_days"
RETENTION_DAYS_RANGE = (1, 365)
LEGACY_INVESTIGATION_DEFAULT = "deepseek/deepseek-chat-v3-0324"
LEGACY_INVESTIGATION_MIGRATED = "investigation_model_legacy_migrated"
INVESTIGATION_MODEL_ENTRY = {
    "id": DEFAULT_INVESTIGATION_MODEL,
    "label": "MiMo-V2.5",
    "description": "Xiaomi MiMo-V2.5",
    "contextLength": 1_000_000,
    "price": 0.21,
    "rank": None,
    "year": 2026,
    "favorite": False,
    "recent": False,
    "reasoning": True,
    "textCompatible": True,
    "tools": True,
    "priceText": "Prices load with the catalog",
    "warnings": [],
}


def _saved_investigation_entry(model_id: str) -> dict[str, Any]:
    return {
        **INVESTIGATION_MODEL_ENTRY,
        "id": model_id,
        "label": model_id,
        "description": "Saved repository investigation model",
        "contextLength": 128_000,
        "price": None,
        "year": None,
    }


def _request_options(values: dict[str, Any]) -> RequestOptions:
    """Per-model request options from saved JSON or the QML options dialog."""
    effort = optional_string(values.get("reasoning_effort"))
    stop = values.get("stop")
    return RequestOptions(
        max_tokens=optional_int(values.get("max_tokens")),
        reasoning_effort=effort if effort in REASONING_EFFORTS else None,
        temperature=optional_float(values.get("temperature")),
        top_p=optional_float(values.get("top_p")),
        stop=[str(item) for item in stop] if isinstance(stop, list) else [],
        data_collection="allow" if values.get("data_collection") == "allow" else "deny",
        zero_data_retention=bool(values.get("zero_data_retention", False)),
    )


class SettingsService(QObject):
    """Preferences, the model catalog, prompt commands and presets, skills, workspace
    configuration, and saved permission rules."""

    def __init__(
        self, host: BridgeHost, storage: Storage, *, catalog: ModelCatalog | None = None
    ):
        super().__init__(host)  # type: ignore[arg-type]
        self.host = host
        self.storage = storage
        self.catalog = catalog or ModelCatalog(storage)
        self.catalog_worker: CatalogWorker | None = None
        self.catalog_autorefresh_attempted = False
        self.permissions: list[dict[str, Any]] = []
        self.commands: list[dict[str, Any]] = []
        self.skills: list[dict[str, Any]] = []
        self.presets: list[dict[str, Any]] = []

    # -- simple preferences ------------------------------------------------------------

    @property
    def theme(self) -> str:
        return self.storage.get_setting("theme", "dark") or "dark"

    def set_theme(self, theme: str) -> None:
        self.storage.set_setting("theme", "light" if theme == "light" else "dark")
        self.host.settingsChanged.emit()

    def server_tools(self) -> dict[str, bool]:
        return {
            key: self.storage.get_setting(f"server_tool:{key}", "0") == "1"
            for key in SERVER_TOOLS
        }

    def set_server_tool(self, name: str, enabled: bool) -> None:
        if name in SERVER_TOOLS:
            self.storage.set_setting(f"server_tool:{name}", "1" if enabled else "0")
            self.host.settingsChanged.emit()

    @property
    def investigation_model(self) -> str:
        return self.storage.get_setting("investigation_model_id", "") or ""

    def ensure_investigation_model(self) -> str:
        """Return the saved investigation model, defaulting it to MiMo and migrating the legacy
        default once, so a later explicit choice of that model is kept."""
        value = self.investigation_model.strip()
        migrated = self.storage.get_setting(LEGACY_INVESTIGATION_MIGRATED, "") == "1"
        if not value or (value == LEGACY_INVESTIGATION_DEFAULT and not migrated):
            value = DEFAULT_INVESTIGATION_MODEL
            self.storage.set_setting("investigation_model_id", value)
        if not migrated:
            self.storage.set_setting(LEGACY_INVESTIGATION_MIGRATED, "1")
        return value

    def save_investigation_model(self, model_id: str) -> None:
        model_id = str(model_id or "").strip()
        if not model_id:
            self.host.errorRequested.emit(
                "Invalid investigation model",
                "Choose a model before saving the repository-investigation setting.",
            )
            return
        self.storage.set_setting("investigation_model_id", model_id)
        self.host.reload_models()
        self.host.settingsChanged.emit()
        self.host.set_status(f"Investigation model saved: {model_id}")

    def gateway_settings(self) -> dict[str, Any]:
        return {
            "url": self.storage.get_setting("gateway_url", DEFAULT_GATEWAY_URL)
            or DEFAULT_GATEWAY_URL,
            "hasToken": bool(self.host.runtime.gateway_token),
            "hasApiKey": bool(get_api_key()),
        }

    def save_gateway(self, url: str, token: str) -> None:
        if not self.host.runtime.save_gateway(url, token):
            return
        self.catalog_autorefresh_attempted = False
        self.host.settingsChanged.emit()
        self.host.set_status("Gateway settings saved; checking connection…")
        self.host.runtime.request_check()

    def save_api_key(self, value: str) -> None:
        try:
            save_api_key(value)
        except Exception as exc:
            self.host.errorRequested.emit("Could not save API key", str(exc))
            return
        self.host.settingsChanged.emit()
        self.host.set_status("API key stored securely")

    # -- per-model request options -----------------------------------------------------

    def request_options(self, model: str) -> RequestOptions:
        try:
            data = json.loads(self.storage.get_setting(f"model_options:{model}") or "{}")
            return _request_options(data)
        except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
            return RequestOptions()

    def set_reasoning_effort(self, model: str, effort: str) -> None:
        options = self.request_options(model)
        options.reasoning_effort = effort if effort in REASONING_EFFORTS else None
        self._save_model_options(model, options)

    def save_model_options(self, model: str, values: dict[str, Any]) -> None:
        self._save_model_options(model, _request_options(values))

    def _save_model_options(self, model: str, options: RequestOptions) -> None:
        self.storage.set_setting(f"model_options:{model}", json.dumps(asdict(options)))
        self.host.settingsChanged.emit()
        self.host.set_status("Model and privacy controls saved")

    # -- model catalog -----------------------------------------------------------------

    def model_entries(self) -> list[dict[str, Any]]:
        selected = self.ensure_investigation_model()
        favorites = set(self.storage.favorite_model_ids())
        recents = set(self.storage.recent_model_ids())
        values = [
            {
                "id": model.id,
                "label": model.label,
                "description": model.description,
                "contextLength": model.context_length,
                "price": model.average_price_per_million,
                "rank": model.artificial_analysis_rank,
                "year": model.release_year,
                "favorite": model.id in favorites,
                "recent": model.id in recents,
                "reasoning": "reasoning" in model.supported_parameters,
                "textCompatible": model.text_compatible,
                "tools": agent_capable(model.supported_parameters),
                "priceText": price_text(model),
                "warnings": model_warnings(model),
            }
            for model in self.catalog.models()
        ]
        values.sort(
            key=lambda item: (
                not item["favorite"],
                not item["recent"],
                str(item["label"]).casefold(),
            )
        )
        # The investigation models stay selectable even when the catalog does not list them.
        ids = {item["id"] for item in values}
        if DEFAULT_INVESTIGATION_MODEL not in ids:
            values.append(dict(INVESTIGATION_MODEL_ENTRY))
        if selected not in ids | {DEFAULT_INVESTIGATION_MODEL}:
            values.append(_saved_investigation_entry(selected))
        return values

    def toggle_favorite_model(self, model_id: str) -> None:
        self.storage.set_model_favorite(
            model_id, model_id not in self.storage.favorite_model_ids()
        )
        self.host.reload_models()

    def refresh_catalog_if_stale(self) -> None:
        if self.catalog.is_stale() and not self.catalog_autorefresh_attempted:
            self.catalog_autorefresh_attempted = True
            self.start_catalog_refresh(report_errors=False)

    def start_catalog_refresh(self, *, report_errors: bool) -> None:
        if self.catalog_worker:
            return
        connection = self.host.runtime.connection()
        if not connection:
            if report_errors:
                self.host.errorRequested.emit(
                    "Gateway token not configured",
                    "Open Settings → Connection and store the local gateway token first.",
                )
            return
        worker = CatalogWorker(GatewayClient(connection))
        worker.complete.connect(self._catalog_complete)
        worker.failed.connect(
            self._catalog_failed if report_errors else self._background_catalog_failed
        )
        worker.finished.connect(self._catalog_finished)
        self.catalog_worker = worker
        worker.start()

    @Slot(str)
    def _catalog_failed(self, message: str) -> None:
        self.host.errorRequested.emit("Model refresh failed", message)

    @Slot(str)
    def _background_catalog_failed(self, message: str) -> None:
        self.host.set_status(f"Model catalog refresh unavailable: {message}")

    @Slot(object)
    def _catalog_complete(self, result: object) -> None:
        if (
            isinstance(result, dict)
            and isinstance(result.get("models"), list)
            and isinstance(result.get("fetched_at"), str)
        ):
            self.catalog.cache(result["models"], result["fetched_at"])
            self.host.reload_models()

    @Slot()
    def _catalog_finished(self) -> None:
        if self.catalog_worker:
            self.catalog_worker.deleteLater()
        self.catalog_worker = None

    # -- prompt commands and presets ---------------------------------------------------

    def reload_commands(self) -> None:
        builtins = [
            {"name": name, "description": description, "builtin": True}
            for name, description in BUILTIN_COMMANDS
        ]
        self.commands = builtins + [
            {**item, "builtin": False} for item in self.storage.list_prompt_commands()
        ]
        self.host.commandsChanged.emit()

    def save_prompt_command(self, name: str, description: str, template: str) -> None:
        try:
            self.storage.save_prompt_command(name, description, template)
        except ValueError as exc:
            self.host.errorRequested.emit("Invalid command", str(exc))
            return
        self.reload_commands()

    def delete_prompt_command(self, name: str) -> None:
        self.storage.delete_prompt_command(name)
        self.reload_commands()

    def reload_presets(self) -> None:
        self.presets = [asdict(item) for item in self.storage.list_prompt_presets()]
        self.host.presetsChanged.emit()

    def save_prompt_preset(self, preset_id: str, name: str, content: str) -> None:
        try:
            self.storage.save_prompt_preset(name, content, preset_id or None)
        except ValueError as exc:
            self.host.errorRequested.emit("Invalid preset", str(exc))
            return
        self.reload_presets()

    def delete_prompt_preset(self, preset_id: str) -> None:
        self.storage.delete_prompt_preset(preset_id)
        self.reload_presets()

    # -- skills and workspace configuration -------------------------------------------

    def reload_skills(self) -> None:
        workspace = self.host.runtime.workspace_path
        if not workspace:
            self.skills = []
        else:
            active = set(
                self.storage.workspace_config(self.host.runtime.identity()).get("active_skills")
                or ()
            )
            self.skills = [
                {
                    "name": skill.name,
                    "description": skill.description,
                    "scope": skill.scope,
                    "active": skill.name in active,
                }
                for skill in available_skills(app_data_dir() / "skills", Path(workspace))
            ]
        self.host.skillsChanged.emit()

    def save_active_skills(self, csv_names: str) -> None:
        workspace = self.host.runtime.workspace_path
        if not workspace:
            return
        identity = self.host.runtime.identity()
        config = self.storage.workspace_config(identity)
        requested = [item.strip().casefold() for item in csv_names.split(",") if item.strip()]
        unknown = sorted(set(requested) - {item["name"] for item in self.skills})
        if unknown:
            self.host.errorRequested.emit("Unknown skills", ", ".join(unknown))
            return
        config["active_skills"] = requested
        self.storage.save_workspace_config(identity, workspace, config)
        self.sync_workspace_config(identity, config)
        self.reload_skills()

    def save_workspace_configuration(self, instructions: str, declarations: str) -> None:
        workspace = self.host.runtime.workspace_path
        if not workspace:
            return
        try:
            custom_tools = json.loads(declarations or "[]")
            if not isinstance(custom_tools, list) or any(
                not isinstance(item, dict) for item in custom_tools
            ):
                raise ValueError("Custom tools must be a JSON array of objects")
        except (json.JSONDecodeError, ValueError) as exc:
            self.host.errorRequested.emit("Invalid custom capabilities", str(exc))
            return
        identity = self.host.runtime.identity()
        config = self.storage.workspace_config(identity)
        config.update(
            {
                "instructions": instructions[:32_000],
                "custom_tools": custom_tools,
                "context_policy": config.get("context_policy", "automatic"),
            }
        )
        self.storage.save_workspace_config(identity, workspace, config)
        self.sync_workspace_config(identity, config)
        self.host.set_status("Workspace configuration saved")

    def set_web_tools(self, enabled: bool) -> None:
        """Turn the documentation tools (web_search, fetch_url) on or off for this workspace;
        they are off by default and offered only in Agent mode (plan 5.4)."""
        self._update_workspace_config(
            {"web_tools": bool(enabled)},
            "Documentation tools enabled for Agent sessions"
            if enabled
            else "Documentation tools disabled",
        )

    def save_unredacted_paths(self, text: str) -> None:
        """Files whose tool results reach the model without secret redaction: a per-file user
        decision (plan 4.5). One workspace-relative path per line."""
        paths: list[str] = []
        for line in text.splitlines():
            value = line.strip().replace("\\", "/").strip("/")
            if not value:
                continue
            if value.startswith("..") or "/../" in f"/{value}/" or ":" in value:
                self.host.errorRequested.emit(
                    "Invalid path", f"{value} is not a relative workspace path."
                )
                return
            if value not in paths:
                paths.append(value)
        if len(paths) > MAX_UNREDACTED_PATHS:
            self.host.errorRequested.emit(
                "Too many paths", f"List at most {MAX_UNREDACTED_PATHS} files."
            )
            return
        self._update_workspace_config({"unredacted_paths": paths}, "Redaction exceptions saved")

    def _update_workspace_config(self, values: dict[str, object], status: str) -> None:
        workspace = self.host.runtime.workspace_path
        if not workspace:
            return
        identity = self.host.runtime.identity()
        config = self.storage.workspace_config(identity)
        config.update(values)
        self.storage.save_workspace_config(identity, workspace, config)
        self.sync_workspace_config(identity, config)
        self.host.settingsChanged.emit()
        self.host.set_status(status)

    def workspace_configuration(self) -> dict[str, Any]:
        if not self.host.runtime.workspace_path:
            return {"instructions": "", "custom_tools": []}
        return dict(self.storage.workspace_config(self.host.runtime.identity()))

    def sync_workspace_config(self, identity: str, config: dict[str, object]) -> None:
        connection = self.host.runtime.connection()
        if not connection:
            return
        allowed = {key: config[key] for key in sorted(WORKSPACE_CONFIG_KEYS) if key in config}
        try:
            GatewayClient(connection).save_workspace_config(identity, allowed)
        except GatewayError:
            pass

    # -- runner settings and staged-session retention (plan 2.2, 2.4, 2.6, 2.7) ---------

    def runtime_settings(self) -> RuntimeSettings:
        """The saved runner settings; anything unreadable or out of range falls back to defaults."""
        try:
            values = json.loads(self.storage.get_setting(RUNTIME_SETTINGS_KEY) or "{}")
            return _runtime_settings(values).validated()
        except (TypeError, ValueError, RuntimeErrorMessage):
            return RuntimeSettings()

    def runtime_settings_view(self) -> dict[str, Any]:
        settings = self.runtime_settings()
        workspace = self.host.runtime.workspace_path
        return {
            "runnerTarget": settings.runner_target,
            "detectedTarget": detect_runner_target(Path(workspace)) if workspace else "",
            "cpus": settings.cpus or 0,
            "memoryGb": settings.memory_gb or 0,
            "pids": settings.pids,
            "gitHistoryDepth": settings.git_history_depth,
            "retentionDays": self.retention_days(),
        }

    def save_runtime_settings(self, values: dict[str, Any]) -> bool:
        """Validate and save runner settings; they apply when the runtime next starts."""
        try:
            settings = _runtime_settings(values).validated()
            retention = int(values.get("retentionDays") or DEFAULT_RETENTION_DAYS)
            if not RETENTION_DAYS_RANGE[0] <= retention <= RETENTION_DAYS_RANGE[1]:
                raise RuntimeErrorMessage(
                    "Keep staged sessions for 1 to 365 days before offering removal."
                )
        except (TypeError, ValueError, RuntimeErrorMessage) as exc:
            self.host.errorRequested.emit("Invalid runner settings", str(exc))
            return False
        saved = {key: asdict(settings)[key] for key in RUNNER_SETTING_FIELDS}
        self.storage.set_setting(RUNTIME_SETTINGS_KEY, json.dumps(saved))
        self.storage.set_setting(RETENTION_DAYS_KEY, str(retention))
        self.host.settingsChanged.emit()
        self.host.set_status("Runner settings saved; they apply when the runtime restarts")
        return True

    def retention_days(self) -> int:
        value = optional_int(self.storage.get_setting(RETENTION_DAYS_KEY))
        if value is None or not RETENTION_DAYS_RANGE[0] <= value <= RETENTION_DAYS_RANGE[1]:
            return DEFAULT_RETENTION_DAYS
        return value

    # -- saved permission rules --------------------------------------------------------

    def load_permission_rules(self) -> None:
        if not self.host.runtime.workspace_path:
            self.permissions = []
            self.host.permissionsChanged.emit()
            return
        connection = self.host.runtime.connection()
        if not connection:
            return
        try:
            self.permissions = GatewayClient(connection).permission_rules(
                self.host.runtime.identity()
            )
        except GatewayError as exc:
            self.host.errorRequested.emit("Could not load permissions", str(exc))
            return
        self.host.permissionsChanged.emit()

    def add_permission_rule(self, spec: dict[str, Any]) -> None:
        """Save a rule from the rules screen: a tool, optional path prefix, and for Bash a
        command prefix such as ``pytest`` or ``npm test`` (plan 4.2)."""
        if not self.host.runtime.workspace_path:
            self.host.errorRequested.emit("Workspace required", "Select a workspace first.")
            return
        allowed = {"tool", "mode", "decision", "path_prefix", "command"}
        cleaned = {
            key: str(value).strip()
            for key, value in spec.items()
            if key in allowed and str(value or "").strip()
        }
        identity = self.host.runtime.identity()
        self._change_permission(
            "Could not save the rule",
            lambda client: client.create_permission_rule(identity, cleaned),
        )

    def set_permission_enabled(self, rule_id: str, enabled: bool) -> None:
        self._change_permission(
            "Could not update permission",
            lambda client: client.set_permission_rule_enabled(rule_id, enabled),
        )

    def delete_permission(self, rule_id: str) -> None:
        self._change_permission(
            "Could not delete permission", lambda client: client.delete_permission_rule(rule_id)
        )

    def _change_permission(
        self, failure: str, change: Callable[[GatewayClient], object]
    ) -> None:
        connection = self.host.runtime.connection()
        if not connection:
            return
        try:
            change(GatewayClient(connection))
            self.host.loadPermissionRules()
        except GatewayError as exc:
            self.host.errorRequested.emit(failure, str(exc))

    def shutdown(self) -> None:
        if self.catalog_worker:
            self.catalog_worker.wait(500)


def _runtime_settings(values: dict[str, Any]) -> RuntimeSettings:
    """Runner settings from saved JSON (snake_case) or the QML form (camelCase); zero or an
    empty value means "derive from Docker's allocation" for CPUs and memory."""
    if not isinstance(values, dict):
        raise TypeError("Runner settings must be an object.")

    def pick(snake: str, camel: str) -> Any:
        return values.get(snake, values.get(camel))

    cpus = optional_float(pick("cpus", "cpus"))
    memory = pick("memory_gb", "memoryGb")
    pids = pick("pids", "pids")
    depth = pick("git_history_depth", "gitHistoryDepth")
    return RuntimeSettings(
        runner_target=str(pick("runner_target", "runnerTarget") or "auto"),
        cpus=cpus or None,
        memory_gb=_whole(memory) or None,
        pids=DEFAULT_RUNNER_PIDS if pids in (None, "") else _whole(pids),
        git_history_depth=(DEFAULT_GIT_HISTORY_DEPTH if depth in (None, "") else _whole(depth)),
    )


def _whole(value: Any) -> int:
    """A whole number from JSON or QML (which sends numbers as floats); else ``ValueError``."""
    if isinstance(value, bool) or value in (None, ""):
        raise ValueError("Enter whole numbers for runner limits.")
    number = float(value)
    if not number.is_integer():
        raise ValueError("Enter whole numbers for runner limits.")
    return int(number)
