"""
Session Orchestrator
Manages daily collection runs across all profiles.

Behaviour:
- Matched follow-set groups are the scheduling unit. A group is parsed from the id:
  study accounts are named U_<persona+condition>_<wave>_<bucket...>, and the matched
  group is <wave>_<bucket> (e.g. U_MI_W1_NewsCR / U_MN_W1_NewsCR / U_FI2_W1_NewsCR ->
  'W1_NewsCR'). Probes (non-U_ ids) are singleton groups.
- Each group runs WEEKDAY_RUNS / WEEKEND_RUNS sessions per active day within the
  collection window (09:00–23:00), with a ~DAY_OFF_PROB chance the whole group rests
  that day — so matched accounts share off-days and run counts ("if one's off, all off").
- Group members run back-to-back (short intra-group gap); longer gaps between groups.
- First pass: every due profile runs once in the day's order; second pass repeats after
  MIN_RERUN_GAP_HOURS.
- State persists in orchestrator_state.json so restarts are safe; day targets are
  (group, date)-seeded so a restart reproduces the same schedule.
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

#CONFIG_FILE = "research_Warmup_config.yaml"
CONFIG_FILE = "research_W2_config.yaml"
STATE_FILE = "orchestrator_state.json"
LOG_FILE = "logs/orchestrator.log"

WINDOW_START_HOUR = 9    # earliest a session may begin
WINDOW_END_HOUR = 23     # latest a session may begin (hard cutoff)

# Per-day session target is decided per FOLLOW-SET GROUP (not per profile) so matched
# accounts share the same active days and the same number of runs.
WEEKDAY_RUNS = 2
WEEKEND_RUNS = 3
DAY_OFF_PROB = 0.13      # ~13% zero-session days [CSV]; the whole group rests together

MIN_RERUN_GAP_HOURS = 3  # minimum hours between run 1 and run 2 for the same profile

# Gaps between consecutive profile runs (seconds)
INTRA_GROUP_GAP_MIN = 60     #  1 min — matched accounts run shortly after one another
INTRA_GROUP_GAP_MAX = 180    #  3 min
GAP_MIN = 10 * 60            # 10 min — between different groups
GAP_MAX = 30 * 60            # 30 min

#To manual trigger collection to overwrite an off-day
RUN_OVERRIDES: dict[tuple[str, str], int] = {
    ("W2_Mano", "2026-08-09"): 2,   
}

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

# ── Grouping (matched follow-set triplets) ─────────────────────────────────────

def group_key(profile_id: str) -> str:
    """Matched-group key parsed from the id. Study accounts are named
    U_<persona+condition>_<wave>_<bucket...>; the matched group is <wave>_<bucket>
    (e.g. U_MI_W1_NewsCR, U_MN_W1_NewsCR, U_FI2_W1_NewsCR -> 'W1_NewsCR'). The
    persona/condition token is what differs within a group. Anything not matching that
    shape (probes) is its own singleton."""
    parts = profile_id.split("_")
    if parts[0] == "U" and len(parts) >= 4 or parts[0] == "C" and len(parts) >= 4:
        return "_".join(parts[2:])          # drop 'U' and the persona token
    return f"solo:{profile_id}"


def build_groups(profile_ids: list) -> dict:
    """group_key -> [profile_ids] in the matched group."""
    groups: dict = {}
    for pid in profile_ids:
        groups.setdefault(group_key(pid), []).append(pid)
    return groups


def runs_target(group_key_: str, day: date) -> int:
    """Sessions for a whole group on a day. Seeded by (group, date): every member gets
    the same answer, days off are staggered across groups, and a mid-day restart
    reproduces the same target."""
    override = RUN_OVERRIDES.get((group_key_, day.isoformat()))
    if override is not None:
        return override
    rng = random.Random(f"{group_key_}:{day.isoformat()}")
    if rng.random() < DAY_OFF_PROB:
        return 0
    return WEEKEND_RUNS if day.weekday() >= 5 else WEEKDAY_RUNS

def build_day_order(groups: dict) -> list:
    """Run order with group members kept adjacent (a triplet runs back-to-back),
    groups shuffled, members shuffled within their group."""
    keys = list(groups.keys())
    random.shuffle(keys)
    order = []
    for k in keys:
        members = groups[k][:]
        random.shuffle(members)
        order.extend(members)
    return order

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


def profiles_done_today(state: dict, all_ids: list, group_of: dict) -> set:
    """Profiles that have completed their group's session target today (0 on a day off)."""
    today = date.today()
    return {pid for pid in all_ids
            if len(_runs_today(state, pid)) >= runs_target(group_of[pid], today)}


