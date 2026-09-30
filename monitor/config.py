"""Config loading and threshold resolution."""

from __future__ import annotations

import copy
import datetime as dt
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "clients.yaml"
DATA_DIR = ROOT / "data"
PUBLIC_DIR = ROOT / "public"


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class ClientConfig:
    def __init__(self, slug: str, raw: dict, defaults: dict):
        self.slug = slug
        self.raw = raw
        self.defaults = defaults
        self.name = raw.get("name", slug)
        self.domain = raw["domain"]
        self.gsc_property = raw["gsc_property"]
        self.ga4_property_id = str(raw.get("ga4_property_id") or "") or None
        self.ecommerce = bool(raw.get("ecommerce", False))
        self.seasonality = bool(raw.get("seasonality", False))
        self.priority_urls = list(raw.get("priority_urls") or [])
        self.template_samples = dict(raw.get("template_samples") or {})
        self.suppression_windows = list(raw.get("suppression_windows") or [])
        self.thresholds = _deep_merge(
            defaults.get("thresholds", {}), raw.get("threshold_overrides", {})
        )

    def setting(self, key: str, fallback=None):
        if key in self.raw:
            return self.raw[key]
        return self.defaults.get(key, fallback)

    def threshold(self, signal: str, key: str, fallback=None):
        return self.thresholds.get(signal, {}).get(key, fallback)

    def is_suppressed(self, on_date: dt.date) -> str | None:
        """Return the suppression reason if this date falls in a known window."""
        for window in self.suppression_windows:
            start = dt.date.fromisoformat(str(window["start"]))
            end = dt.date.fromisoformat(str(window["end"]))
            if start <= on_date <= end:
                return window.get("reason", "suppression window")
        return None


def load_clients(only: str | None = None) -> list[ClientConfig]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)

    defaults = raw.get("defaults", {})
    clients = []
    for slug, block in (raw.get("clients") or {}).items():
        if only and slug != only:
            continue
        clients.append(ClientConfig(slug, block, defaults))

    if only and not clients:
        raise SystemExit(f"No client named '{only}' in clients.yaml")
    return clients


def service_account_info() -> dict | None:
    """Service account credentials, supplied as a secret rather than a file."""
    import json

    blob = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if blob:
        return json.loads(blob)

    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if path and Path(path).exists():
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    return None
