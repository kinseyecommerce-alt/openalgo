"""Deployment automation for OpenAlgo's Python Strategy Host.

Registers strategies with the /python host by copying each source file into
strategies/scripts/<strategy_id>.py and merging a host-conformant config
entry into strategies/strategy_configs.json. The host's APScheduler then
auto-starts/stops them at the given IST times on trading days.

Two kinds of registrations exist:
    - The two original production strategies (RSI mean-reversion and Four-EMA
      retracement), deployed by DEFAULT.
    - The 20 variants of the multi-variant intraday engine
      (scripts/variant_intraday_strategy.py). Each variant deploys the SAME
      source file under a different stem (the variant key); the engine
      resolves its active variant from the deployed filename. These are
      NEVER deployed by default -- turning on 20 auto-trading strategies must
      be an explicit choice: pass --strategies all-variants for the 20, a
      comma-separated subset of variant keys, or 'all' for everything.

Usage:
    uv run python strategies/deploy_strategies.py --user <user_id> \
        [--exchange NSE] [--start 09:20] [--stop 15:20] \
        [--days mon,tue,wed,thu,fri] [--strategies <stems>|all-variants|all] \
        [--dry-run] [--force] [--list] [--config-dir <dir>]

Design notes:
    - Idempotent: an existing registration whose file_name starts with the
      same stem prefix is skipped unless --force is given.
    - Never touches entries it does not own (different stems).
    - Refuses to replace an entry whose is_running flag is true.
    - The JSON write mirrors the host's atomic pattern exactly: dump to
      CONFIG_FILE.with_suffix(".json.tmp"), fsync, then os.replace.
    - Pure helpers (build_config / find_existing / merge_registration /
      validators) are separated from all I/O so they are unit-testable
      offline. No network, no Flask imports.
"""

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# The two original production strategies this script owns
# (stem = filename without .py). These are the DEFAULT deployment set.
STRATEGY_STEMS = (
    "rsi_meanreversion_strategy",
    "four_ema_retracement_strategy",
)

# The multi-variant intraday engine: ONE source file deployed under many
# stems. Keys must stay in sync with the VARIANTS registry in
# strategies/scripts/variant_intraday_strategy.py -- the engine resolves its
# active variant from the deployed filename stem.
VARIANT_SOURCE_STEM = "variant_intraday_strategy"
VARIANT_KEYS = (
    "ema_ribbon_trend",
    "vwap_breakout",
    "vwap_reversion",
    "orb_breakout",
    "supertrend_follow",
    "bollinger_squeeze",
    "bollinger_reversion",
    "macd_momentum",
    "donchian_breakout",
    "stochastic_reversal",
    "atr_channel_ride",
    "prev_day_level_fade",
    "momentum_roc",
    "heikin_ashi_trend",
    "volume_spike_breakout",
    "inside_bar_breakout",
    "engulfing_at_ema",
    "pivot_bounce",
    "gap_go",
    "triple_ema_cross",
)

# All registrations as (source_stem, deploy_stem): the file
# scripts/<source_stem>.py is copied to scripts/<deploy_stem>_<ts>.py.
STRATEGIES = tuple((stem, stem) for stem in STRATEGY_STEMS) + tuple(
    (VARIANT_SOURCE_STEM, key) for key in VARIANT_KEYS
)
SOURCE_BY_DEPLOY_STEM = {deploy: source for source, deploy in STRATEGIES}

# Deploying the 20 variants must be an explicit opt-in (--strategies).
DEFAULT_DEPLOY_STEMS = STRATEGY_STEMS

VALID_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# Default locations (repo layout). --config-dir overrides the base dir that
# holds strategy_configs.json and the scripts/ copy target, for testability.
DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent
# Source the canonical strategy files from strategies/examples/ -- that is the
# git-tracked copy present on every fresh clone. strategies/scripts/ is
# gitignored (it holds runtime/user uploads and is empty after a plain clone),
# so sourcing from there would make deploy fail on a production server. We copy
# FROM examples/ INTO scripts/<id>.py (the runtime location the host reads).
DEFAULT_SOURCE_DIR = DEFAULT_CONFIG_DIR / "examples"


# ---------------------------------------------------------------------------
# Pure helpers (no I/O) - unit-tested in test/test_deploy_strategies.py
# ---------------------------------------------------------------------------


