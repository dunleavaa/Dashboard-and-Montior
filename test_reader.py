"""
Quick test that the reader can see the NT8 logger files.
Run from the same folder as nt_reader.py, accounts_config.py, accounts.yaml.

Usage:
    python test_reader.py
    python test_reader.py Z:/
    python test_reader.py //NTMACHINE/NT_Logger
"""

import sys
from nt_reader import NTReader
from accounts_config import AccountsRegistry

# Default to Z:/ but allow override on command line
share_path = sys.argv[1] if len(sys.argv) > 1 else "Z:/"

print(f"Reading from: {share_path}\n")

reader = NTReader(share_path)
reg = AccountsRegistry.load("accounts.yaml")

# --- Heartbeat ---
hb = reader.read_heartbeat()

print("=" * 60)
print("HEARTBEAT")
print("=" * 60)
print(f"  reachable: {hb.is_reachable}")
print(f"  stale:     {hb.is_stale}")
if hb.error:
    print(f"  ERROR:     {hb.error}")
if hb.timestamp_utc:
    print(f"  NT time:   {hb.timestamp_utc}")
    print(f"  age:       {hb.age_seconds:.1f} seconds")
print(f"  accounts:  {len(hb.accounts)} in file")

print(f"\nAccounts after accounts.yaml filter:")
shown = 0
for a in hb.accounts:
    cfg = reg.resolve(a.name)
    if cfg is None:
        continue
    shown += 1
    status = "ONLINE " if a.connected else "offline"
    cash = a.cash_value if a.cash_value is not None else 0
    print(f"  [{status}] {cfg.display_name:35s} ${cash:>11,.2f}  positions: {len(a.positions)}")
print(f"  ({shown} shown, {len(hb.accounts) - shown} hidden by config)")

# --- Strategies ---
st = reader.read_strategies()

print("\n" + "=" * 60)
print("STRATEGIES")
print("=" * 60)
print(f"  reachable: {st.is_reachable}")
print(f"  stale:     {st.is_stale}")
if st.error:
    print(f"  ERROR:     {st.error}")

if not st.strategies:
    print("  (no strategies reported)")
for s in st.strategies:
    cfg = reg.resolve(s.account)
    acct_label = cfg.alias if cfg else s.account
    flag = "ON " if s.enabled else "off"
    print(f"  [{flag}] {s.name:25s} on {s.instrument:12s} state={s.state:12s} acct={acct_label}")

# --- Executions ---
print("\n" + "=" * 60)
print("EXECUTIONS")
print("=" * 60)
execs = reader.read_new_executions()
print(f"  First call always returns 0 (skips history). Got: {len(execs)}")
print("  Run this script twice with a fill in between to see new executions.")
