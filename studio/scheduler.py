"""OS-native background scheduling for `python main.py auto-run`.

macOS -> launchd LaunchAgent; Windows -> Task Scheduler (schtasks); Linux -> systemd user timer.
Installing a schedule is the human's explicit, durable authorization for unattended paid runs
within the configured budgets, so it requires --confirm-unattended-spend.
"""
from __future__ import annotations

import os
from pathlib import Path
import platform
import plistlib
import shutil
import sys

from studio.autopilot import ScheduleConfig

LABEL = "com.talenovi.autopilot"


def detect_os() -> str:
    system = platform.system()
    return {"Darwin": "macos", "Windows": "windows", "Linux": "linux"}.get(system, system.lower())


def run_command(root: Path, python: str | None = None) -> list[str]:
    return [python or sys.executable, str(root / "main.py"), "auto-run", "--confirm-spend"]


def tool_path() -> str:
    """launchd/systemd start with a minimal PATH; include ffmpeg's directory explicitly."""
    dirs = []
    ff = shutil.which("ffmpeg")
    if ff:
        dirs.append(str(Path(ff).parent))
    dirs += ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    return ":".join(dict.fromkeys(dirs))


def launchd_plist(root: Path, sched: ScheduleConfig, python: str | None = None) -> bytes:
    logs = root / "logs"
    plist: dict = {
        "Label": LABEL,
        "ProgramArguments": run_command(root, python),
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {"PATH": tool_path(), "PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(logs / "launchd.out.log"),
        "StandardErrorPath": str(logs / "launchd.err.log"),
        "ProcessType": "Background",
        "RunAtLoad": False,
    }
    if sched.every_n_days == 1:
        plist["StartCalendarInterval"] = {"Hour": sched.hour, "Minute": sched.minute}
    else:
        plist["StartInterval"] = sched.every_n_days * 86_400
    return plistlib.dumps(plist)


def systemd_units(root: Path, sched: ScheduleConfig, python: str | None = None) -> dict[str, str]:
    cmd = " ".join(f'"{c}"' for c in run_command(root, python))
    on_calendar = (f"*-*-* {sched.hour:02}:{sched.minute:02}:00" if sched.every_n_days == 1
                   else f"*-*-1/{sched.every_n_days} {sched.hour:02}:{sched.minute:02}:00")
    return {
        "talenovi-autopilot.service": (
            "[Unit]\nDescription=TaleNovi autopilot video run\n\n[Service]\nType=oneshot\n"
            f"WorkingDirectory={root}\nEnvironment=PATH={tool_path()}\nExecStart={cmd}\n"
            "KillSignal=SIGTERM\nTimeoutStopSec=120\n"),
        "talenovi-autopilot.timer": (
            "[Unit]\nDescription=Schedule TaleNovi autopilot\n\n[Timer]\n"
            f"OnCalendar={on_calendar}\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n"),
    }


def windows_command(root: Path, sched: ScheduleConfig, python: str | None = None) -> list[str]:
    cmd = " ".join(f'"{c}"' for c in run_command(root, python))
    return ["schtasks", "/Create", "/TN", "TaleNoviAutopilot", "/TR", cmd, "/SC", "DAILY",
            "/MO", str(sched.every_n_days), "/ST", f"{sched.hour:02}:{sched.minute:02}", "/F"]


def describe(root: Path, sched: ScheduleConfig) -> str:
    os_name = detect_os()
    if os_name == "macos":
        target = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        return (f"Recommended scheduler: launchd (macOS)\nPlist: {target}\n\n"
                + launchd_plist(root, sched).decode()
                + f"\nInstall: python main.py schedule-install --confirm-unattended-spend\n"
                  f"Then load: launchctl bootstrap gui/{os.getuid()} {target}\n"
                  f"Stop:      launchctl bootout gui/{os.getuid()} {target}\n"
                  "Note: macOS runs missed StartCalendarInterval jobs after wake, but not while powered off.")
    if os_name == "windows":
        return ("Recommended scheduler: Windows Task Scheduler\nRun in PowerShell:\n  "
                + " ".join(windows_command(root, sched)) + "\nRemove: schtasks /Delete /TN TaleNoviAutopilot /F")
    units = systemd_units(root, sched)
    return ("Recommended scheduler: systemd user timer (Linux)\nPlace in ~/.config/systemd/user/:\n\n"
            + "\n".join(f"# {name}\n{body}" for name, body in units.items())
            + "\nEnable: systemctl --user daemon-reload && systemctl --user enable --now talenovi-autopilot.timer\n"
              "Without systemd, cron: "
              f"{sched.minute} {sched.hour} */{sched.every_n_days} * * cd {root} && "
              + " ".join(run_command(root)))


def install(root: Path, sched: ScheduleConfig, confirm_unattended_spend: bool) -> str:
    """Write the scheduler definition. Loading it is a separate, printed command."""
    if not confirm_unattended_spend:
        raise ValueError("Scheduling enables unattended PAID runs within config budgets. Review "
                         "config/autopilot.json budgets, then pass --confirm-unattended-spend.")
    os_name = detect_os()
    (root / "logs").mkdir(exist_ok=True)
    if os_name == "macos":
        target = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(launchd_plist(root, sched))
        return (f"Wrote {target}\nLoad it now with:\n  launchctl bootstrap gui/{os.getuid()} {target}\n"
                f"Unload with:\n  launchctl bootout gui/{os.getuid()} {target}")
    if os_name == "linux":
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        for name, body in systemd_units(root, sched).items():
            (unit_dir / name).write_text(body, encoding="utf-8")
        return ("Wrote systemd units. Enable with:\n  systemctl --user daemon-reload && "
                "systemctl --user enable --now talenovi-autopilot.timer")
    return "On Windows run this command yourself:\n  " + " ".join(windows_command(root, sched))
