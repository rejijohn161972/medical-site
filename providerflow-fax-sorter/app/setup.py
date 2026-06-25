"""First-time setup / reset for the ProviderFlow Fax Sorter.

Prompts for the ProviderFlow login, OpenAI API key, model and output folder;
installs the Playwright Chromium browser; optionally tests the login; and
schedules the automatic Monday-Friday 8:45 AM run.

No secret is hard-coded. Everything entered here is stored locally (git-ignored)
under %LOCALAPPDATA%\\ProviderFlowFaxSorter and in a local .env file.
"""
from __future__ import annotations

import getpass
import os
import subprocess
import sys
from pathlib import Path

from config_store import (
    DEFAULT_BASE_URL, DEFAULT_OUTPUT_DIR, DEFAULT_OPENAI_MODEL,
    load_config, save_local_config, APPDATA_DIR,
    embedded_openai_api_key, _key_file_value,
)


def desktop_path() -> Path:
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"


def write_desktop_button(name: str, target_bat: Path) -> None:
    d = desktop_path()
    if not d.exists():
        return
    (d / name).write_text(f'@echo off\r\ncall "{target_bat}"\r\n', encoding="utf-8")


def install_playwright_browser() -> bool:
    print("Installing the Chromium browser engine (one time, may take a minute)...")
    try:
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
        return True
    except Exception as e:
        print("Could not install the Chromium engine automatically.")
        print("Run this once in a terminal:  python -m playwright install chromium")
        print("Details:", e)
        return False


TASK_NAME = "ProviderFlow Fax Sorter"
LOGON_TASK_NAME = "ProviderFlow Fax Sorter Logon"


def _run(cmd: list[str]):
    return subprocess.run(cmd, capture_output=True, text=True)


def _enable_catch_up(task_name: str) -> None:
    """Best-effort: make a run missed while asleep/off start as soon as possible.

    schtasks can't set 'Start when available', so use PowerShell. If this fails,
    the basic hourly + logon triggers still work, so it's non-fatal.
    """
    ps = (
        "$ErrorActionPreference='Stop';"
        "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable "
        "-MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2);"
        "$s.DisallowStartIfOnBatteries=$false; $s.StopIfGoingOnBatteries=$false;"
        f"Set-ScheduledTask -TaskName '{task_name}' -Settings $s | Out-Null"
    )
    try:
        _run(["powershell", "-NoProfile", "-Command", ps])
    except Exception:
        pass


def create_task(root_dir: Path) -> bool:
    vbs = root_dir / "app" / "run_hidden.vbs"
    runner = f'wscript.exe "{vbs}"'
    # Remove earlier task versions so schedules don't stack up.
    for old in (TASK_NAME, LOGON_TASK_NAME, "ProviderFlow Fax Sorter 845AM"):
        _run(["schtasks", "/Delete", "/TN", old, "/F"])

    # 1) Hourly: every :45 from 08:45 through 16:45 (4:45 PM), Monday-Friday.
    hourly = _run([
        "schtasks", "/Create", "/TN", TASK_NAME, "/SC", "WEEKLY",
        "/D", "MON,TUE,WED,THU,FRI", "/ST", "08:45", "/RI", "60", "/DU", "0008:05",
        "/TR", runner, "/F",
    ])
    if hourly.returncode != 0:
        print("Could not create the Windows automatic schedule.")
        print(hourly.stderr or hourly.stdout)
        return False

    # 2) Catch-up ~30s after each logon (any day; the ledger prevents duplicates).
    _run([
        "schtasks", "/Create", "/TN", LOGON_TASK_NAME, "/SC", "ONLOGON",
        "/DELAY", "0000:30", "/TR", runner, "/F",
    ])

    # 3) Bonus: also catch up an hourly run missed while the PC was asleep/off.
    _enable_catch_up(TASK_NAME)
    return True


