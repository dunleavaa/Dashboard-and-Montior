"""
Per-(account, symbol) realized P&L tracker.

Consumes Execution events from nt_reader and maintains FIFO position state
per (account, symbol). Realized P&L is booked whenever a fill reduces an
existing position. Open positions are tracked but their unrealized P&L
is not computed here (that lives in NT8's heartbeat).

Daily reset: at the configured session-open time (default 6 PM ET), all
realized P&L counters reset to zero. Open position state survives the
reset (a position you held overnight is still held in the new session).

Persists to state.json so a service restart doesn't lose intra-session
P&L. The file is owned end-to-end by this module — never edited by hand.
"""

from __future__ import annotations

import json
import logging
import os
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from nt_reader import Execution

log = logging.getLogger(__name__)


# Multipliers for futures contracts. Used to convert price difference
# into dollar P&L. Add new symbols here as needed.
#
# Format: {symbol_root: dollars_per_point}
# Example: NQ moves 1 point = $20 (E-mini Nasdaq, $20 per point)
CONTRACT_MULTIPLIERS = {
    "NQ":  20.0,    # E-mini Nasdaq 100
    "MNQ": 2.0,     # Micro Nasdaq
    "ES":  50.0,    # E-mini S&P 500
    "MES": 5.0,     # Micro S&P
    "YM":  5.0,     # E-mini Dow
    "MYM": 0.5,     # Micro Dow
    "RTY": 50.0,    # E-mini Russell
    "M2K": 5.0,     # Micro Russell
    "GC":  100.0,   # Gold
    "MGC": 10.0,    # Micro Gold
    "SI":  5000.0,  # Silver (5,000 oz)
    "SIL": 1000.0,  # Micro Silver (1,000 oz)
    "CL":  1000.0,  # Crude Oil
    "MCL": 100.0,   # Micro Crude
    "NG":  10000.0, # Natural Gas
    "ZB":  1000.0,  # 30-Year Bond
    "ZN":  1000.0,  # 10-Year Note
    "6E":  125000.0,# Euro FX
    "6J":  125000.0,# Japanese Yen
}

# Default if symbol root isn't in the table. We log a warning so it's
# obvious that the P&L numbers for that symbol are wrong by a known factor.
DEFAULT_MULTIPLIER = 1.0


def get_multiplier(instrument_full_name: str) -> float:
    """
    Extract the contract root from "NQ 06-26" → "NQ" → look up multiplier.
    """
    root = (instrument_full_name or "").split(" ", 1)[0].upper()
    mult = CONTRACT_MULTIPLIERS.get(root)
    if mult is None:
        log.warning(
            "No multiplier for instrument '%s' (root '%s') — using 1.0. "
            "Add it to CONTRACT_MULTIPLIERS in pnl_tracker.py.",
            instrument_full_name, root,
        )
        return DEFAULT_MULTIPLIER
    return mult


# ---------------------------------------------------------------------------
# State shapes
# ---------------------------------------------------------------------------


@dataclass
class OpenLot:
    """A single un-closed contract at a specific entry price."""
    side: str          # "long" or "short"
    qty: int           # always positive; sign is captured in 'side'
    price: float
    opened_at: str     # ISO timestamp


@dataclass
class SymbolState:
    """Per-(account, symbol) tracker."""
    open_lots: deque[OpenLot] = field(default_factory=deque)
    realized_pnl: float = 0.0    # session-to-date, dollars
    trades_today: int = 0        # # of closed contracts (each close counts as 1)
    wins_today: int = 0          # # of closed contracts with positive P&L
    commission_today: float = 0.0
    last_fill_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "open_lots": [asdict(lot) for lot in self.open_lots],
            "realized_pnl": self.realized_pnl,
            "trades_today": self.trades_today,
            "wins_today": self.wins_today,
            "commission_today": self.commission_today,
            "last_fill_at": self.last_fill_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SymbolState":
        s = cls()
        for lot in d.get("open_lots", []):
            s.open_lots.append(OpenLot(
                side=lot["side"],
                qty=int(lot["qty"]),
                price=float(lot["price"]),
                opened_at=lot.get("opened_at", ""),
            ))
        s.realized_pnl = float(d.get("realized_pnl", 0))
        s.trades_today = int(d.get("trades_today", 0))
        s.wins_today = int(d.get("wins_today", 0))
        s.commission_today = float(d.get("commission_today", 0))
        s.last_fill_at = d.get("last_fill_at")
        return s


