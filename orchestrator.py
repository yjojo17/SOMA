"""
Session Orchestrator
Manages daily collection runs across all probe profiles.

Behaviour:
- Runs each profile TWICE per day within the collection window (09:00–23:00)
- First pass: all profiles run once in shuffled order
- Second pass: same shuffled order, only after MIN_RERUN_GAP_HOURS since first run
- A random gap between consecutive profile runs mimics organic usage patterns
- State persists in orchestrator_state.json so restarts are safe
"""

import json
import logging
import random
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from configurable_collector import ConfigurableNetworkCollector

# ── Constants ─────────────────────────────────────────────────────────────────

CONFIG_FILE = "research_config_copy.yaml"
STATE_FILE = "orchestrator_state.json"
LOG_FILE = "logs/orchestrator.log"

WINDOW_START_HOUR = 9    # earliest a session may begin
WINDOW_END_HOUR = 23     # latest a session may begin (hard cutoff)

RUNS_PER_DAY = 1         # how many sessions each profile gets per day
MIN_RERUN_GAP_HOURS = 3  # minimum hours between run 1 and run 2 for the same profile

# Random gap between consecutive profile runs (seconds)
GAP_MIN = 5 * 60    #  5 minutes
GAP_MAX = 15 * 60   # 15 minutes

# ── Logging ───────────────────────────────────────────────────────────────────

Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("orchestrator")

# ── State helpers ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _runs_today(state: dict, profile_id: str) -> list:
    """Return list of run records for profile_id completed today."""
    today = date.today().isoformat()
    runs = state.get(profile_id, {}).get("runs", [])
    return [r for r in runs if r.get("date") == today]


def profiles_done_today(state: dict, all_ids: list) -> set:
    """Profiles that have completed RUNS_PER_DAY runs today."""
    return {pid for pid in all_ids if len(_runs_today(state, pid)) >= RUNS_PER_DAY}


def profiles_due(state: dict, all_ids: list, day_order: list) -> list:
    """
    Return profiles that still need a run today, in the right order.
    First-pass profiles (0 runs today) come before second-pass profiles (1 run today).
    Second-pass profiles are only included if MIN_RERUN_GAP_HOURS has elapsed.
    Within each pass, preserves day_order (the shuffled order for today).
    """
    today = date.today().isoformat()
    first_pass = []
    second_pass = []

    for pid in day_order:
        runs = _runs_today(state, pid)
        count = len(runs)
        if count == 0:
            first_pass.append(pid)
        elif count < RUNS_PER_DAY:
            # Only eligible for second pass if enough time has passed
            last_run_time = datetime.fromisoformat(runs[-1]["time"])
            gap = (datetime.now() - last_run_time).total_seconds() / 3600
            if gap >= MIN_RERUN_GAP_HOURS:
                second_pass.append(pid)
            else:
                wait_h = MIN_RERUN_GAP_HOURS - gap
                log.debug(f"{pid}: second run in {wait_h:.1f}h")

    return first_pass + second_pass


def mark_done(state: dict, profile_id: str, success: bool, posts: int) -> None:
    if profile_id not in state:
        state[profile_id] = {"runs": []}
    if "runs" not in state[profile_id]:
        state[profile_id]["runs"] = []

    state[profile_id]["runs"].append({
        "date": date.today().isoformat(),
        "time": datetime.now().isoformat(),
        "success": success,
        "posts_collected": posts,
    })
    save_state(state)

# ── Scheduling helpers ────────────────────────────────────────────────────────

def inside_window() -> bool:
    h = datetime.now().hour
    return WINDOW_START_HOUR <= h < WINDOW_END_HOUR


