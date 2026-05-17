"""
Main service loop for the NT8 monitor.

Runs forever, polling the NT_Logger share at a configurable interval.
Each poll:
  1. Reads heartbeat.json + strategies.json + new lines from executions.log
  2. Updates the pinned status message in Discord
  3. Posts any new fills to #nt-fills
  4. Detects alert conditions (NT down, stale heartbeat, strategy disabled,
     account disconnected) and posts/recovers alerts in #nt-alerts
  5. Reloads accounts.yaml if it changed
  6. Sleeps until the next tick

Crashes are caught and logged; the loop never dies on a single bad poll.

Usage:
    python service.py                    # uses default config paths
    python service.py --share Z:/        # override share path
    python service.py --interval 30      # override poll interval
    python service.py --once             # run one cycle and exit (testing)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from accounts_config import AccountsRegistry
from discord_poster import DiscordConfig, DiscordPoster
from nt_reader import (
    AccountSnapshot,
    HeartbeatSnapshot,
    NTReader,
    StrategiesSnapshot,
)
from grid_builder import build_grid
from grid_discord_poster import post_grid
from grid_scheduler import events_to_fire
from grid_settings import GridSettings
from grid_state import GridState
from trades_discord_poster import post_trade_close
from pnl_tracker import PnLTracker
from web_server import SnapshotStore, WebServer

log = logging.getLogger("nt_monitor")


# ---------------------------------------------------------------------------
# Alert state — what was up last poll, so we can detect transitions
# ---------------------------------------------------------------------------


@dataclass
class AlertState:
    """Tracks which conditions were true on the previous poll."""
    nt_unreachable: bool = False
    nt_stale: bool = False
    # strategy_id -> was_enabled_last_poll
    strategy_enabled: dict[str, bool] = None
    # account_id -> was_connected_last_poll
    account_connected: dict[str, bool] = None
    # Have we successfully posted at least once? Used to suppress alerts on
    # the very first poll (so a service restart doesn't claim everything just
    # broke when actually we're just catching up).
    initialized: bool = False

    def __post_init__(self):
        if self.strategy_enabled is None:
            self.strategy_enabled = {}
        if self.account_connected is None:
            self.account_connected = {}


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class NTMonitorService:
    def __init__(
        self,
        share_path: str,
        accounts_path: str,
        discord_config_path: str,
        interval_sec: int,
        state_path: str = "state.json",
        web_host: str = "0.0.0.0",
        web_port: int = 8080,
        web_enabled: bool = True,
    ):
        self.share_path = share_path
        self.accounts_path = accounts_path
        self.interval_sec = interval_sec

        log.info("Loading accounts.yaml from %s", accounts_path)
        self.accounts = AccountsRegistry.load(accounts_path)

        log.info("Loading discord config from %s", discord_config_path)
        self.discord_cfg = DiscordConfig.load(discord_config_path)

        log.info("Connecting to share at %s", share_path)
        self.reader = NTReader(share_path)
        self.poster = DiscordPoster(self.discord_cfg, self.accounts)

        log.info("Loading P&L state from %s", state_path)
        self.pnl = PnLTracker(state_path)

        self.snapshot_store = SnapshotStore()
        self.web_server: Optional[WebServer] = None
        if web_enabled:
            self.web_server = WebServer(self.snapshot_store, web_host, web_port)

        # Grid feature — optional. If grid_settings.yaml is absent the
        # feature stays dormant; everything else runs unchanged.
        grid_settings_path = os.path.join(share_path, "grid_settings.yaml")
        grid_state_path    = os.path.join(share_path, "grid_state.json")
        self.grid_settings: Optional[GridSettings] = None
        try:
            self.grid_settings = GridSettings.load(grid_settings_path)
            log.info("Loaded grid_settings.yaml from %s", grid_settings_path)
        except FileNotFoundError:
            log.info(
                "No grid_settings.yaml at %s — grid feature disabled. "
                "Copy grid_settings.example.yaml and edit to enable.",
                grid_settings_path,
            )
        except Exception:
            log.exception(
                "grid_settings.yaml failed to load — grid feature disabled"
            )
        self.grid_state = GridState.load(grid_state_path)

        # Trade tracking: posted-trade IDs persist between service restarts
        # so trade-close announcements don't double-post after a restart.
        self.trades_posted_path = os.path.join(
            share_path, "trades_posted.json"
        )
        self.trades_posted: set[str] = self._load_trades_posted()

        self.alert_state = AlertState()
        self._stop = False

    # -----------------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------------

    def run(self, once: bool = False) -> None:
        log.info("Service starting (poll interval = %ds)", self.interval_sec)

        if self.web_server:
            self.web_server.start()

        # First poll: quick to start, don't wait the full interval
        next_tick = time.monotonic()

        while not self._stop:
            try:
                self.tick()
            except Exception:
                # Never let a bad poll kill the service. Log full traceback,
                # back off briefly, try again next tick.
                log.exception("Unexpected error in tick — continuing")

            if once:
                log.info("--once requested, exiting after one tick")
                return

            # Steady cadence: sleep until next_tick + interval, even if the
            # tick itself took some time. Skips ticks if we fell badly behind.
            next_tick += self.interval_sec
            sleep_for = next_tick - time.monotonic()
            if sleep_for < 0:
                # We fell behind — reset to "now" rather than burning catch-up
                # cycles that would just stress Discord's API.
                log.warning("Tick took longer than interval; resetting cadence")
                next_tick = time.monotonic()
                sleep_for = self.interval_sec

            # Sleep in small chunks so SIGINT can interrupt us promptly
            end = time.monotonic() + sleep_for
            while not self._stop and time.monotonic() < end:
                time.sleep(min(0.5, end - time.monotonic()))

        log.info("Service stopped")
        if self.web_server:
            self.web_server.stop()

    def request_stop(self, *_args) -> None:
        log.info("Stop requested")
        self._stop = True

    # -----------------------------------------------------------------------
    # Single poll cycle
    # -----------------------------------------------------------------------

    def tick(self) -> None:
        # Hot-reload account aliases if the file changed
        if self.accounts.reload_if_changed():
            log.info("accounts.yaml reloaded")

        # Cross the session boundary if needed (resets daily counters)
        self.pnl.check_session_reset()

        # Read everything
        hb = self.reader.read_heartbeat()
        st = self.reader.read_strategies()
        fills = self.reader.read_new_executions()

        # Apply fills to P&L tracker BEFORE building snapshot so the
        # dashboard reflects them on the same poll
        if fills:
            self.pnl.process_executions(fills)

        # Always update the status message — even when unreachable,
        # so the embed shows the red banner.
        try:
            self.poster.update_status_message(hb, st)
        except Exception:
            log.exception("Failed to update status message")

        # Post any new fills to Discord
        for fill in fills:
            try:
                self.poster.post_fill(fill)
            except Exception:
                log.exception("Failed to post fill: %s", fill)

        # Refresh the web dashboard snapshot
        self._update_dashboard_snapshot(hb, st)

        # Evaluate alerts
        self._check_alerts(hb, st)

        # Post the grid embed if a scheduled event has rolled over
        self._maybe_post_grid()
        self._maybe_post_trades()

        # First successful poll completed
        self.alert_state.initialized = True

    # -----------------------------------------------------------------------
    # Dashboard snapshot building
    # -----------------------------------------------------------------------

    def _update_dashboard_snapshot(
        self,
        hb: HeartbeatSnapshot,
        st: StrategiesSnapshot,
    ) -> None:
        """Builds the dict the web page consumes via /api/snapshot."""

        # Banner state — same logic as the Discord embed
        if not hb.is_reachable:
            color, title, subtitle = "red", "NT8 unreachable", hb.error or ""
        elif hb.is_stale:
            color, title = "red", "NT8 not updating"
            subtitle = f"Heartbeat is {hb.age_seconds:.0f}s old"
        elif any(not s.enabled for s in st.strategies if self.accounts.resolve(s.account)):
            color, title, subtitle = "yellow", "Strategy disabled", "One or more strategies are off"
        else:
            color, title, subtitle = "green", "Running normally", "All systems OK"

        # Account totals from the P&L tracker (account-level today)
        account_pnl = self.pnl.total_realized_today()

        # Account cards
        account_cards = []
        for a in hb.accounts:
            cfg = self.accounts.resolve(a.name)
            if cfg is None:
                continue
            account_cards.append({
                "id": a.name,
                "alias": cfg.alias,
                "type_label": cfg.type.title() if cfg.type else "",
                "connection": a.connection,
                "connected": a.connected,
                "cash_value": a.cash_value,
                "realized_pnl_today": account_pnl.get(a.name, 0.0),
            })

        # Per-symbol P&L rows
        pnl_rows = []
        for row in self.pnl.summary():
            cfg = self.accounts.resolve(row.account)
            if cfg is None:
                continue
            pnl_rows.append({
                "account": row.account,
                "account_alias": cfg.alias,
                "symbol": row.symbol,
                "realized_pnl": row.realized_pnl,
                "commission": row.commission,
                "net_pnl": row.realized_pnl - row.commission,
                "trades": row.trades,
                "wins": row.wins,
                "win_rate": row.win_rate,
                "open_position": row.open_position,
            })

        # Strategy cards
        strategy_cards = []
        for s in st.strategies:
            cfg = self.accounts.resolve(s.account)
            if cfg is None:
                continue
            strategy_cards.append({
                "name": s.name,
                "instrument": s.instrument,
                "account_alias": cfg.alias,
                "state": s.state,
                "enabled": s.enabled,
            })

        # Open positions across all visible accounts
        open_positions = []
        for a in hb.accounts:
            cfg = self.accounts.resolve(a.name)
            if cfg is None:
                continue
            for p in a.positions:
                open_positions.append({
                    "account_alias": cfg.alias,
                    "instrument": p.instrument,
                    "side": p.side,
                    "qty": p.qty,
                    "avg_price": p.avg_price,
                })

        # Grid feature: rebuild a fresh snapshot every tick from the latest
        # ATR file and current settings, then attach it to the dashboard
        # snapshot. This is independent of the scheduled Discord posts —
        # the web view always shows "what would the grid look like right
        # now", while Discord only fires at session boundaries.
        grid_dict = self._build_current_grid_for_web()
        trades_today = self._load_trades_today()

        snapshot = {
            "snapshot_at": datetime.now(timezone.utc).isoformat(),
            "status_color": color,
            "status_title": title,
            "status_subtitle": subtitle,
            "heartbeat_age_sec": hb.age_seconds,
            "heartbeat_reachable": hb.is_reachable,
            "accounts": account_cards,
            "pnl_rows": pnl_rows,
            "strategies": strategy_cards,
            "open_positions": open_positions,
            "grid": grid_dict,                 # None if feature not configured
            "trades": trades_today,            # list of today's closed trades (6pm->4pm window)
            "session_start_local": self._session_start_local().isoformat(),
        }
        self.snapshot_store.update(snapshot)

    # -----------------------------------------------------------------------
    # Trade tracking — reads trades.json from the share, posts new closures
    # to the configured Discord webhook, and exposes today's trades on the
    # dashboard snapshot. "Today" = the most recent 6pm-to-4pm ET window.
    # -----------------------------------------------------------------------

    def _session_start_local(self):
        """Most recent 6pm local-time boundary. If now >= 18:00, start =
        today 18:00; else start = yesterday 18:00. The session runs through
        next-day 16:00, matching the user's chosen trading-day window."""
        from datetime import timedelta
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
        now_local = datetime.now(tz=tz)
        boundary = now_local.replace(hour=18, minute=0, second=0, microsecond=0)
        if now_local < boundary:
            boundary -= timedelta(days=1)
        return boundary

    def _read_trades_file(self) -> list[dict]:
        """Read trades.json from the share. Returns [] if missing or invalid."""
        path = os.path.join(self.share_path, "trades.json")
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return list(data.get("trades", []))
        except Exception:
            log.exception("Failed to read trades.json")
            return []

    def _load_trades_today(self) -> list[dict]:
        """Subset of trades.json filtered to the current session window.
        Sorted newest-first."""
        from datetime import timezone as _tz
        from zoneinfo import ZoneInfo
        trades = self._read_trades_file()
        if not trades:
            return []
        local_tz = ZoneInfo("America/New_York")
        session_start_utc = self._session_start_local().astimezone(_tz.utc)
        out = []
        for t in trades:
            exit_iso = t.get("exit_time")
            if not exit_iso:
                continue
            try:
                # Handle both "2026-05-15T12:34:56+00:00" and "...Z"
                s = exit_iso[:-1] + "+00:00" if exit_iso.endswith("Z") else exit_iso
                exit_dt = datetime.fromisoformat(s)
            except Exception:
                continue
            # NT8's DateTime.ToString("o") on Unspecified-kind values emits
            # no offset; Python parses those as naive. Treat naive timestamps
            # as the NT machine's local time (America/New_York for this user).
            if exit_dt.tzinfo is None:
                exit_dt = exit_dt.replace(tzinfo=local_tz)
            if exit_dt >= session_start_utc:
                out.append(t)
        out.sort(key=lambda x: x.get("exit_time", ""), reverse=True)
        return out

    def _load_trades_posted(self) -> set:
        """Load the persistent set of trade IDs already posted to Discord."""
        if not os.path.exists(self.trades_posted_path):
            return set()
        try:
            with open(self.trades_posted_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return set(data.get("posted_ids", []))
        except Exception:
            log.exception(
                "Failed to read trades_posted.json — starting with empty set"
            )
            return set()

    def _save_trades_posted(self) -> None:
        try:
            tmp = self.trades_posted_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"posted_ids": sorted(self.trades_posted)}, f)
            os.replace(tmp, self.trades_posted_path)
        except Exception:
            log.exception("Failed to save trades_posted.json")

    def _maybe_post_trades(self) -> None:
        """Read trades.json, post newly-completed trades to Discord, and
        cap the in-memory set so it doesn't grow without bound."""
        if self.grid_settings is None:
            return
        url = (self.grid_settings.trades_webhook_url or "").strip()
        if not url:
            return  # Discord trade posting disabled

        trades = self._read_trades_file()
        if not trades:
            return

        new_any = False
        for t in trades:
            tid = t.get("id")
            if not tid or tid in self.trades_posted:
                continue

            # Backfilled trades come from the strategy's startup history replay
            # — they're real, but historical. The user wants them on the
            # dashboard, not in the Discord fills channel. Mark them as posted
            # so we never accidentally announce them later if the flag ever
            # gets dropped from the JSON, then move on.
            if t.get("backfilled"):
                self.trades_posted.add(tid)
                new_any = True
                continue

            ok = post_trade_close(url, t, timezone="America/New_York")
            if ok:
                self.trades_posted.add(tid)
                new_any = True
                log.info(
                    "Posted trade close: %s %s %s ct  P&L %s",
                    t.get("instrument"), t.get("side"), t.get("qty"),
                    t.get("pnl_dollars"),
                )

        if new_any:
            # Cap the posted set at 1000 entries to bound the state file size
            if len(self.trades_posted) > 1000:
                self.trades_posted = set(sorted(self.trades_posted)[-1000:])
            self._save_trades_posted()

    def _build_current_grid_for_web(self) -> Optional[dict]:
        """Build a current grid snapshot for the web dashboard.

        Returns None (rendered as 'no grid yet') if:
          - the grid feature isn't configured (no grid_settings.yaml), or
          - atr_ranges.json doesn't exist or won't parse.

        Never raises — failures log and degrade gracefully so a broken
        ATR file can't take down the dashboard update path.
        """
        if self.grid_settings is None:
            return None
        try:
            atr_path = os.path.join(self.share_path, "atr_ranges.json")
            with open(atr_path, "r", encoding="utf-8") as f:
                atr_data = json.load(f)
            # Use a neutral event name for the web view since it's not
            # tied to a particular session boundary.
            snap = build_grid(
                atr_data=atr_data,
                settings=self.grid_settings,
                event_name="Live",
            )
            return snap.to_dict()
        except Exception:
            log.exception("Failed to build grid for web dashboard")
            return None

    # -----------------------------------------------------------------------
    # Alert evaluation
    # -----------------------------------------------------------------------

    def _check_alerts(
        self,
        hb: HeartbeatSnapshot,
        st: StrategiesSnapshot,
    ) -> None:
        """
        Evaluates each alert condition and posts/recovers as appropriate.

        We're careful about the FIRST poll after service start: we don't want
        to claim "X just broke" when really we just don't know its previous
        state. So on the first poll we record state without firing alerts.
        """

        # --- Condition 1: NT8 unreachable (share down / files missing) ---
        if not hb.is_reachable:
            if self.alert_state.initialized:
                self.poster.post_alert(
                    key="nt_unreachable",
                    title="🔴 NT8 unreachable",
                    description=(
                        f"Cannot read heartbeat from `{self.share_path}`.\n"
                        f"Reason: `{hb.error}`\n\n"
                        "Likely causes: NT machine is down, network share is "
                        "offline, or share permissions changed."
                    ),
                    severity="error",
                )
            self.alert_state.nt_unreachable = True
            # Skip downstream checks — without a valid heartbeat, everything
            # else would falsely fire too.
            return
        else:
            if self.alert_state.nt_unreachable:
                self.poster.post_recovery(
                    key="nt_unreachable",
                    title="NT8 unreachable",
                    description="Heartbeat file is being read again.",
                )
            self.alert_state.nt_unreachable = False

        # --- Condition 2: Heartbeat stale (NT process up but monitor frozen) ---
        if hb.is_stale:
            if self.alert_state.initialized:
                self.poster.post_alert(
                    key="nt_stale",
                    title="🟡 NT8 monitor frozen",
                    description=(
                        f"Heartbeat is {hb.age_seconds:.0f}s old "
                        f"(expected refresh every {hb.poll_interval_sec}s).\n\n"
                        "NT8 process is alive (file exists) but the monitor "
                        "strategy isn't writing updates. Strategy may be "
                        "disabled, NT8 may be hung, or system clocks may "
                        "be out of sync."
                    ),
                    severity="warning",
                )
            self.alert_state.nt_stale = True
        else:
            if self.alert_state.nt_stale:
                self.poster.post_recovery(
                    key="nt_stale",
                    title="NT8 monitor frozen",
                    description="Heartbeat is fresh again.",
                )
            self.alert_state.nt_stale = False

        # --- Condition 3: Strategy enabled state changed ---
        # Only consider strategies on accounts we care about (per accounts.yaml).
        # Use account+name as a stable key so renaming or moving doesn't
        # confuse the tracking.
        current_strats: dict[str, tuple[bool, str, str, str]] = {}
        for s in st.strategies:
            cfg = self.accounts.resolve(s.account)
            if cfg is None:
                continue
            key = f"strat:{s.account}:{s.name}"
            current_strats[key] = (s.enabled, s.name, s.instrument, cfg.alias)

        # Newly disabled
        for key, (enabled, name, instr, acct_alias) in current_strats.items():
            was_enabled = self.alert_state.strategy_enabled.get(key)
            if was_enabled is True and not enabled and self.alert_state.initialized:
                self.poster.post_alert(
                    key=key,
                    title=f"🔴 Strategy disabled: {name}",
                    description=(
                        f"**{name}** on {instr} ({acct_alias}) was enabled "
                        f"and is now disabled. State: `{[s.state for s in st.strategies if s.name == name][0]}`."
                    ),
                    severity="error",
                )
            elif was_enabled is False and enabled:
                self.poster.post_recovery(
                    key=key,
                    title=f"Strategy disabled: {name}",
                    description=f"**{name}** is enabled again.",
                )

        # Strategy disappeared entirely (was tracked, now gone)
        gone = set(self.alert_state.strategy_enabled.keys()) - set(current_strats.keys())
        for key in gone:
            if self.alert_state.initialized:
                self.poster.post_alert(
                    key=key,
                    title="🔴 Strategy removed",
                    description=(
                        f"`{key}` was being tracked but no longer appears in NT8. "
                        "It may have been deleted from the chart."
                    ),
                    severity="warning",
                )

        # Update tracking state
        self.alert_state.strategy_enabled = {
            k: v[0] for k, v in current_strats.items()
        }

        # --- Condition 4: Account disconnected ---
        for acct in hb.accounts:
            cfg = self.accounts.resolve(acct.name)
            if cfg is None:
                continue
            key = f"acct:{acct.name}"
            was_connected = self.alert_state.account_connected.get(key)
            if was_connected is True and not acct.connected and self.alert_state.initialized:
                self.poster.post_alert(
                    key=key,
                    title=f"🔴 Account disconnected: {cfg.alias}",
                    description=(
                        f"**{cfg.display_name}** lost its connection.\n"
                        f"Connection: `{acct.connection or 'none'}`"
                    ),
                    severity="error",
                )
            elif was_connected is False and acct.connected:
                self.poster.post_recovery(
                    key=key,
                    title=f"Account disconnected: {cfg.alias}",
                    description=f"**{cfg.display_name}** is connected again.",
                )
            self.alert_state.account_connected[key] = acct.connected

    # -----------------------------------------------------------------------
    # Grid feature — scheduled embed posts to #trade-grid
    # -----------------------------------------------------------------------

    def _maybe_post_grid(self) -> None:
        """Fire any due grid events. No-op when the feature isn't configured."""
        if self.grid_settings is None:
            return

        # Hot-reload settings if the user edited the YAML
        try:
            self.grid_settings.reload_if_changed()
        except Exception:
            log.exception("grid_settings.reload_if_changed() failed; continuing")

        pending = events_to_fire(
            self.grid_settings.schedule, self.grid_state
        )
        if not pending:
            return

        # Read fresh ATR data for this batch (cheap — local file)
        atr_path = os.path.join(self.share_path, "atr_ranges.json")
        try:
            with open(atr_path, "r", encoding="utf-8") as f:
                atr_data = json.load(f)
        except Exception:
            log.exception(
                "Failed to read atr_ranges.json from %s; skipping grid post",
                atr_path,
            )
            return

        for pe in pending:
            try:
                snap = build_grid(
                    atr_data=atr_data,
                    settings=self.grid_settings,
                    event_name=pe.event.name,
                )
                post_grid(
                    webhook_url=self.grid_settings.webhook_url,
                    snapshot=snap,
                    state=self.grid_state,
                )
                self.grid_state.mark_fired(pe.event.name, pe.local_date_iso)
                log.info(
                    "Grid posted for event=%s date=%s",
                    pe.event.name, pe.local_date_iso,
                )
            except Exception:
                log.exception(
                    "Grid post failed for event=%s — will retry next tick",
                    pe.event.name,
                )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="NT8 → Discord monitor service")
    parser.add_argument("--share", default="Z:/",
                        help="Path to NT_Logger share (default: Z:/)")
    parser.add_argument("--accounts", default="accounts.yaml",
                        help="Path to accounts config (default: accounts.yaml)")
    parser.add_argument("--discord-config", default="discord_config.yaml",
                        help="Path to discord config (default: discord_config.yaml)")
    parser.add_argument("--interval", type=int, default=30,
                        help="Poll interval in seconds (default: 30)")
    parser.add_argument("--state", default="state.json",
                        help="Path to P&L state file (default: state.json)")
    parser.add_argument("--web-host", default="0.0.0.0",
                        help="Dashboard bind host (default: 0.0.0.0 = all interfaces)")
    parser.add_argument("--web-port", type=int, default=8080,
                        help="Dashboard port (default: 8080)")
    parser.add_argument("--no-web", action="store_true",
                        help="Disable the web dashboard")
    parser.add_argument("--once", action="store_true",
                        help="Run one tick and exit (for testing)")
    parser.add_argument("--log-level", default="INFO",
                        help="DEBUG, INFO, WARNING, ERROR")
    parser.add_argument("--log-file", default=None,
                        help="Optional file to write logs to (in addition to console)")
    args = parser.parse_args()

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )

    try:
        service = NTMonitorService(
            share_path=args.share,
            accounts_path=args.accounts,
            discord_config_path=args.discord_config,
            interval_sec=args.interval,
            state_path=args.state,
            web_host=args.web_host,
            web_port=args.web_port,
            web_enabled=not args.no_web,
        )
    except Exception as exc:
        log.error("Failed to start service: %s", exc)
        return 1

    # Graceful shutdown on Ctrl-C / kill
    signal.signal(signal.SIGINT, service.request_stop)
    signal.signal(signal.SIGTERM, service.request_stop)

    service.run(once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
