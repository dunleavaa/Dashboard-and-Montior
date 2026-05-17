# NT_Logger — NT8 status & ATR export → Python dashboard / alerts / grid

A two-part pipeline that gets live trading state out of NinjaTrader 8 and
into a Python service that drives a web dashboard, Discord alerts, and a
risk-sizing grid.

```
┌──────────────────────┐    JSON files on    ┌──────────────────────┐
│  NT8 strategy        │   a share folder    │  Python service      │
│  MonitorExporter.cs  │ ──────────────────► │  service.py          │
│  (no trading,        │                     │  (dashboard, alerts, │
│   just exports)      │                     │   Discord grid)      │
└──────────────────────┘                     └──────────────────────┘
```

The NT8 strategy writes four files; the Python service reads them. All
file I/O is local disk (sub-millisecond). Nothing in NT8 makes network
calls — that's all on the Python side, fully decoupled.

## Files written by `MonitorExporter.cs`

| File              | Cadence              | Purpose                                       |
|-------------------|----------------------|-----------------------------------------------|
| `heartbeat.json`  | every N seconds      | status snapshot (connection, P&L, accounts)   |
| `executions.log`  | append-only          | one JSON line per fill, written immediately   |
| `strategies.json` | every N seconds      | list of enabled strategies on the workstation |
| `atr_ranges.json` | every N seconds      | latest ATR per instrument across 1m/5m/15m/daily plus tick size & point value |

---

## Part 1 — ATR export (NT8 side)

The ATR export feeds two things on the Python side: the grid's risk
sizing (so the suggested SL/TP distances are scaled by current
volatility, not hard-coded) and the future AI-signal sanity envelope
(reject incoming signals whose SL or TP distance is implausible relative
to current ATR).

### Properties on the strategy

Configure these in the NT8 strategy dialog under the **ATR** group:

| Property         | Default                              | Notes                                                     |
|------------------|--------------------------------------|-----------------------------------------------------------|
| `AtrInstruments` | `MNQ 06-26,MES 06-26,MGC 06-26`      | Comma-separated NT8 contract codes. Leave blank to disable ATR export. **Update at rollover.** |
| `AtrPeriod`      | `14`                                 | Lookback period. 14 is standard.                          |

For each instrument in `AtrInstruments`, the strategy adds **four** data
series at startup (1-minute, 5-minute, 15-minute, daily). NT8 will
backfill them on load; the first valid ATR value per series appears
after `AtrPeriod` bar closes on that timeframe.

### Output format — `atr_ranges.json`

```json
{
  "timestamp_utc": "2026-05-17T14:32:01.1234567Z",
  "atr_period": 14,
  "instruments": {
    "MNQ 06-26": {
      "atr_1m":     3.2,
      "atr_5m":    11.8,
      "atr_15m":   22.3,
      "atr_daily": 287.5,
      "tick_size": 0.25,
      "point_value": 2.0,
      "bars_loaded_1m":    1440,
      "bars_loaded_5m":     288,
      "bars_loaded_15m":     96,
      "bars_loaded_daily":   30,
      "updated_1m":     "2026-05-17T14:32:00.0000000Z",
      "updated_5m":     "2026-05-17T14:30:00.0000000Z",
      "updated_15m":    "2026-05-17T14:30:00.0000000Z",
      "updated_daily":  "2026-05-16T22:00:00.0000000Z"
    }
  }
}
```

`tick_size` and `point_value` come from `MasterInstrument` so the Python
consumer can convert ATR points → ticks → dollars per contract without
needing its own lookup table.

### Contract rollover — the one gotcha

NT8 resolves contract codes literally. `MNQ 06-26` means **June 2026
e-mini Nasdaq** specifically, not "front-month MNQ". When you roll:

1. Disable `MonitorExporter` in the Strategies tab.
2. Edit the strategy properties → change `AtrInstruments` to the new month
   (e.g. `MNQ 09-26,MES 09-26,MGC 09-26`).
3. Re-enable the strategy. Expect a brief cold-load wait the first time
   NT8 sees the new contracts — daily bars in particular can take ~30s.

If you forget, you'll see `bars_loaded_*: 0` for the stale instrument in
the JSON, and the dashboard will flag missing ATR for it.

### Troubleshooting via the `bars_loaded_*` fields

These exist so you can distinguish three different failure modes
without attaching a debugger:

| Symptom                                       | Likely cause                                                                 |
|-----------------------------------------------|------------------------------------------------------------------------------|
| `atr_X` is `null`, `bars_loaded_X` is `0`     | `AddDataSeries` silently failed (bad contract code) or the data subscription doesn't cover that bar type. Check the NT8 Output window for `"AddDataSeries failed for ..."`. |
| `atr_X` is `null`, `bars_loaded_X` is < period | Series is loaded but still warming up. Will populate after `AtrPeriod` bar closes. |
| `atr_X` populated, `updated_X` is stale       | Data feed disconnected or instrument session is closed. Check NT8 connection status. |

---

## Part 2 — Grid feature (Python side)

Drop these five files into the same folder as `service.py`:

```
grid_settings.py
grid_state.py
grid_builder.py
grid_scheduler.py
grid_discord_poster.py
```

Drop the YAML into the same folder as `atr_ranges.json` (the NT_Logger
share), then edit it to set your webhook URL and risk numbers:

```
\\share\NT_Logger\grid_settings.yaml
```

The state file lives in the same folder as the YAML — the service creates
it automatically on first run:

```
\\share\NT_Logger\grid_state.json
```

### One-time Discord setup

1. Create the channel `#trade-grid` (or whatever you want to call it).
2. Channel settings → Integrations → Webhooks → New Webhook → copy URL.
3. Paste that URL into `webhook_url:` in `grid_settings.yaml`.
4. Wait for the first scheduled event to fire (or restart the service to
   pick up the YAML changes).
5. When the first grid post appears, **right-click → Pin Message** ONCE.
   The service will then keep editing that same pinned message in place
   on every subsequent post.

If you ever delete the pinned message accidentally, the service notices
the next time it tries to PATCH, posts a fresh one, and you re-pin once.

### service.py integration

Three small additions to the existing `service.py`. All additive — no
existing code changes.

#### 1. Imports (near the other `from … import …` lines)

```python
from grid_builder import build_grid
from grid_discord_poster import post_grid
from grid_scheduler import events_to_fire
from grid_settings import GridSettings
from grid_state import GridState
```

#### 2. `__init__` — load settings + state alongside the others

```python
# Inside NTMonitorService.__init__, after the existing self.pnl load:

import os

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
```

#### 3. `tick()` — at the end, after the existing alert check

```python
# Inside NTMonitorService.tick(), after self._check_alerts(...):

self._maybe_post_grid()
```

#### 4. New helper method on the service class

Add at the bottom of the class, alongside `_check_alerts`:

```python
def _maybe_post_grid(self) -> None:
    """Fire any due grid events. No-op when the feature isn't configured."""
    if self.grid_settings is None:
        return

    # Hot-reload settings if the user edited the YAML
    try:
        self.grid_settings.reload_if_changed()
    except Exception:
        log.exception("grid_settings.reload_if_changed() failed; continuing")

    pending = events_to_fire(self.grid_settings.schedule, self.grid_state)
    if not pending:
        return

    # Read fresh ATR data per event (cheap — local file)
    try:
        import json
        atr_path = os.path.join(self.share_path, "atr_ranges.json")
        with open(atr_path, "r", encoding="utf-8") as f:
            atr_data = json.load(f)
    except Exception:
        log.exception("Failed to read atr_ranges.json; skipping grid post")
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
```

That's it. ~30 lines added to service.py, zero changes to existing logic.

### Dependencies

The grid feature uses two libraries you likely already have, plus stdlib:

```
pip install requests pyyaml
```

`zoneinfo` is stdlib in Python 3.9+. If you're on Windows and see a
`ZoneInfoNotFoundError`, also install `tzdata`:

```
pip install tzdata
```

### Testing without waiting for the next bell

To verify the pipeline works without waiting for 09:32 ET, temporarily
add an event a few minutes in the future to `grid_settings.yaml`:

```yaml
events:
  - { name: "Cash open",     time: "09:30" }
  - { name: "Cash close",    time: "16:00" }
  - { name: "Futures close", time: "17:00" }
  - { name: "Futures open",  time: "18:00" }
  - { name: "TEST",          time: "14:55" }   # pick something 2-3 min out
```

Save the file. Within one poll interval (30s default) the service hot-
reloads it. When 14:57 ET arrives (the 14:55 trigger + 2-min offset),
you'll see the post appear in `#trade-grid`. Right-click → Pin it.
Remove the TEST line, save again — service hot-reloads, future cycles
run on the real schedule, pinned message keeps updating in place.
