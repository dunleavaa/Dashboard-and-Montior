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
import logging
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
        }
        self.snapshot_store.update(snapshot)

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
