"""Per-saved-target lifecycle generations stored in existing AppSetting records.

Hub edit guards register a snapshot while holding their ordinary edit locks.
Their flush increments only changed request-affecting targets, transactionally.
Database imports remain lazy for standalone Runner imports.
"""
from __future__ import annotations

import copy

WATCH_KEY = "soduwork-model-generation-watches"


def request_config(config) -> dict:
    return {key: value for key, value in (config or {}).items()
            if key not in ("capabilities", "imageRouting")}


def generation_key(*parts) -> str:
    from .model_runtime import fingerprint
    return "model-target-generation-v1:" + fingerprint(parts)[:32]


def target_keys(row, provider, client=None) -> list[str]:
    keys = []
    if not provider.get("personal"):
        keys.append(generation_key("provider", provider["id"]))
    if not row.get("own"):
        keys.append(generation_key("model", row.get("id") or provider["id"] + ":" + row["name"]))
    if client is not None:
        # A personal credential/address does not inherit lifecycle evidence
        # from an unrelated personal model, provider or device.
        from .model_catalog import provider_key
        _, key_from = provider_key(provider, client)
        if key_from != "org" or row.get("own"):
            keys.append(generation_key("personal-provider", client.id, provider["id"]))
        if row.get("own"):
            keys.append(generation_key("personal-model", client.id, provider["id"], row["name"]))
    return keys


def _states(table, identifier, row):
    if row is None:
        return {}
    name = table.__tablename__
    if name == "model_provider":
        return {generation_key("provider", identifier): {
            field: getattr(row, field) for field in ("kind", "base_url", "api_key", "enabled")}}
    if name == "model_def":
        return {generation_key("model", identifier): {
            **{field: getattr(row, field) for field in ("provider_id", "name", "enabled", "ctx_window", "modality")},
            "config": request_config(row.config)}}
    if name == "client":
        providers = {str(value.get("id") or ""): value for value in (row.config or {}).get("modelProviders", [])
                     if isinstance(value, dict) and value.get("localOnly") is not True}
        keys = row.model_keys or {}
        states = {generation_key("personal-provider", identifier, provider_id): {
            "key": keys.get(provider_id), "config": {field: providers.get(provider_id, {}).get(field)
                for field in ("kind", "baseUrl", "authMode", "personal")}}
            for provider_id in providers.keys() | keys.keys()}
        for model in row.models or []:
            if isinstance(model, dict):
                states[generation_key("personal-model", identifier, model.get("providerId"), model.get("name"))] = {
                    "providerId": model.get("providerId"), "name": model.get("name"),
                    "modality": model.get("modality") or "chat",
                    "contextWindow": model.get("ctxWindow") or model.get("contextWindow") or 0,
                    "config": request_config(model.get("config"))}
        return states
    return {}


def _flush(session, _context, _instances):
    from ..models import AppSetting
    for (table, identifier), watch in list(session.info.get(WATCH_KEY, {}).items()):
        new = next((row for row in session.new if isinstance(row, table) and row.id == identifier), None)
        row = new if new is not None else session.get(table, identifier)
        if row in session.deleted:
            row = None
        current = _states(table, identifier, row)
        for key in current.keys() | watch.keys():
            if current.get(key) == watch.get(key):
                continue
            value = session.get(AppSetting, key)
            if value is None:
                session.add(AppSetting(key=key, value="1"))
            else:
                try:
                    previous = int(value.value)
                except (ValueError, TypeError):
                    previous = 0
                value.value = str(previous + 1)
        session.info[WATCH_KEY][(table, identifier)] = copy.deepcopy(current)


def _clear(session, *_args):
    session.info.pop(WATCH_KEY, None)


def track(session, table, identifier, row):
    from sqlalchemy import event
    target = session.sync_session
    if not event.contains(target, "before_flush", _flush):
        event.listen(target, "before_flush", _flush)
        event.listen(target, "after_commit", _clear)
        event.listen(target, "after_rollback", _clear)
    watches = session.info.setdefault(WATCH_KEY, {})
    watches.setdefault((table, identifier), copy.deepcopy(_states(table, identifier, row)))
