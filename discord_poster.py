"""
Posts NT8 status, fills, and alerts to Discord.

Three posting modes:
  - Pinned status message in #nt-status: edited in place via the Bot API
    so the channel stays clean (no spam of new messages every 30s).
  - Fills posted to #nt-fills: webhook, append-only, one embed per fill.
  - Alerts posted to #nt-alerts: webhook, append-only, with @mention.

This module is deliberately stateless about NT8 — it accepts the snapshot
dataclasses from nt_reader and the AccountsRegistry for display formatting.
The main service loop (service.py) decides when to call which method.

Discord rate limits handled:
  - Per-channel: 5 messages / 5 seconds (we're at ~1 message / 30s, fine)
  - Per-bot: 50 requests / second global (not a concern at our scale)
  - 429 responses: respected via Retry-After header
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import requests

from accounts_config import AccountsRegistry, AccountConfig
from nt_reader import (
    AccountSnapshot,
    Execution,
    HeartbeatSnapshot,
    StrategiesSnapshot,
)

log = logging.getLogger(__name__)


# Discord embed colors. Discord wants integers; these are hex converted.
COLOR_GREEN  = 0x2ECC71
COLOR_YELLOW = 0xF1C40F
COLOR_RED    = 0xE74C3C
COLOR_BLUE   = 0x3498DB
COLOR_GRAY   = 0x95A5A6


# Standard request timeout. Discord usually responds in <1s; longer than
# 10s means something's wrong and we should fail fast and retry next poll.
HTTP_TIMEOUT = 10


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


@dataclass
class DiscordConfig:
    bot_token: str
    status_channel_id: int
    alert_mention_user_id: Optional[int]
    webhook_fills: str
    webhook_alerts: str
    status_edit_interval_sec: int
    alert_dedupe_window_sec: int
    post_recovery: bool

    @classmethod
    def load(cls, path: str) -> "DiscordConfig":
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        bot = data.get("bot", {}) or {}
        wh = data.get("webhooks", {}) or {}
        bh = data.get("behavior", {}) or {}

        cfg = cls(
            bot_token=str(bot.get("token", "")).strip(),
            status_channel_id=int(bot.get("status_channel_id") or 0),
            alert_mention_user_id=(int(bot["alert_mention_user_id"])
                                   if bot.get("alert_mention_user_id") else None),
            webhook_fills=str(wh.get("fills", "")).strip(),
            webhook_alerts=str(wh.get("alerts", "")).strip(),
            status_edit_interval_sec=int(bh.get("status_edit_interval_sec", 30)),
            alert_dedupe_window_sec=int(bh.get("alert_dedupe_window_sec", 300)),
            post_recovery=bool(bh.get("post_recovery", True)),
        )

        # Validate enough config exists to do anything useful — fail loud
        if not cfg.bot_token or "PASTE" in cfg.bot_token:
            raise ValueError("discord_config.yaml: bot.token is missing or unset")
        if not cfg.status_channel_id:
            raise ValueError("discord_config.yaml: bot.status_channel_id is missing")
        if "PASTE" in cfg.webhook_fills or "PASTE" in cfg.webhook_alerts:
            raise ValueError("discord_config.yaml: webhook URLs are not set")

        return cfg


# ---------------------------------------------------------------------------
# Poster
# ---------------------------------------------------------------------------


class DiscordPoster:
    """Wraps all Discord interactions into a single class."""

    def __init__(self, config: DiscordConfig, accounts: AccountsRegistry):
        self.cfg = config
        self.accounts = accounts

        # Persistent HTTP session — avoids reconnecting to Discord every poll
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "NTMonitor/1.0 (private trading-status bot)"
        })

        # State for the editable pinned status message
        self._status_message_id: Optional[int] = None

        # Dedupe state for alerts: alert_key -> last-posted timestamp
        self._alert_history: dict[str, float] = {}
        # Track currently-active alerts so we know when to post recovery
        self._active_alerts: set[str] = set()

    # -----------------------------------------------------------------------
    # PUBLIC API — called by the main service loop
    # -----------------------------------------------------------------------

    def update_status_message(
        self,
        hb: HeartbeatSnapshot,
        st: StrategiesSnapshot,
    ) -> None:
        """
        Edit (or create) the pinned status embed in #nt-status.
        Safe to call every poll — uses Bot API edit, not webhook posts.
        """
        embed = self._build_status_embed(hb, st)
        payload = {"embeds": [embed], "content": ""}

        if self._status_message_id is None:
            # First call: locate or create the pinned message
            self._status_message_id = self._find_or_create_status_message(payload)
        else:
            ok = self._edit_message(self._status_message_id, payload)
            if not ok:
                # Message was probably deleted by a user — recreate
                log.info("Status message edit failed, recreating")
                self._status_message_id = self._find_or_create_status_message(payload)

    def post_fill(self, exc: Execution) -> None:
        """Post a single fill embed to #nt-fills via webhook."""
        cfg = self.accounts.resolve(exc.account)
        acct_label = cfg.display_name if cfg else exc.account

        # Color: blue for entry, gray for exit. We can't perfectly tell these
        # apart from execution data alone, so we guess based on whether the
        # strategy entry signal name contains common entry/exit keywords.
        color = self._guess_fill_color(exc)
        side_emoji = "🟢" if exc.action.lower() == "long" else "🔴"

        embed = {
            "title": f"{side_emoji} {exc.action.upper()} {exc.qty} {exc.instrument}  @  {exc.price:,.2f}",
            "color": color,
            "fields": [
                {"name": "Account",  "value": acct_label, "inline": True},
                {"name": "Strategy", "value": exc.strategy or "manual", "inline": True},
                {"name": "Time",     "value": _format_local_time(exc.exec_time), "inline": True},
            ],
            "footer": {"text": f"order {exc.order_id[:8]}…  exec {exc.exec_id[:8]}…"},
            "timestamp": exc.exec_time.isoformat(),
        }
        if exc.commission:
            embed["fields"].append({
                "name": "Commission", "value": f"${exc.commission:.2f}", "inline": True,
            })

        self._post_webhook(self.cfg.webhook_fills, {"embeds": [embed]})

    def post_alert(self, key: str, title: str, description: str,
                   severity: str = "error") -> None:
        """
        Post an alert to #nt-alerts. Deduplicates so a flapping condition
        doesn't spam the channel.

        Args:
          key: stable identifier for this alert type (e.g. "nt_unreachable",
               "strategy_disabled:SphinxFibrillator"). Used for dedup and
               recovery tracking.
          title: short headline shown as the embed title.
          description: longer message body.
          severity: "error" (red), "warning" (yellow), or "info" (blue).
        """
        now = time.time()
        last = self._alert_history.get(key, 0)
        if now - last < self.cfg.alert_dedupe_window_sec:
            log.debug("Alert '%s' suppressed by dedupe window", key)
            self._active_alerts.add(key)
            return

        color = {"error": COLOR_RED, "warning": COLOR_YELLOW, "info": COLOR_BLUE}.get(
            severity, COLOR_RED)

        content = ""
        if severity == "error" and self.cfg.alert_mention_user_id:
            content = f"<@{self.cfg.alert_mention_user_id}>"

        embed = {
            "title": title,
            "description": description,
            "color": color,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        ok = self._post_webhook(self.cfg.webhook_alerts,
                                {"content": content, "embeds": [embed]})
        if ok:
            self._alert_history[key] = now
            self._active_alerts.add(key)

    def post_recovery(self, key: str, title: str, description: str = "") -> None:
        """
        If a previously-posted alert is now resolved, optionally post a
        recovery message. No-op if the alert wasn't active.
        """
        if key not in self._active_alerts:
            return
        self._active_alerts.discard(key)
        if not self.cfg.post_recovery:
            return

        embed = {
            "title": f"✅ Recovered: {title}",
            "description": description or "Condition no longer detected.",
            "color": COLOR_GREEN,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._post_webhook(self.cfg.webhook_alerts, {"embeds": [embed]})

    # -----------------------------------------------------------------------
    # EMBED BUILDING — pure formatting
    # -----------------------------------------------------------------------

    def _build_status_embed(
        self,
        hb: HeartbeatSnapshot,
        st: StrategiesSnapshot,
    ) -> dict:
        """The big always-current dashboard embed."""

        # Overall status determination — worst condition wins
        if not hb.is_reachable:
            color = COLOR_RED
            title = "🔴 NT8 UNREACHABLE"
            subtitle = hb.error or "Cannot read heartbeat file"
        elif hb.is_stale:
            color = COLOR_RED
            title = "🔴 NT8 NOT UPDATING"
            subtitle = f"Heartbeat is {hb.age_seconds:.0f}s old (process may be frozen)"
        elif any(not s.enabled for s in st.strategies if self.accounts.resolve(s.account)):
            color = COLOR_YELLOW
            title = "🟡 NT8 RUNNING — STRATEGY DISABLED"
            subtitle = "One or more strategies are not enabled"
        else:
            color = COLOR_GREEN
            title = "🟢 NT8 RUNNING"
            subtitle = "All strategies enabled"

        fields = []

        # --- Accounts table ---
        acct_lines = []
        for a in hb.accounts:
            cfg = self.accounts.resolve(a.name)
            if cfg is None:
                continue
            cash = a.cash_value if a.cash_value is not None else 0
            conn = "🟢" if a.connected else "⚪"
            pos_str = f" · {len(a.positions)} pos" if a.positions else ""
            acct_lines.append(
                f"{conn} {cfg.type_emoji} **{cfg.alias}** — ${cash:,.2f}{pos_str}"
            )
        if acct_lines:
            fields.append({
                "name": f"Accounts ({len(acct_lines)})",
                "value": "\n".join(acct_lines)[:1024],  # Discord field cap
                "inline": False,
            })

        # --- Strategies table ---
        strat_lines = []
        for s in st.strategies:
            cfg = self.accounts.resolve(s.account)
            if cfg is None:
                continue
            flag = "🟢" if s.enabled else "🔴"
            strat_lines.append(
                f"{flag} **{s.name}** — {s.instrument} on {cfg.alias} ({s.state})"
            )
        if strat_lines:
            fields.append({
                "name": f"Strategies ({len(strat_lines)})",
                "value": "\n".join(strat_lines)[:1024],
                "inline": False,
            })

        # --- Open positions across all accounts ---
        pos_lines = []
        for a in hb.accounts:
            cfg = self.accounts.resolve(a.name)
            if cfg is None:
                continue
            for p in a.positions:
                arrow = "📈" if p.side.lower() == "long" else "📉"
                pos_lines.append(
                    f"{arrow} **{cfg.alias}**: {p.side} {p.qty} {p.instrument} @ {p.avg_price:,.2f}"
                )
        if pos_lines:
            fields.append({
                "name": f"Open positions ({len(pos_lines)})",
                "value": "\n".join(pos_lines)[:1024],
                "inline": False,
            })

        embed = {
            "title": title,
            "description": subtitle,
            "color": color,
            "fields": fields,
            "footer": {
                "text": f"NT timestamp: {_format_local_time(hb.timestamp_utc)}  ·  "
                        f"updated {_format_local_time(datetime.now(timezone.utc))}"
                        if hb.timestamp_utc else
                        f"updated {_format_local_time(datetime.now(timezone.utc))}"
            },
        }
        return embed

    def _guess_fill_color(self, exc: Execution) -> int:
        """
        Heuristic: entry-signal names with 'exit', 'stop', 'target' in them
        are exits and shown in gray. Everything else is treated as an entry.
        """
        sig = (exc.strategy or "").lower()
        if any(k in sig for k in ("exit", "stop", "target", "tp", "sl")):
            return COLOR_GRAY
        return COLOR_BLUE

    # -----------------------------------------------------------------------
    # HTTP — Bot API and webhooks
    # -----------------------------------------------------------------------

    def _bot_headers(self) -> dict:
        return {
            "Authorization": f"Bot {self.cfg.bot_token}",
            "Content-Type": "application/json",
        }

    def _find_or_create_status_message(self, payload: dict) -> Optional[int]:
        """
        On first run, look for an existing pinned message authored by us.
        If found, edit it. If not, post a new one and pin it.
        """
        # Get the bot's own user ID so we can identify our own messages
        try:
            r = self.session.get(
                "https://discord.com/api/v10/users/@me",
                headers=self._bot_headers(), timeout=HTTP_TIMEOUT,
            )
            r.raise_for_status()
            bot_user_id = int(r.json()["id"])
        except (requests.RequestException, ValueError, KeyError) as exc:
            log.error("Could not identify bot user: %s", exc)
            return None

        # Look at the channel's pinned messages — adopt one if it's ours
        try:
            r = self.session.get(
                f"https://discord.com/api/v10/channels/{self.cfg.status_channel_id}/pins",
                headers=self._bot_headers(), timeout=HTTP_TIMEOUT,
            )
            r.raise_for_status()
            pins = r.json()
            for msg in pins:
                if int(msg["author"]["id"]) == bot_user_id:
                    msg_id = int(msg["id"])
                    log.info("Adopting existing pinned status message %s", msg_id)
                    self._edit_message(msg_id, payload)
                    return msg_id
        except requests.RequestException as exc:
            log.warning("Could not list pins: %s", exc)

        # No existing pin found — create a new message and pin it
        try:
            r = self._discord_request(
                "POST",
                f"https://discord.com/api/v10/channels/{self.cfg.status_channel_id}/messages",
                json=payload,
            )
            if r is None or r.status_code >= 300:
                log.error("Failed to post initial status message: %s",
                          r.text if r is not None else "no response")
                return None
            msg_id = int(r.json()["id"])
        except (requests.RequestException, ValueError, KeyError) as exc:
            log.error("Failed to post initial status message: %s", exc)
            return None

        # Pin it (best-effort — channel can have at most 50 pins)
        try:
            pin_r = self._discord_request(
                "PUT",
                f"https://discord.com/api/v10/channels/{self.cfg.status_channel_id}/pins/{msg_id}",
            )
            if pin_r is not None and pin_r.status_code >= 300:
                log.warning("Could not pin status message: %s", pin_r.text)
        except requests.RequestException as exc:
            log.warning("Pin request failed: %s", exc)

        return msg_id

    def _edit_message(self, message_id: int, payload: dict) -> bool:
        try:
            r = self._discord_request(
                "PATCH",
                f"https://discord.com/api/v10/channels/"
                f"{self.cfg.status_channel_id}/messages/{message_id}",
                json=payload,
            )
            if r is None:
                return False
            if r.status_code == 404:
                # Message was deleted
                return False
            if r.status_code >= 300:
                log.warning("Edit failed (%s): %s", r.status_code, r.text[:200])
                return False
            return True
        except requests.RequestException as exc:
            log.warning("Edit request failed: %s", exc)
            return False

    def _post_webhook(self, url: str, payload: dict) -> bool:
        try:
            r = self._discord_request("POST", url, json=payload, is_webhook=True)
            if r is None:
                return False
            if r.status_code >= 300:
                log.warning("Webhook post failed (%s): %s",
                            r.status_code, r.text[:200])
                return False
            return True
        except requests.RequestException as exc:
            log.warning("Webhook request failed: %s", exc)
            return False

    def _discord_request(self, method: str, url: str, *,
                         json: Optional[dict] = None,
                         is_webhook: bool = False) -> Optional[requests.Response]:
        """
        Wrapper that handles 429 rate-limit responses by sleeping for the
        Retry-After window and retrying once. Webhook requests don't carry
        the bot Authorization header.
        """
        headers = {} if is_webhook else self._bot_headers()
        if json is not None:
            headers.setdefault("Content-Type", "application/json")

        for attempt in range(2):
            r = self.session.request(method, url, json=json,
                                     headers=headers, timeout=HTTP_TIMEOUT)
            if r.status_code == 429:
                # Rate limited — read the wait period from the header and back off
                retry = float(r.headers.get("Retry-After", "1.0"))
                log.warning("Discord rate limited; sleeping %.2fs", retry)
                time.sleep(min(retry, 5.0))
                continue
            return r
        return r  # last response after retry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_local_time(dt: Optional[datetime]) -> str:
    """Format a UTC datetime in the local timezone for human display."""
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone()
    return local.strftime("%H:%M:%S %Z")
