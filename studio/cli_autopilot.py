"""CLI commands for the autopilot (registered from main.py)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

AUTOPILOT_COMMANDS = {"auto-check", "auto-run", "auto-status", "auto-approve-script", "schedule-show",
                      "schedule-install"}


def register(commands: argparse._SubParsersAction) -> None:
    def config_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", default="config/autopilot.json")

    p = commands.add_parser("auto-check", help="No-cost readiness check for live autopilot runs")
    config_arg(p)
    p = commands.add_parser("auto-run", help="Autonomous topic->long-form+Short pipeline (paid unless --dry-run)")
    config_arg(p)
    p.add_argument("--dry-run", action="store_true", help="Offline fakes; no network, no cost")
    p.add_argument("--preview", action="store_true", help="With --dry-run: small resolutions for speed")
    p.add_argument("--videos", type=int, help="New videos to start this run (default: config videos_per_run)")
    p.add_argument("--confirm-spend", action="store_true",
                   help="Consent to paid API calls within config budgets (not a dollar cap itself)")
    p = commands.add_parser("auto-status", help="List autopilot videos, statuses and costs")
    p.add_argument("--dry-run", action="store_true")
    p = commands.add_parser("auto-approve-script", help="Human approval of a script before paid media")
    p.add_argument("--video", required=True)
    p.add_argument("--confirm-reviewed", action="store_true")
    p = commands.add_parser("schedule-show", help="Show the recommended OS scheduler setup")
    config_arg(p)
    p = commands.add_parser("schedule-install", help="Write the OS scheduler definition (unattended paid runs)")
    config_arg(p)
    p.add_argument("--confirm-unattended-spend", action="store_true")


def _paths(root: Path, dry_run: bool) -> tuple[Path, Path]:
    if dry_run:
        return root / "outputs" / "autopilot-dryrun", root / "state" / "dryrun"
    return root / "outputs" / "autopilot", root / "state"


def _load_config(root: Path, path: str, allow_example: bool = False):
    from studio.autopilot import AutopilotConfig
    cfg = root / path
    if not cfg.is_file() and allow_example:
        cfg = root / "config" / "autopilot.example.json"
    return AutopilotConfig.load(cfg)


def dispatch(root: Path, args: argparse.Namespace) -> None:
    from studio.providers import load_catalog
    catalog = load_catalog(root)

    if args.command == "auto-check":
        from studio.services import preflight
        report = preflight(_load_config(root, args.config, allow_example=True), catalog)
        if not (root / args.config).is_file():
            report["problems"].append(f"{args.config} missing (checked the example config instead)")
            report["ready"] = False
        print(json.dumps(report, indent=2))
        if not report["ready"]:
            sys.exit(2)
        return

    if args.command == "auto-status":
        from studio.autopilot import status_table
        outputs, state = _paths(root, args.dry_run)
        print(json.dumps(status_table(outputs, state), indent=2))
        return

    if args.command == "auto-approve-script":
        from studio.autopilot import approve_script
        digest = approve_script(Path(args.video).expanduser().resolve(), args.confirm_reviewed)
        print(f"Script approved (fingerprint {digest[:12]}). The next auto-run continues with paid media.")
        return

    if args.command in ("schedule-show", "schedule-install"):
        from studio import scheduler
        config = _load_config(root, args.config)
        if args.command == "schedule-show":
            print(scheduler.describe(root, config.schedule))
        else:
            print(scheduler.install(root, config.schedule, args.confirm_unattended_spend))
        return

    if args.command == "auto-run":
        from studio.autopilot import Autopilot, setup_logging
        from studio.models import LONG_FORM, SHORT_FORM
        dry = args.dry_run
        if not dry and not args.confirm_spend:
            raise ValueError("auto-run makes paid API calls. Review config/autopilot.json budgets and pass "
                             "--confirm-spend, or use --dry-run for an offline rehearsal.")
        if args.preview and not dry:
            raise ValueError("--preview is only for --dry-run")
        config = _load_config(root, args.config, allow_example=dry)
        outputs, state = _paths(root, dry)
        log_path = setup_logging(root / "logs")
        if dry:
            import shutil
            from studio.services import build_dryrun_services
            if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
                print("WARNING: ffmpeg/ffprobe not found; the dry run will stop at the narration stage.",
                      file=sys.stderr)
            services = build_dryrun_services()
        else:
            from studio.services import build_live_services
            services = build_live_services(config, catalog)
        profiles = (LONG_FORM.scaled(3), SHORT_FORM.scaled(3)) if args.preview else (LONG_FORM, SHORT_FORM)
        pilot = Autopilot(root, config, services, outputs=outputs, state_dir=state, catalog=catalog,
                          profiles=profiles)
        pilot.install_signal_handlers()
        results = pilot.run(max_new=args.videos)
        print(json.dumps({"log": str(log_path), "results": results}, indent=2))
        if pilot.should_stop():
            sys.exit(130)
