"""Authenticated SSH-key bindings for the restricted delivery receiver.

A binding is the only place the centre learns who a caller is.  The batch
payload never carries machine, account, provider or kind, so editing the JSON
cannot change attribution.  Bindings contain identifiers and permissions
only; they must never contain credentials.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

KIND_AGY_USAGE = "agy_usage"
KIND_QUOTA_SAMPLE = "quota_sample"
SUPPORTED_KINDS = (KIND_AGY_USAGE, KIND_QUOTA_SAMPLE)
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_TEXT_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class BindingError(ValueError):
    """The binding file is missing, malformed or ambiguous."""


@dataclass(frozen=True)
class Binding:
    binding_id: str
    machine: str
    account: str
    kind: str
    provider: str | None = None

    def public_identity(self) -> dict[str, Any]:
        identity: dict[str, Any] = {
            "binding_id": self.binding_id,
            "machine": self.machine,
            "account": self.account,
            "kind": self.kind,
        }
        if self.provider is not None:
            identity["provider"] = self.provider
        return identity


def _text(value: Any, field: str, *, limit: int = 200) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BindingError(f"{field} must be a non-empty string")
    text = value.strip()
    if len(text) > limit or _TEXT_CONTROL_RE.search(text):
        raise BindingError(f"{field} is too long or contains control characters")
    return text


def parse_binding(binding_id: str, raw: Any) -> Binding:
    if not _ID_RE.match(binding_id or ""):
        raise BindingError(f"invalid binding id: {binding_id!r}")
    if not isinstance(raw, Mapping):
        raise BindingError(f"binding {binding_id!r} must be an object")
    machine = _text(raw.get("machine"), f"{binding_id}.machine")
    account = _text(raw.get("account"), f"{binding_id}.account")
    kind = _text(raw.get("kind"), f"{binding_id}.kind", limit=32)
    if kind not in SUPPORTED_KINDS:
        raise BindingError(f"{binding_id}.kind is not supported: {kind!r}")
    provider_raw = raw.get("provider")
    provider: str | None = None
    if kind == KIND_QUOTA_SAMPLE:
        provider = _text(provider_raw, f"{binding_id}.provider", limit=32).lower()
        if provider not in ("agy", "codex"):
            raise BindingError(f"{binding_id}.provider must be agy or codex")
    elif provider_raw not in (None, ""):
        raise BindingError(f"{binding_id} is an agy_usage binding and must not set provider")
    return Binding(
        binding_id=binding_id,
        machine=machine,
        account=account,
        kind=kind,
        provider=provider,
    )


def load_bindings(path: str | Path) -> dict[str, Binding]:
    """Load ``{id: {...}}`` or ``{"bindings": [...]}``; reject duplicates."""

    source = Path(path).expanduser()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BindingError(f"binding file not found: {source}") from None
    except (OSError, ValueError) as exc:
        raise BindingError(f"cannot read binding file: {exc}") from None

    entries: dict[str, Any] = {}
    if isinstance(payload, Mapping) and "bindings" in payload:
        raw_entries = payload["bindings"]
        if isinstance(raw_entries, Mapping):
            entries = dict(raw_entries)
        elif isinstance(raw_entries, list):
            for index, raw in enumerate(raw_entries):
                if not isinstance(raw, Mapping):
                    raise BindingError(f"bindings[{index}] must be an object")
                binding_id = raw.get("id")
                if not isinstance(binding_id, str) or not binding_id:
                    raise BindingError(f"bindings[{index}].id is required")
                if binding_id in entries:
                    raise BindingError(f"duplicate binding id: {binding_id!r}")
                entries[binding_id] = raw
        else:
            raise BindingError("bindings must be an object or a list")
    elif isinstance(payload, Mapping):
        entries = dict(payload)
    else:
        raise BindingError("binding file must contain an object")

    if not entries:
        raise BindingError("binding file contains no bindings")
    return {
        binding_id: parse_binding(binding_id, raw)
        for binding_id, raw in entries.items()
    }


def require_binding(bindings: Mapping[str, Binding], binding_id: str) -> Binding:
    try:
        return bindings[binding_id]
    except KeyError:
        raise BindingError(f"unknown binding id: {binding_id!r}") from None


__all__ = [
    "KIND_AGY_USAGE",
    "KIND_QUOTA_SAMPLE",
    "Binding",
    "BindingError",
    "load_bindings",
    "parse_binding",
    "require_binding",
]
