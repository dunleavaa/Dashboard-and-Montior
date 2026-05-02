"""
Smoke-test the Discord poster end-to-end using current data from the share.

Usage:
    python test_discord.py            # uses Z:/ as share
    python test_discord.py /mnt/nt    # custom share path

What it does:
    1. Loads Discord config and account aliases
    2. Reads current heartbeat + strategies from the share
    3. Posts/edits the pinned status message in #nt-status
    4. Posts a fake test fill to #nt-fills
    5. Posts a fake test alert to #nt-alerts
    6. Posts the recovery for that alert

If everything works, you'll see all three messages in your Discord channels.
"""

import logging
import sys
from datetime import datetime, timezone

from accounts_config import AccountsRegistry
from discord_poster import DiscordConfig, DiscordPoster
from nt_reader import Execution, NTReader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

share_path = sys.argv[1] if len(sys.argv) > 1 else "Z:/"

print(f"Reading from: {share_path}")
print("Loading config...")

reg = AccountsRegistry.load("accounts.yaml")
cfg = DiscordConfig.load("discord_config.yaml")
poster = DiscordPoster(cfg, reg)
reader = NTReader(share_path)

# --- 1. Status message ---
print("\n[1/4] Reading current NT status...")
hb = reader.read_heartbeat()
st = reader.read_strategies()
print(f"      heartbeat: reachable={hb.is_reachable} stale={hb.is_stale} accounts={len(hb.accounts)}")
print(f"      strategies: {len(st.strategies)}")

print("\n[2/4] Updating pinned status message in #nt-status...")
poster.update_status_message(hb, st)
print("      -> check Discord. Should see a pinned embed in #nt-status.")

# --- 2. Fake fill ---
print("\n[3/4] Posting a fake test fill to #nt-fills...")
fake_fill = Execution(
    timestamp_utc=datetime.now(timezone.utc),
    exec_time=datetime.now(timezone.utc),
    account="Sim101",
    instrument="NQ 06-26",
    action="Long",
    qty=1,
    price=21345.50,
    order_id="test-order-id-12345678",
    exec_id="test-exec-id-87654321",
    strategy="TEST_FILL",
    commission=2.50,
)
poster.post_fill(fake_fill)
print("      -> check #nt-fills for a green LONG embed.")

# --- 3. Fake alert + recovery ---
print("\n[4/4] Posting a test alert to #nt-alerts, then recovery...")
poster.post_alert(
    key="test_alert",
    title="Test alert",
    description="This is a test of the alert system. If you see this, alerts work.",
    severity="warning",
)
print("      -> check #nt-alerts for a yellow alert.")

import time
time.sleep(2)

poster.post_recovery(
    key="test_alert",
    title="Test alert",
    description="Recovery confirmation also works.",
)
print("      -> check #nt-alerts for the green recovery embed.")

print("\nAll done. If Discord shows three messages (status pinned, fill, alert+recovery), the poster is working.")
