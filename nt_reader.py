"""
Reads the JSON files written by NT8's MonitorExporter strategy.

Handles:
  - Network share unavailability (NT machine down, share offline, network issue)
  - Stale files (NT process running but strategy frozen)
  - Partial reads (file being written as we read it — atomic rename in NT8 side
    makes this rare but we handle it anyway)
  - Tailing executions.log for new fills since last poll

Returns dataclass snapshots that the rest of the service consumes. None of
this code knows about Discord, the dashboard, or anything downstream.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes that mirror the JSON shape, with derived health fields added.
# ---------------------------------------------------------------------------


@dataclass
class Position:
    instrument: str
    side: str          # "Long" or "Short"
    qty: int
    avg_price: float


@dataclass
class AccountSnapshot:
    name: str
    connection: str
    connected: bool
    cash_value: Optional[float]
    realized_pnl: Optional[float]
    buying_power: Optional[float]
    positions: list[Position] = field(default_factory=list)


@dataclass
class HeartbeatSnapshot:
    """A single read of heartbeat.json, with health metadata."""

    # Fresh data
    timestamp_utc: Optional[datetime]   # what NT wrote into the file
    poll_interval_sec: int
    accounts: list[AccountSnapshot]

    # Health metadata derived by the reader
    read_at: datetime                   # when WE read the file (always set)
    age_seconds: Optional[float]        # how stale the file is
    is_stale: bool                      # True if older than the staleness threshold
    is_reachable: bool                  # False if we couldn't read the file at all
    error: Optional[str] = None         # human-readable failure reason if !is_reachable


@dataclass
class StrategyEntry:
    account: str
    name: str
    instrument: str
    state: str         # "Realtime", "Historical", "Disabled", etc.
    enabled: bool


@dataclass
class StrategiesSnapshot:
    timestamp_utc: Optional[datetime]
    strategies: list[StrategyEntry]
    read_at: datetime
    age_seconds: Optional[float]
    is_stale: bool
    is_reachable: bool
    error: Optional[str] = None


@dataclass
class Execution:
    """One line from executions.log."""
    timestamp_utc: datetime
    exec_time: datetime
    account: str
    instrument: str
    action: str        # "Long" or "Short" (this is MarketPosition, i.e. resulting position direction)
    qty: int
    price: float
    order_id: str
    exec_id: str
    strategy: str
    commission: float


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


class NTReader:
    """
    Polls the network-mounted NT_Logger directory for snapshot files.

    Lifetime: long-lived, one instance per service. Maintains state for
    tailing executions.log (file offset, inode tracking).
    """

    def __init__(
        self,
        logger_dir: str | Path,
        staleness_multiplier: float = 3.0,
        min_staleness_seconds: float = 75.0,
    ):
        """
        Args:
          logger_dir: path to NT_Logger (e.g. "Z:/" on Windows, or
                      "/mnt/nt_logger" on Linux, or a UNC path like
                      r"\\\\NTMACHINE\\NT_Logger").
          staleness_multiplier: a heartbeat is "stale" if it's older than
                      poll_interval_sec * this multiplier. Default 3x means
                      we tolerate 2 missed polls before flagging.
          min_staleness_seconds: floor on the staleness threshold. Even if
                      poll_interval is short, never flag stale below this.
        """
        self.dir = Path(logger_dir)
        self.staleness_multiplier = staleness_multiplier
        self.min_staleness_seconds = min_staleness_seconds

        # Tail state for executions.log
        self._exec_offset: int = 0
        self._exec_inode: Optional[int] = None  # detect file replacement (NT restart)
        self._exec_initialized: bool = False

    # -----------------------------------------------------------------------
    # Public reads
    # -----------------------------------------------------------------------

    def read_heartbeat(self) -> HeartbeatSnapshot:
        now = datetime.now(timezone.utc)
        path = self.dir / "heartbeat.json"

        try:
            data = self._read_json_atomic(path)
        except FileNotFoundError:
            return HeartbeatSnapshot(
                timestamp_utc=None, poll_interval_sec=0, accounts=[],
                read_at=now, age_seconds=None, is_stale=True,
                is_reachable=False, error="heartbeat.json not found",
            )
        except (OSError, IOError) as exc:
            return HeartbeatSnapshot(
                timestamp_utc=None, poll_interval_sec=0, accounts=[],
                read_at=now, age_seconds=None, is_stale=True,
                is_reachable=False, error=f"share unreachable: {exc}",
            )
        except json.JSONDecodeError as exc:
            return HeartbeatSnapshot(
                timestamp_utc=None, poll_interval_sec=0, accounts=[],
                read_at=now, age_seconds=None, is_stale=True,
                is_reachable=False, error=f"invalid JSON: {exc}",
            )

        ts = _parse_iso(data.get("timestamp_utc"))
        poll = int(data.get("poll_interval_sec") or 0)
        age = (now - ts).total_seconds() if ts else None

        threshold = max(self.min_staleness_seconds, poll * self.staleness_multiplier) if poll else self.min_staleness_seconds
        is_stale = age is None or age > threshold

        accounts = []
        for a in data.get("accounts", []) or []:
            positions = [
                Position(
                    instrument=p.get("instrument", ""),
                    side=p.get("side", ""),
                    qty=int(p.get("qty") or 0),
                    avg_price=float(p.get("avg_price") or 0),
                )
                for p in (a.get("positions") or [])
            ]
            accounts.append(AccountSnapshot(
                name=a.get("name", ""),
                connection=a.get("connection", "") or "",
                connected=bool(a.get("connected", False)),
                cash_value=_to_float(a.get("cash_value")),
                realized_pnl=_to_float(a.get("realized_pnl")),
                buying_power=_to_float(a.get("buying_power")),
                positions=positions,
            ))

        return HeartbeatSnapshot(
            timestamp_utc=ts,
            poll_interval_sec=poll,
            accounts=accounts,
            read_at=now,
            age_seconds=age,
            is_stale=is_stale,
            is_reachable=True,
        )

    def read_strategies(self) -> StrategiesSnapshot:
        now = datetime.now(timezone.utc)
        path = self.dir / "strategies.json"

        try:
            data = self._read_json_atomic(path)
        except FileNotFoundError:
            return StrategiesSnapshot(
                timestamp_utc=None, strategies=[],
                read_at=now, age_seconds=None, is_stale=True,
                is_reachable=False, error="strategies.json not found",
            )
        except (OSError, IOError) as exc:
            return StrategiesSnapshot(
                timestamp_utc=None, strategies=[],
                read_at=now, age_seconds=None, is_stale=True,
                is_reachable=False, error=f"share unreachable: {exc}",
            )
        except json.JSONDecodeError as exc:
            return StrategiesSnapshot(
                timestamp_utc=None, strategies=[],
                read_at=now, age_seconds=None, is_stale=True,
                is_reachable=False, error=f"invalid JSON: {exc}",
            )

        ts = _parse_iso(data.get("timestamp_utc"))
        age = (now - ts).total_seconds() if ts else None
        is_stale = age is None or age > self.min_staleness_seconds

        strategies = [
            StrategyEntry(
                account=s.get("account", ""),
                name=s.get("name", ""),
                instrument=s.get("instrument", ""),
                state=s.get("state", ""),
                enabled=bool(s.get("enabled", False)),
            )
            for s in (data.get("strategies") or [])
        ]

        return StrategiesSnapshot(
            timestamp_utc=ts, strategies=strategies,
            read_at=now, age_seconds=age, is_stale=is_stale,
            is_reachable=True,
        )

    def read_new_executions(self) -> list[Execution]:
        """
        Tails executions.log and returns Execution objects for any new lines
        since the last call. Handles file rotation (NT restart truncates
        the log) by detecting inode/size changes.

        The first call in a service lifetime seeks to end-of-file rather
        than replaying the entire history; that prevents a startup flood
        of "new" executions for fills that already happened yesterday.
        """
        path = self.dir / "executions.log"
        try:
            stat = path.stat()
        except FileNotFoundError:
            return []
        except OSError as exc:
            log.warning("Could not stat executions.log: %s", exc)
            return []

        # First-ever call: skip to end so we don't replay history
        if not self._exec_initialized:
            self._exec_offset = stat.st_size
            self._exec_inode = _try_inode(stat)
            self._exec_initialized = True
            return []

        # Detect rotation: file got smaller, or inode changed
        current_inode = _try_inode(stat)
        if (
            stat.st_size < self._exec_offset
            or (current_inode is not None and current_inode != self._exec_inode)
        ):
            log.info("executions.log rotated (size or inode changed); resetting tail")
            self._exec_offset = 0
            self._exec_inode = current_inode

        if stat.st_size == self._exec_offset:
            return []

        new_execs: list[Execution] = []
        try:
            with path.open("r", encoding="utf-8") as f:
                f.seek(self._exec_offset)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        new_execs.append(_parse_execution(rec))
                    except (json.JSONDecodeError, ValueError, KeyError) as exc:
                        # A partially-written line at the very end is the most
                        # likely cause. We'll re-read it on the next poll once
                        # NT finishes writing.
                        log.debug("Skipping unparseable execution line: %s", exc)
                        # Don't advance offset past the bad line; back off and
                        # try again next poll.
                        return new_execs
                self._exec_offset = f.tell()
        except (OSError, IOError) as exc:
            log.warning("Failed reading executions.log: %s", exc)
            return new_execs

        return new_execs

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _read_json_atomic(self, path: Path) -> dict:
        """
        Reads a JSON file with one retry on transient errors.

        NT8 writes via temp-file-then-rename, so we should never see a
        partial file in practice. But on Windows network shares, a rename
        observed mid-flight can briefly produce a "file in use" error.
        One short retry handles that case cleanly.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(2):
            try:
                with path.open("r", encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, IOError, json.JSONDecodeError) as exc:
                last_exc = exc
                if attempt == 0:
                    time.sleep(0.1)
        # Re-raise the last exception with original type so callers can branch
        raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        # NT writes "2026-05-02T15:23:46.6440043Z" — Python 3.11+ handles the Z
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _try_inode(stat) -> Optional[int]:
    # st_ino is 0 on some Windows network shares. Treat 0 as "unknown" so
    # we don't constantly think the file rotated.
    ino = getattr(stat, "st_ino", None)
    return ino if ino else None


def _parse_execution(rec: dict) -> Execution:
    return Execution(
        timestamp_utc=_parse_iso(rec.get("timestamp_utc")) or datetime.now(timezone.utc),
        exec_time=_parse_iso(rec.get("exec_time")) or datetime.now(timezone.utc),
        account=rec.get("account", ""),
        instrument=rec.get("instrument", ""),
        action=rec.get("action", ""),
        qty=int(rec.get("qty") or 0),
        price=float(rec.get("price") or 0),
        order_id=rec.get("order_id", ""),
        exec_id=rec.get("exec_id", ""),
        strategy=rec.get("strategy", ""),
        commission=float(rec.get("commission") or 0),
    )