def profiles_due(state: dict, day_order: list, group_of: dict) -> list:
    """
    Return profiles that still need a run today, in day_order (group members adjacent).
    First-pass profiles (0 runs today) come before second-pass profiles (>=1 run today).
    Second-pass profiles are only included if MIN_RERUN_GAP_HOURS has elapsed.
    """
    today_d = date.today()
    first_pass = []
    second_pass = []

    for pid in day_order:
        target = runs_target(group_of[pid], today_d)
        runs = _runs_today(state, pid)
        count = len(runs)
        if count == 0 and target >= 1:
            first_pass.append(pid)
        elif 0 < count < target:
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

    groups = build_groups(all_profile_ids)
    group_of = {pid: k for k, members in groups.items() for pid in members}

    log.info(f"Orchestrator started — {len(all_profile_ids)} profiles in {len(groups)} follow-set groups")
    log.info(f"Runs/day per group: weekday {WEEKDAY_RUNS}, weekend {WEEKEND_RUNS}, {DAY_OFF_PROB:.0%} off-days")
    log.info(f"Collection window: {WINDOW_START_HOUR:02d}:00 – {WINDOW_END_HOUR:02d}:00")
    log.info(f"Min gap between same-profile runs: {MIN_RERUN_GAP_HOURS}h")
    for k, members in groups.items():
        if len(members) > 1:
            log.info(f"  group {k}: {members}")

    # Run order fixed for the whole day (group members adjacent), regenerated at midnight
    current_day = date.today().isoformat()
    day_order = build_day_order(groups)

    while True:
        # Regenerate order at the start of a new day
        today = date.today().isoformat()
        if today != current_day:
            current_day = today
            day_order = build_day_order(groups)
            log.info(f"New day — run order: {day_order}")

        # Wait for the collection window
        if not inside_window():
            wait = seconds_until_window()
            log.info(f"Outside window. Sleeping {wait/3600:.1f}h until {WINDOW_START_HOUR:02d}:00.")
            time.sleep(wait)
            continue

        # Determine what still needs to run
        state = load_state()

        if not profiles_due(state, day_order, group_of):
            done = profiles_done_today(state, all_profile_ids, group_of)
            if len(done) >= len(all_profile_ids):
                wait = seconds_until_window()
                log.info("All groups done for today. Sleeping until tomorrow.")
                time.sleep(wait)
            else:
                # Some profiles waiting for MIN_RERUN_GAP — check again in a few minutes
                log.info("No profiles due yet (waiting for rerun gap). Checking again in 10 min.")
                time.sleep(10 * 60)
            continue

        due = profiles_due(state, day_order, group_of)
        run_counts = {pid: len(_runs_today(state, pid)) for pid in due}
        log.info(f"Profiles due: {[(pid, f'run {run_counts[pid]+1}/{runs_target(group_of[pid], date.today())}') for pid in due]}")

        for profile_id in due:
            if not inside_window():
                log.warning(f"Window closed before {profile_id}. Will retry tomorrow.")
                break

            if window_closes_in() < 120:
                log.warning("Window closing in <2 min. Stopping for today.")
                break

            run_num = len(_runs_today(load_state(), profile_id)) + 1
            log.info(f"▶  {profile_id} — run {run_num}/{runs_target(group_of[profile_id], date.today())}")
            success, posts = run_profile(profile_id, target_posts)
            state = load_state()
            mark_done(state, profile_id, success, posts)

            status = f"✓ {posts} posts" if success else "✗ failed"
            log.info(f"   {profile_id}: {status}")

            # Gap before next profile — short if the next one shares this follow set
            state = load_state()
            remaining = profiles_due(state, day_order, group_of)
            if remaining and inside_window():
                if group_of[remaining[0]] == group_of[profile_id]:
                    gap = random.uniform(INTRA_GROUP_GAP_MIN, INTRA_GROUP_GAP_MAX)
                    log.info(f"   Same group — short gap: {gap/60:.1f} min")
                else:
                    gap = random.uniform(GAP_MIN, GAP_MAX)
                    log.info(f"   Next group — gap: {gap/60:.1f} min")
                time.sleep(gap)

        time.sleep(60)


if __name__ == "__main__":
    main()