def main() -> int:
    print("ProviderFlow Fax Sorter - Setup")
    print("===============================")
    print("Run this the first time, or any time the login/OpenAI settings change.\n")

    existing = {}
    try:
        existing = load_config()
    except Exception:
        existing = {}

    ex_user = (existing.get("providerflow_username") or "").strip()
    ex_pass = (existing.get("providerflow_password") or "").strip()
    ex_key = (existing.get("openai_api_key") or "").strip()
    ex_out = (existing.get("output_dir") or DEFAULT_OUTPUT_DIR).strip()
    ex_model = (existing.get("openai_model") or DEFAULT_OPENAI_MODEL).strip()

    up = "ProviderFlow username" + (f" [Enter = {ex_user}]" if ex_user else "")
    username = input(up + ": ").strip() or ex_user
    if not username:
        print("Username is required.")
        return 1

    pp = "ProviderFlow password" + (" [Enter = keep saved]" if ex_pass else "")
    password = getpass.getpass(pp + ": ").strip() or ex_pass
    if not password:
        print("Password is required.")
        return 1

    # The OpenAI key may already be provided by the build (embedded / key file)
    # or a prior setup. If so, the VA does not need to type anything.
    build_key = (embedded_openai_api_key() or _key_file_value()).strip()
    if ex_key:
        api_key = ex_key
        if api_key == build_key and build_key:
            print("OpenAI key: already configured in this build (no entry needed).")
        else:
            print("OpenAI key: using the saved key (no entry needed).")
    else:
        api_key = getpass.getpass("OpenAI API key (sk-...): ").strip()
        if not api_key:
            print("OpenAI API key is required.")
            return 1

    model = input(f"OpenAI model [Enter = {ex_model}]: ").strip() or ex_model
    out = input(f"Output folder [Enter = {ex_out}]: ").strip() or ex_out
    Path(out).mkdir(parents=True, exist_ok=True)

    root_dir = Path(__file__).resolve().parent.parent
    install_playwright_browser()

    # Optional login test using the real browser.
    print("\nTest the ProviderFlow login now? It briefly opens a browser.")
    if input("Test login? [Y/n]: ").strip().lower() in ("", "y", "yes"):
        try:
            from browser_session import BrowserSession
            debug_dir = APPDATA_DIR / "debug" / "setup_login_test"
            with BrowserSession(DEFAULT_BASE_URL, username, password, debug_dir, headed=True) as s:
                s.login()
                faxes = s.collect_pending_faxes(limit=3)
                s.discovery_dump()
            print(f"Login test PASSED. Sample rows read: {len(faxes)}.")
            if not faxes:
                print("Note: login worked but 0 rows were read. A discovery snapshot was saved to:")
                print(f"  {debug_dir}")
        except Exception as e:
            print("\nLogin test FAILED:", e)
            print("Open ProviderFlow in Chrome and confirm the same username/password works.")
            print(f"Diagnostic snapshot (if any): {APPDATA_DIR / 'debug' / 'setup_login_test'}")
            if input("Save settings anyway? [y/N]: ").strip().lower() not in ("y", "yes"):
                print("Nothing saved.")
                return 1

    # Don't persist a build-provided key into local config/.env, so the embedded
    # key (or openai_key.txt) stays the single rotatable source of truth.
    persist_key = "" if (build_key and api_key == build_key) else api_key
    save_local_config(
        base_url=DEFAULT_BASE_URL, username=username, password=password,
        openai_api_key=persist_key, openai_model=model, output_dir=out, headed=False,
    )
    print("\nSettings saved (locally, not in the repository).")

    if create_task(root_dir):
        print("Automatic runs scheduled: every hour at :45, 8:45 AM to 4:45 PM, Monday-Friday.")
        print("Plus a catch-up run when you log in, or after the computer wakes from sleep.")
    else:
        print("Automatic schedule NOT created. You can still run 2_RUN_NOW.bat manually.")

    write_desktop_button("RUN FAX SORTER NOW.bat", root_dir / "2_RUN_NOW.bat")
    write_desktop_button("OPEN SORTED FAXES.bat", root_dir / "3_OPEN_SORTED_FAXES.bat")

    print("\nSETUP COMPLETE.")
    print("  Run now:        2_RUN_NOW.bat")
    print("  Open folders:   3_OPEN_SORTED_FAXES.bat")
    print("  Diagnose:       4_DISCOVERY_TEST.bat")
    print("The machine must be on, online, and awake during 8:45 AM-4:45 PM for the hourly runs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