def seconds_until_window() -> float:
    now = datetime.now()
    target = now.replace(hour=WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def window_closes_in() -> float:
    now = datetime.now()
    close = now.replace(hour=WINDOW_END_HOUR, minute=0, second=0, microsecond=0)
    return max(0.0, (close - now).total_seconds())

# ── Collection runner ─────────────────────────────────────────────────────────

def run_profile(profile_id: str, target_posts: int) -> tuple[bool, int]:
    collector = None
    try:
        collector = ConfigurableNetworkCollector(
            profile_id=profile_id,
            config_file=CONFIG_FILE,
            use_virtual_display=True,
        )
        collector.initialize_browser()
        feed_data = collector.collect_feed(target_posts=target_posts)

        if feed_data:
            return True, len(feed_data)
        return False, 0

    except Exception as exc:
        log.error(f"{profile_id}: unhandled error — {exc}", exc_info=True)
        return False, 0

    finally:
        if collector:
            collector.cleanup()

# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    cfg = yaml.safe_load(open(CONFIG_FILE))
    target_posts = cfg["collection_settings"]["posts_per_session"]
    all_profile_ids = [p["id"] for p in cfg["research_profiles"]]

    log.info(f"Orchestrator started — {len(all_profile_ids)} profiles, {RUNS_PER_DAY} runs/day")
    log.info(f"Collection window: {WINDOW_START_HOUR:02d}:00 – {WINDOW_END_HOUR:02d}:00")
    log.info(f"Min gap between runs: {MIN_RERUN_GAP_HOURS}h")

    # Shuffled order is fixed for the whole day, regenerated at midnight
    current_day = date.today().isoformat()
    day_order = all_profile_ids[:]
    random.shuffle(day_order)

    while True:
        # Regenerate shuffle at the start of a new day
        today = date.today().isoformat()
        if today != current_day:
            current_day = today
            day_order = all_profile_ids[:]
            random.shuffle(day_order)
            log.info(f"New day — shuffled run order: {day_order}")

        # Wait for the collection window
        if not inside_window():
            wait = seconds_until_window()
            log.info(f"Outside window. Sleeping {wait/3600:.1f}h until {WINDOW_START_HOUR:02d}:00.")
            time.sleep(wait)
            continue

        # Determine what still needs to run
        state = load_state()

        if not profiles_due(state, all_profile_ids, day_order):
            done = profiles_done_today(state, all_profile_ids)
            if len(done) >= len(all_profile_ids):
                wait = seconds_until_window()
                log.info(f"All {len(all_profile_ids)} profiles completed {RUNS_PER_DAY} runs. Sleeping until tomorrow.")
                time.sleep(wait)
            else:
                # Some profiles waiting for MIN_RERUN_GAP — check again in a few minutes
                log.info("No profiles due yet (waiting for rerun gap). Checking again in 10 min.")
                time.sleep(10 * 60)
            continue

        due = profiles_due(state, all_profile_ids, day_order)
        run_counts = {pid: len(_runs_today(state, pid)) for pid in due}
        log.info(f"Profiles due: {[(pid, f'run {run_counts[pid]+1}/{RUNS_PER_DAY}') for pid in due]}")

        for profile_id in due:
            if not inside_window():
                log.warning(f"Window closed before {profile_id}. Will retry tomorrow.")
                break

            if window_closes_in() < 120:
                log.warning("Window closing in <2 min. Stopping for today.")
                break

            run_num = len(_runs_today(load_state(), profile_id)) + 1
            log.info(f"▶  {profile_id} — run {run_num}/{RUNS_PER_DAY}")
            success, posts = run_profile(profile_id, target_posts)
            state = load_state()
            mark_done(state, profile_id, success, posts)

            status = f"✓ {posts} posts" if success else "✗ failed"
            log.info(f"   {profile_id}: {status}")

            # Gap before next profile
            state = load_state()
            remaining = profiles_due(state, all_profile_ids, day_order)
            if remaining and inside_window():
                gap = random.uniform(GAP_MIN, GAP_MAX)
                log.info(f"   Gap: {gap/60:.1f} min")
                time.sleep(gap)

        time.sleep(60)


if __name__ == "__main__":
    main()