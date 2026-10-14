#!/usr/bin/env python3
"""ProjectManager scheduled duties — a Hermes no-agent cron script (no LLM turn
for scheduling; the only model call is the optional weekly-brief note).

Every run executes ``python -m plugins.project_manager.jobs tick``:
  1. approval executor (approved changes -> ClickUp / meetings table; expiry);
  2. meeting reminders, only inside the day-before slot (pm.yaml);
  3. weekly brief, only inside its slot (pm.yaml).
Schedules are decided inside the job in America/New_York, so the cron
expression only sets how often we look. Dedupe keys in pm.message_log make
repeated runs inside a slot no-ops, and a file lock skips a run while the
previous one is still going.

Install (human-gated; see VIDUOPS/vps-scripts/cutover-project-manager.sh):
    cp scripts/pm_tick.py /home/bmc/.hermes/scripts/pm_tick.py
    docker exec hermes hermes cron create --no-agent --script pm_tick.py \
        --deliver local --name "PM tick (approvals, reminders, weekly brief)" "* * * * *"

Prints only when something happened; exits non-zero if any part failed.
"""

from __future__ import annotations

import sys
from pathlib import Path

for candidate in (Path("/opt/hermes"), Path(__file__).resolve().parent.parent):
    if (candidate / "plugins" / "project_manager").is_dir():
        sys.path.insert(0, str(candidate))
        break

from plugins.project_manager.jobs import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["tick", *sys.argv[1:]]))