@dataclass
class PnLSummary:
    """A single row in the per-symbol summary table."""
    account: str
    symbol: str
    realized_pnl: float
    trades: int
    wins: int
    win_rate: float       # 0.0 to 1.0
    commission: float
    open_position: int    # signed: positive = long, negative = short, 0 = flat


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


class PnLTracker:
    """
    Owns per-(account, symbol) state. One instance per service.
    """

    def __init__(
        self,
        state_path: str | Path,
        session_reset_hour_local: int = 18,  # 6 PM
        session_reset_timezone: str = "America/New_York",
    ):
        self.state_path = Path(state_path)
        self.tz = ZoneInfo(session_reset_timezone)
        self.session_reset_hour = session_reset_hour_local

        # Keyed by (account_id, instrument_full_name)
        self.state: dict[tuple[str, str], SymbolState] = {}

        # The session-open instant used by the current state. When we cross
        # into a new session, we reset counters and update this.
        self.current_session_open: datetime = self._most_recent_session_open(
            datetime.now(timezone.utc)
        )

        self._load()

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def process_executions(self, execs: list[Execution]) -> None:
        """
        Apply a batch of new executions to the running state.
        Caller (the service) is responsible for calling check_session_reset()
        before this so executions land in the right session.
        """
        for exc in execs:
            self._process_one(exc)
        if execs:
            self._save()

    def check_session_reset(self, now: Optional[datetime] = None) -> bool:
        """
        Compare wall clock to the session boundary; if we've crossed it,
        reset realized counters and update current_session_open.

        Returns True if a reset happened (caller may want to log it).
        """
        now = now or datetime.now(timezone.utc)
        latest_open = self._most_recent_session_open(now)
        if latest_open > self.current_session_open:
            log.info(
                "Session reset: %s -> %s",
                self.current_session_open.isoformat(),
                latest_open.isoformat(),
            )
            for st in self.state.values():
                st.realized_pnl = 0.0
                st.trades_today = 0
                st.wins_today = 0
                st.commission_today = 0.0
                # Open lots survive the reset — they're still held positions
            self.current_session_open = latest_open
            self._save()
            return True
        return False

    def summary(self) -> list[PnLSummary]:
        """
        Returns one row per (account, symbol) currently tracked or holding
        open positions. Sorted by account name then symbol.
        """
        rows: list[PnLSummary] = []
        for (acct, sym), st in self.state.items():
            # Sum signed open quantity
            open_qty = sum(
                lot.qty if lot.side == "long" else -lot.qty
                for lot in st.open_lots
            )
            wr = (st.wins_today / st.trades_today) if st.trades_today else 0.0
            rows.append(PnLSummary(
                account=acct, symbol=sym,
                realized_pnl=st.realized_pnl,
                trades=st.trades_today,
                wins=st.wins_today,
                win_rate=wr,
                commission=st.commission_today,
                open_position=open_qty,
            ))
        rows.sort(key=lambda r: (r.account, r.symbol))
        return rows

    def total_realized_today(self) -> dict[str, float]:
        """Account → total realized P&L across all symbols (after commission)."""
        totals: dict[str, float] = {}
        for (acct, _sym), st in self.state.items():
            totals[acct] = totals.get(acct, 0.0) + st.realized_pnl - st.commission_today
        return totals

    # -----------------------------------------------------------------------
    # Internal: FIFO matching engine
    # -----------------------------------------------------------------------

    def _process_one(self, exc: Execution) -> None:
        if exc.qty <= 0:
            return
        key = (exc.account, exc.instrument)
        st = self.state.setdefault(key, SymbolState())
        st.last_fill_at = exc.exec_time.isoformat() if exc.exec_time else None
        st.commission_today += exc.commission

        # NT8's "action" field on Execution is the resulting MarketPosition,
        # which is awkward — it tells us the *direction* but we still need
        # to match against existing open lots to know if this opened or closed.
        #
        # The reliable approach: look at our current open position. If we're
        # flat or this fill is in the same direction as the existing lots,
        # it OPENS new lots. If it's the opposite direction, it CLOSES (and
        # may flip the position if qty exceeds what's open).
        action_side = (exc.action or "").lower()
        if action_side not in ("long", "short"):
            log.warning("Unknown action '%s' on execution %s", exc.action, exc.exec_id)
            return

        # Determine current net position direction
        current_side = self._current_side(st)
        mult = get_multiplier(exc.instrument)

        if current_side is None or current_side == action_side:
            # Opening or adding to the existing position
            st.open_lots.append(OpenLot(
                side=action_side,
                qty=exc.qty,
                price=exc.price,
                opened_at=exc.exec_time.isoformat() if exc.exec_time else "",
            ))
            return

        # Closing (opposite direction). Walk the FIFO queue and book P&L.
        remaining = exc.qty
        while remaining > 0 and st.open_lots:
            lot = st.open_lots[0]
            if lot.side == action_side:
                # Direction flipped mid-walk — should not happen with FIFO
                # but guard anyway. Treat the rest as opening new lots.
                break
            close_qty = min(remaining, lot.qty)
            # P&L per contract:
            #   long lot closed by sell: (sell_price - entry_price) * mult
            #   short lot closed by buy: (entry_price - buy_price) * mult
            if lot.side == "long":
                pnl = (exc.price - lot.price) * mult * close_qty
            else:
                pnl = (lot.price - exc.price) * mult * close_qty

            st.realized_pnl += pnl
            st.trades_today += close_qty
            if pnl > 0:
                st.wins_today += close_qty
            # NOTE: we count contracts, not "trades" in the round-trip sense.
            # Closing 2 contracts in one fill = 2 toward trades_today.
            # If the user wants round-trip semantics later we can add a
            # separate counter that increments only when position goes flat.

            lot.qty -= close_qty
            remaining -= close_qty
            if lot.qty == 0:
                st.open_lots.popleft()

        # If the fill was larger than the open position, the remainder
        # opens a new lot in the opposite direction (position flip).
        if remaining > 0:
            st.open_lots.append(OpenLot(
                side=action_side,
                qty=remaining,
                price=exc.price,
                opened_at=exc.exec_time.isoformat() if exc.exec_time else "",
            ))

    def _current_side(self, st: SymbolState) -> Optional[str]:
        """Returns 'long', 'short', or None (flat) based on open lots."""
        if not st.open_lots:
            return None
        # All open lots should be the same side (FIFO closing prevents mixing).
        return st.open_lots[0].side

    # -----------------------------------------------------------------------
    # Session boundary
    # -----------------------------------------------------------------------

    def _most_recent_session_open(self, now_utc: datetime) -> datetime:
        """
        Returns the most recent session-open instant <= now, in UTC.

        E.g. if reset hour is 18 ET and now is Tuesday 9 AM ET, returns
        Monday 6 PM ET (in UTC). If now is Tuesday 7 PM ET, returns
        Tuesday 6 PM ET (in UTC).
        """
        now_local = now_utc.astimezone(self.tz)
        candidate = now_local.replace(
            hour=self.session_reset_hour, minute=0, second=0, microsecond=0,
        )
        if candidate > now_local:
            candidate -= timedelta(days=1)
        return candidate.astimezone(timezone.utc)

    # -----------------------------------------------------------------------
    # Persistence
    # -----------------------------------------------------------------------

    def _load(self) -> None:
        if not self.state_path.exists():
            log.info("No existing state.json; starting fresh")
            return
        try:
            with self.state_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            log.error("state.json unreadable, starting fresh: %s", exc)
            return

        saved_open = data.get("session_open")
        if saved_open:
            try:
                saved_open_dt = datetime.fromisoformat(saved_open)
            except ValueError:
                saved_open_dt = self.current_session_open
        else:
            saved_open_dt = self.current_session_open

        # If the saved session is older than the current one, the service
        # was off during a session boundary. Drop the realized counters
        # but keep open positions (they're still held).
        latest = self._most_recent_session_open(datetime.now(timezone.utc))
        rolled_over = saved_open_dt < latest

        for key, val in (data.get("state") or {}).items():
            try:
                acct, sym = key.split("|", 1)
            except ValueError:
                continue
            st = SymbolState.from_dict(val)
            if rolled_over:
                st.realized_pnl = 0.0
                st.trades_today = 0
                st.wins_today = 0
                st.commission_today = 0.0
            self.state[(acct, sym)] = st

        self.current_session_open = latest if rolled_over else saved_open_dt

        if rolled_over:
            log.info("State loaded across a session boundary; counters reset")
        else:
            log.info("Loaded state for %d (account, symbol) pairs", len(self.state))

    def _save(self) -> None:
        data = {
            "session_open": self.current_session_open.isoformat(),
            "state": {
                f"{acct}|{sym}": st.to_dict()
                for (acct, sym), st in self.state.items()
            },
        }
        # Atomic write: tmp file then rename
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.state_path)
        except OSError as exc:
            log.error("Failed to write state.json: %s", exc)
