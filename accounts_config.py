"""
Loads and applies account aliases / types from accounts.yaml.

The config maps NT8 account IDs (like "TDFYSL50351370795") to friendly
display names ("Tradify 50K #1") and types ("funded", "challenge", etc).

Used by both the Discord poster and the dashboard JSON writer so that
account names are consistent everywhere.

The file is hot-reloaded — modify accounts.yaml and the next poll
picks up the changes without restarting the service.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger(__name__)


# Type → emoji + label used in Discord embeds and the web dashboard.
# Anything not listed falls through to a generic gray badge.
TYPE_DISPLAY = {
    "live":      ("🟢", "Live"),
    "funded":    ("💰", "Funded"),
    "challenge": ("🎯", "Challenge"),
    "eval":      ("📋", "Eval"),
    "sim":       ("🧪", "Sim"),
    "demo":      ("🧪", "Demo"),
}


@dataclass
class AccountConfig:
    id: str
    alias: str
    type: str = ""
    notes: str = ""
    hide: bool = False

    @property
    def display_name(self) -> str:
        """e.g. 'Tradify 50K #1 (Funded)' for embeds and tables."""
        if not self.type:
            return self.alias
        _, label = TYPE_DISPLAY.get(self.type.lower(), ("", self.type.title()))
        return f"{self.alias} ({label})"

    @property
    def type_emoji(self) -> str:
        emoji, _ = TYPE_DISPLAY.get(self.type.lower(), ("⚪", ""))
        return emoji


@dataclass
class AccountsRegistry:
    """Holds the parsed accounts.yaml plus mtime for hot-reload."""

    path: Path
    show_unlisted: bool = False
    by_id: dict[str, AccountConfig] = field(default_factory=dict)
    _mtime: float = 0.0

    @classmethod
    def load(cls, path: str | os.PathLike) -> "AccountsRegistry":
        reg = cls(path=Path(path))
        reg.reload()
        return reg

    def reload_if_changed(self) -> bool:
        """Re-read the YAML if the file has been modified. Returns True if reloaded."""
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            log.warning("accounts.yaml not found at %s; using empty config", self.path)
            self.by_id = {}
            self.show_unlisted = True  # safer default: don't silently hide
            return False

        if mtime == self._mtime:
            return False

        self.reload()
        return True

    def reload(self) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            log.warning("accounts.yaml not found at %s", self.path)
            self.by_id = {}
            self.show_unlisted = True
            return
        except yaml.YAMLError as exc:
            # Don't crash the service on a typo in the config — keep the old
            # registry alive and log the problem so the user can fix it.
            log.error("accounts.yaml parse error, keeping previous config: %s", exc)
            return

        defaults = data.get("defaults", {}) or {}
        self.show_unlisted = bool(defaults.get("show_unlisted", False))

        new_by_id: dict[str, AccountConfig] = {}
        for entry in data.get("accounts", []) or []:
            if not entry.get("id"):
                continue
            cfg = AccountConfig(
                id=entry["id"],
                alias=entry.get("alias", entry["id"]),
                type=entry.get("type", "") or "",
                notes=entry.get("notes", "") or "",
                hide=bool(entry.get("hide", False)),
            )
            new_by_id[cfg.id] = cfg

        self.by_id = new_by_id
        self._mtime = self.path.stat().st_mtime
        log.info(
            "Loaded %d account aliases (show_unlisted=%s)",
            len(self.by_id),
            self.show_unlisted,
        )

    # --- Lookup helpers used by the Discord poster + dashboard writer ---

    def resolve(self, account_id: str) -> Optional[AccountConfig]:
        """
        Returns the AccountConfig for an NT8 account ID, or None if the
        account should be hidden (either explicitly or because it's unlisted
        and show_unlisted=False).
        """
        cfg = self.by_id.get(account_id)
        if cfg is not None:
            return None if cfg.hide else cfg

        if self.show_unlisted:
            # Synthesize a default config so unlisted accounts still appear
            return AccountConfig(id=account_id, alias=account_id, type="")

        return None