def validate_hhmm(value):
    """Validate a 24-hour HH:MM time string.

    Args:
        value: Candidate time string, e.g. "09:20".

    Returns:
        The validated string, unchanged.

    Raises:
        ValueError: If the value is not strictly HH:MM (zero-padded, 00:00
            through 23:59).
    """
    if not isinstance(value, str) or not HHMM_RE.match(value):
        raise ValueError(f"Invalid time {value!r}: expected HH:MM (00:00-23:59)")
    return value


def validate_days(value):
    """Validate a comma-separated day list against mon..sun.

    Args:
        value: Comma-separated day names, e.g. "mon,tue,wed,thu,fri".

    Returns:
        List of lowercase day names in the order given, duplicates removed.

    Raises:
        ValueError: If the list is empty or contains an unknown day name.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Day list must be a non-empty comma-separated string")
    days = []
    for raw in value.split(","):
        day = raw.strip().lower()
        if not day:
            continue
        if day not in VALID_DAYS:
            raise ValueError(f"Invalid day {raw!r}: expected subset of {','.join(VALID_DAYS)}")
        if day not in days:
            days.append(day)
    if not days:
        raise ValueError("Day list must contain at least one day")
    return days


def resolve_strategy_selection(value):
    """Parse the --strategies flag into an ordered list of deploy stems.

    Args:
        value: Raw flag value, or None when the flag was not given. Accepts a
            comma-separated list of deploy stems plus the special tokens
            "all" (every registration) and "all-variants" (the 20
            variant-engine deployments).

    Returns:
        Ordered, de-duplicated list of deploy stems. None maps to the
        default set (the two original production strategies ONLY -- the 20
        variants are always an explicit opt-in).

    Raises:
        ValueError: If the selection is empty or names an unknown stem.
    """
    if value is None:
        return list(DEFAULT_DEPLOY_STEMS)
    if not isinstance(value, str):
        raise ValueError("--strategies must be a comma-separated string of deploy stems")
    tokens = [t.strip() for t in value.split(",") if t.strip()]
    if not tokens:
        raise ValueError("--strategies must name at least one deploy stem")
    selection = []
    for token in tokens:
        key = token.lower()
        if key == "all":
            expansion = [deploy for _, deploy in STRATEGIES]
        elif key == "all-variants":
            expansion = list(VARIANT_KEYS)
        elif key in SOURCE_BY_DEPLOY_STEM:
            expansion = [key]
        else:
            raise ValueError(
                f"Unknown strategy {token!r}: expected 'all', 'all-variants', or one of "
                + ", ".join(deploy for _, deploy in STRATEGIES)
            )
        for stem in expansion:
            if stem not in selection:
                selection.append(stem)
    return selection


def build_config(
    name,
    strategy_id,
    user_id,
    exchange,
    schedule_start,
    schedule_stop,
    schedule_days,
    created_at,
):
    """Build a host-conformant strategy config entry.

    Mirrors the dict written by the /python host's /new route
    (blueprints/python_strategy.py), so the host's scheduler and start/stop
    machinery accept the registration without modification.

    Args:
        name: Display name for the strategy.
        strategy_id: Unique id, "<stem>_<YYYYmmddHHMMSS>".
        user_id: Owner user id (the host resolves the API key from this).
        exchange: Exchange code, e.g. "NSE" (uppercased).
        schedule_start: Daily start time "HH:MM" (IST).
        schedule_stop: Daily stop time "HH:MM" (IST).
        schedule_days: List of lowercase day names, subset of mon..sun.
        created_at: ISO-format IST timestamp string.

    Returns:
        Dict matching the host's config schema.
    """
    return {
        "name": name,
        "file_path": str(Path("strategies") / "scripts" / f"{strategy_id}.py"),
        "file_name": f"{strategy_id}.py",
        "exchange": exchange.upper(),
        "is_running": False,
        "is_scheduled": True,
        "created_at": created_at,
        "user_id": user_id,
        "schedule_start": schedule_start,
        "schedule_stop": schedule_stop,
        "schedule_days": list(schedule_days),
    }


def find_existing(configs, stem):
    """Find an existing registration for a strategy stem.

    Args:
        configs: The {strategy_id: config} mapping.
        stem: Source-file stem, e.g. "rsi_meanreversion_strategy".

    Returns:
        The strategy_id of the first entry whose file_name starts with
        "<stem>_", or None if no such entry exists.
    """
    prefix = f"{stem}_"
    for strategy_id, config in configs.items():
        if str(config.get("file_name", "")).startswith(prefix):
            return strategy_id
    return None


def merge_registration(configs, stem, new_id, config, force=False):
    """Merge a new registration into the config mapping without mutation.

    Preserves all entries not owned by this stem. Never mutates the input
    mapping or any entry inside it.

    Args:
        configs: Existing {strategy_id: config} mapping.
        stem: Source-file stem this registration belongs to.
        new_id: New strategy_id to register.
        config: New config entry (from build_config).
        force: If True, replace an existing entry for the same stem.

    Returns:
        Tuple (new_configs, action) where action is one of "deployed",
        "skipped", or "replaced".

    Raises:
        RuntimeError: If force is True and the existing entry for this stem
            has is_running set to true.
    """
    new_configs = dict(configs)
    existing_id = find_existing(new_configs, stem)
    if existing_id is not None:
        if not force:
            return new_configs, "skipped"
        if new_configs[existing_id].get("is_running"):
            raise RuntimeError(
                f"Refusing to replace {existing_id}: is_running is true. "
                "Stop the strategy in the /python UI before redeploying."
            )
        del new_configs[existing_id]
        new_configs[new_id] = config
        return new_configs, "replaced"
    new_configs[new_id] = config
    return new_configs, "deployed"


# ---------------------------------------------------------------------------
# I/O layer
# ---------------------------------------------------------------------------


def load_configs(config_file):
    """Load the strategy config mapping from disk.

    Args:
        config_file: Path to strategy_configs.json.

    Returns:
        The parsed {strategy_id: config} dict, or {} if the file is absent.
    """
    if not config_file.exists():
        return {}
    with open(config_file, encoding="utf-8") as f:
        return json.load(f)


def save_configs(configs, config_file):
    """Write the config mapping atomically, mirroring the host's pattern.

    Dumps to CONFIG_FILE.with_suffix(".json.tmp"), fsyncs, then os.replace
    so a kill mid-write cannot leave a half-written JSON blob behind.

    Args:
        configs: The {strategy_id: config} mapping to persist.
        config_file: Path to strategy_configs.json.
    """
    config_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = config_file.with_suffix(config_file.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(configs, f, indent=2, default=str, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, config_file)


def list_registrations(config_file):
    """Print current registrations (id, name, exchange, schedule).

    Args:
        config_file: Path to strategy_configs.json.

    Returns:
        Process exit code (0).
    """
    configs = load_configs(config_file)
    if not configs:
        print(f"No strategies registered ({config_file})")
        return 0
    print(f"Registered strategies in {config_file}:")
    for strategy_id, config in configs.items():
        days = ",".join(config.get("schedule_days", []))
        print(
            f"  {strategy_id}\n"
            f"    name: {config.get('name', '')}\n"
            f"    exchange: {config.get('exchange', '')}\n"
            f"    schedule: {config.get('schedule_start', '')}-"
            f"{config.get('schedule_stop', '')} [{days}]\n"
            f"    running: {config.get('is_running', False)}  "
            f"scheduled: {config.get('is_scheduled', False)}"
        )
    return 0


def deploy(
    user_id, exchange, start, stop, days, config_dir, source_dir, dry_run, force, stems=None
):
    """Deploy the selected strategies into the host's registry.

    Args:
        user_id: Owner user id for the registrations.
        exchange: Exchange code, e.g. "NSE".
        start: Daily start time "HH:MM" (IST), pre-validated.
        stop: Daily stop time "HH:MM" (IST), pre-validated.
        days: Validated list of lowercase day names.
        config_dir: Base dir holding strategy_configs.json and scripts/.
        source_dir: Dir holding the strategy source files.
        dry_run: If True, print the plan without writing anything.
        force: If True, redeploy over an existing registration.
        stems: Deploy stems to register (from resolve_strategy_selection).
            None deploys the default set (the two original strategies).

    Returns:
        Process exit code (0 on success, 1 on error).
    """
    if stems is None:
        stems = list(DEFAULT_DEPLOY_STEMS)
    config_file = config_dir / "strategy_configs.json"
    scripts_dir = config_dir / "scripts"
    configs = load_configs(config_file)
    now = datetime.now(IST)
    timestamp = now.strftime("%Y%m%d%H%M%S")
    changed = False

    for stem in stems:
        source_stem = SOURCE_BY_DEPLOY_STEM.get(stem)
        if source_stem is None:
            print(f"error: unknown deploy stem: {stem}", file=sys.stderr)
            return 1
        source = source_dir / f"{source_stem}.py"
        if not source.is_file():
            print(f"error: source file not found: {source}", file=sys.stderr)
            return 1

        existing_id = find_existing(configs, stem)
        new_id = f"{stem}_{timestamp}"
        config = build_config(
            name=stem,
            strategy_id=new_id,
            user_id=user_id,
            exchange=exchange,
            schedule_start=start,
            schedule_stop=stop,
            schedule_days=days,
            created_at=now.isoformat(),
        )

        try:
            configs, action = merge_registration(configs, stem, new_id, config, force=force)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        if action == "skipped":
            print(f"{stem}: already deployed as {existing_id} (use --force to redeploy)")
            continue

        target = scripts_dir / f"{new_id}.py"
        if dry_run:
            if action == "replaced":
                print(f"{stem}: [dry-run] would remove {existing_id} and its file")
            print(f"{stem}: [dry-run] would copy {source} -> {target}")
            print(f"{stem}: [dry-run] would register {new_id} ({action})")
            continue

        scripts_dir.mkdir(parents=True, exist_ok=True)
        if action == "replaced":
            old_file = scripts_dir / f"{existing_id}.py"
            if old_file.is_file():
                old_file.unlink()
                print(f"{stem}: removed old file {old_file}")
        shutil.copy2(source, target)
        try:
            os.chmod(target, 0o755)
        except OSError:
            pass
        changed = True
        print(f"{stem}: {action} as {new_id}")

    if changed and not dry_run:
        save_configs(configs, config_file)
        print(f"Wrote {config_file}")
    elif dry_run:
        print("[dry-run] no files written")
    else:
        print("No changes")
    return 0


def main(argv=None):
    """CLI entry point.

    Args:
        argv: Argument list (defaults to sys.argv[1:]).

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(
        description="Deploy the production strategies to the Python Strategy Host."
    )
    parser.add_argument("--user", help="Owner user id (required for deploy)")
    parser.add_argument("--exchange", default="NSE", help="Exchange code (default: NSE)")
    parser.add_argument("--start", default="09:20", help="Schedule start HH:MM IST")
    parser.add_argument("--stop", default="15:20", help="Schedule stop HH:MM IST")
    parser.add_argument(
        "--days",
        default="mon,tue,wed,thu,fri",
        help="Comma-separated schedule days (subset of mon..sun)",
    )
    parser.add_argument(
        "--strategies",
        default=None,
        help=(
            "Comma-separated deploy stems to deploy. Default: the two original "
            "production strategies only. Special values: 'all-variants' deploys "
            "the 20 variant-engine strategies, 'all' deploys everything. "
            "Deploying the 20 variants is always an explicit opt-in."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Print plan without writing")
    parser.add_argument("--force", action="store_true", help="Redeploy over existing entries")
    parser.add_argument("--list", action="store_true", help="List registrations and exit")
    parser.add_argument(
        "--config-dir",
        default=str(DEFAULT_CONFIG_DIR),
        help="Base dir holding strategy_configs.json and scripts/ (for testing)",
    )
    args = parser.parse_args(argv)

    config_dir = Path(args.config_dir)

    if args.list:
        return list_registrations(config_dir / "strategy_configs.json")

    if not args.user:
        parser.error("--user is required for deploy (the host resolves the API key from it)")

    try:
        start = validate_hhmm(args.start)
        stop = validate_hhmm(args.stop)
        days = validate_days(args.days)
        stems = resolve_strategy_selection(args.strategies)
    except ValueError as exc:
        parser.error(str(exc))

    return deploy(
        user_id=args.user,
        exchange=args.exchange,
        start=start,
        stop=stop,
        days=days,
        config_dir=config_dir,
        source_dir=DEFAULT_SOURCE_DIR,
        dry_run=args.dry_run,
        force=args.force,
        stems=stems,
    )


if __name__ == "__main__":
    sys.exit(main())
