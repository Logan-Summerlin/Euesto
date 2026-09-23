from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Slot

from shared.coercion import optional_float, optional_int
from shared.requests import DEFAULT_INVESTIGATION_MODEL

from ..controllers import GenerationController
from ..extensions import available_skills
from ..gateway_client import DEFAULT_GATEWAY_URL, GatewayClient, GatewayError
from ..model_catalog import ModelCatalog
from ..models import RequestOptions
from ..settings import app_data_dir, get_api_key, save_api_key
from ..storage import Storage
from ..workers import CatalogWorker
from .host import BridgeHost

SERVER_TOOLS = ("web_search", "web_fetch", "datetime")
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
SYNCED_WORKSPACE_KEYS = ("instructions", "active_skills", "default_mode", "context_policy", "custom_tools")
LEGACY_INVESTIGATION_DEFAULT = "deepseek/deepseek-chat-v3-0324"
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


class SettingsService(QObject):
    """Preferences, the model catalog, prompt commands and presets, skills, workspace
    configuration, and saved permission rules."""

    def __init__(self, host: BridgeHost, storage: Storage, *, catalog: ModelCatalog | None = None):
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
        return {key: self.storage.get_setting(f"server_tool:{key}", "0") == "1" for key in SERVER_TOOLS}

    def set_server_tool(self, name: str, enabled: bool) -> None:
        if name in SERVER_TOOLS:
            self.storage.set_setting(f"server_tool:{name}", "1" if enabled else "0")
            self.host.settingsChanged.emit()

    @property
    def investigation_model(self) -> str:
        return self.storage.get_setting("investigation_model_id", "") or ""

    def ensure_investigation_model(self) -> str:
        """Return the saved investigation model, defaulting (and migrating the legacy default) to MiMo."""
        value = self.investigation_model.strip()
        if value in {"", LEGACY_INVESTIGATION_DEFAULT}:
            value = DEFAULT_INVESTIGATION_MODEL
            self.storage.set_setting("investigation_model_id", value)
        return value

    def save_investigation_model(self, model_id: str) -> None:
        model_id = str(model_id or "").strip()
        if not model_id:
            self.host.errorRequested.emit("Invalid investigation model", "Choose a model before saving the repository-investigation setting.")
            return
        self.storage.set_setting("investigation_model_id", model_id)
        self.host.reload_models()
        self.host.settingsChanged.emit()
        self.host.set_status(f"Investigation model saved: {model_id}")

    def gateway_settings(self) -> dict[str, Any]:
        return {
            "url": self.storage.get_setting("gateway_url", DEFAULT_GATEWAY_URL) or DEFAULT_GATEWAY_URL,
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
        return GenerationController.request_options(self.storage, model)

    def set_reasoning_effort(self, model: str, effort: str) -> None:
        options = self.request_options(model)
        options.reasoning_effort = None if effort in {"", "default"} else effort
        self._save_model_options(model, options)

    def save_model_options(self, model: str, values: dict[str, Any]) -> None:
        options = self.request_options(model)
        options.max_tokens = optional_int(values.get("max_tokens"))
        options.temperature = optional_float(values.get("temperature"))
        options.top_p = optional_float(values.get("top_p"))
        stop = values.get("stop")
        options.stop = [str(item) for item in stop] if isinstance(stop, list) else []
        effort = str(values.get("reasoning_effort") or "")
        options.reasoning_effort = effort if effort in {"minimal", "low", "medium", "high"} else None
        options.data_collection = "allow" if values.get("data_collection") == "allow" else "deny"
        options.zero_data_retention = bool(values.get("zero_data_retention", False))
        self._save_model_options(model, options)

    def _save_model_options(self, model: str, options: RequestOptions) -> None:
        self.storage.set_setting(f"model_options:{model}", json.dumps(asdict(options)))
        self.host.settingsChanged.emit()
        self.host.set_status("Model and privacy controls saved")

    # -- model catalog -----------------------------------------------------------------

    def model_entries(self) -> list[dict[str, Any]]:
        selected = self.ensure_investigation_model()
        favorites = set(self.storage.favorite_model_ids())
        recents = set(self.storage.recent_model_ids())
        alias_by_model = {model: alias for alias, model in self.storage.model_aliases().items()}
        values = [
            {
                "id": model.id,
                "label": alias_by_model.get(model.id, model.label),
                "description": model.description,
                "contextLength": model.context_length,
                "price": model.average_price_per_million,
                "rank": model.artificial_analysis_rank,
                "year": model.release_year,
                "favorite": model.id in favorites,
                "recent": model.id in recents,
                "reasoning": "reasoning" in model.supported_parameters,
                "textCompatible": model.text_compatible,
            }
            for model in self.catalog.models()
        ]
        values.sort(key=lambda item: (not item["favorite"], not item["recent"], str(item["label"]).casefold()))
        # The investigation models stay selectable even when the catalog does not list them.
        ids = {item["id"] for item in values}
        if DEFAULT_INVESTIGATION_MODEL not in ids:
            values.append(dict(INVESTIGATION_MODEL_ENTRY))
        if selected not in ids | {DEFAULT_INVESTIGATION_MODEL}:
            values.append(_saved_investigation_entry(selected))
        return values

    def toggle_favorite_model(self, model_id: str) -> None:
        self.storage.set_model_favorite(model_id, model_id not in self.storage.favorite_model_ids())
        self.host.reload_models()

    def save_model_alias(self, alias: str, model_id: str) -> None:
        try:
            self.storage.save_model_alias(alias, model_id)
        except ValueError as exc:
            self.host.errorRequested.emit("Invalid alias", str(exc))
            return
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
        worker.failed.connect(self._catalog_failed if report_errors else self._background_catalog_failed)
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
        if isinstance(result, dict) and isinstance(result.get("models"), list) and isinstance(result.get("fetched_at"), str):
            self.catalog.cache(result["models"], result["fetched_at"])
            self.host.reload_models()

    @Slot()
    def _catalog_finished(self) -> None:
        if self.catalog_worker:
            self.catalog_worker.deleteLater()
        self.catalog_worker = None

    # -- prompt commands and presets ---------------------------------------------------

    def reload_commands(self) -> None:
        builtins = [{"name": name, "description": description, "builtin": True} for name, description in BUILTIN_COMMANDS]
        self.commands = builtins + [{**item, "builtin": False} for item in self.storage.list_prompt_commands()]
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
            active = set(self.storage.workspace_config(self.host.runtime.identity()).get("active_skills") or ())
            self.skills = [
                {"name": skill.name, "description": skill.description, "scope": skill.scope, "active": skill.name in active}
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
        self._sync_workspace_config(identity, config)
        self.reload_skills()

    def save_workspace_configuration(self, instructions: str, declarations: str) -> None:
        workspace = self.host.runtime.workspace_path
        if not workspace:
            return
        try:
            custom_tools = json.loads(declarations or "[]")
            if not isinstance(custom_tools, list) or any(not isinstance(item, dict) for item in custom_tools):
                raise ValueError("Custom tools must be a JSON array of objects")
        except (json.JSONDecodeError, ValueError) as exc:
            self.host.errorRequested.emit("Invalid custom capabilities", str(exc))
            return
        identity = self.host.runtime.identity()
        config = self.storage.workspace_config(identity)
        config.update({"instructions": instructions[:32_000], "custom_tools": custom_tools, "context_policy": config.get("context_policy", "automatic")})
        self.storage.save_workspace_config(identity, workspace, config)
        self._sync_workspace_config(identity, config)
        self.host.set_status("Workspace configuration saved")

    def workspace_configuration(self) -> dict[str, Any]:
        if not self.host.runtime.workspace_path:
            return {"instructions": "", "custom_tools": []}
        return dict(self.storage.workspace_config(self.host.runtime.identity()))

    def _sync_workspace_config(self, identity: str, config: dict[str, object]) -> None:
        connection = self.host.runtime.connection()
        if not connection:
            return
        allowed = {key: config[key] for key in SYNCED_WORKSPACE_KEYS if key in config}
        try:
            GatewayClient(connection).save_workspace_config(identity, allowed)
        except GatewayError:
            pass

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
            self.permissions = GatewayClient(connection).permission_rules(self.host.runtime.identity())
        except GatewayError as exc:
            self.host.errorRequested.emit("Could not load permissions", str(exc))
            return
        self.host.permissionsChanged.emit()

    def set_permission_enabled(self, rule_id: str, enabled: bool) -> None:
        connection = self.host.runtime.connection()
        if not connection:
            return
        try:
            GatewayClient(connection).set_permission_rule_enabled(rule_id, enabled)
            self.host.loadPermissionRules()
        except GatewayError as exc:
            self.host.errorRequested.emit("Could not update permission", str(exc))

    def delete_permission(self, rule_id: str) -> None:
        connection = self.host.runtime.connection()
        if not connection:
            return
        try:
            GatewayClient(connection).delete_permission_rule(rule_id)
            self.host.loadPermissionRules()
        except GatewayError as exc:
            self.host.errorRequested.emit("Could not delete permission", str(exc))

    def shutdown(self) -> None:
        if self.catalog_worker:
            self.catalog_worker.wait(500)
