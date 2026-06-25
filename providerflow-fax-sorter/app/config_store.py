"""Configuration loading for the ProviderFlow Fax Sorter.

Secrets (ProviderFlow password, OpenAI API key) are NEVER stored in this
repository. They are read, in priority order, from:

  1. Real environment variables (PF_USERNAME, PF_PASSWORD, OPENAI_API_KEY, ...)
  2. A local .env file next to the project (created by setup, git-ignored)
  3. A local config.json under %LOCALAPPDATA%\\ProviderFlowFaxSorter (git-ignored)

This keeps PHI-adjacent credentials off disk-in-source-control while still
letting a non-technical user run a simple setup once.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:  # python-dotenv not installed yet (e.g. before first setup)
    load_dotenv = None


# Non-secret defaults.
DEFAULT_OUTPUT_DIR = r"C:\FaxOutput"
DEFAULT_BASE_URL = "https://secure.providerflow.com"
DEFAULT_OPENAI_MODEL = "gpt-4o"

# Per-user, outside the repo. Holds non-secret config plus (optionally) the
# locally entered credentials. Never committed.
APPDATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ProviderFlowFaxSorter"
CONFIG_PATH = APPDATA_DIR / "config.json"

# Project root (folder that contains this app/ package).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

# The clinic's OpenAI key can be EMBEDDED in the distributed build so the VA
# never types it. This stays EMPTY in source control (no secret in git); the
# real value is injected only into the private build handed to the clinic.
# Resolution priority: OPENAI_API_KEY env > .env/config.json > openai_key.txt
# > this embedded key. To rotate, drop a new key into openai_key.txt — no code
# edit needed — or replace the embedded string below.
EMBEDDED_OPENAI_API_KEY_B64 = ""
KEY_FILE_CANDIDATES = (PROJECT_ROOT / "openai_key.txt", APPDATA_DIR / "openai_key.txt")


def embedded_openai_api_key() -> str:
    try:
        return base64.b64decode((EMBEDDED_OPENAI_API_KEY_B64 or "").encode("ascii")).decode("utf-8").strip()
    except Exception:
        return ""


def _key_file_value() -> str:
    for p in KEY_FILE_CANDIDATES:
        try:
            if p.exists():
                val = p.read_text(encoding="utf-8").strip()
                if val:
                    return val
        except Exception:
            continue
    return ""


def ensure_appdata() -> None:
    APPDATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_env_file() -> None:
    """Load the local .env into os.environ if python-dotenv is available."""
    if load_dotenv and ENV_PATH.exists():
        load_dotenv(ENV_PATH, override=False)


def _read_local_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def load_config() -> dict:
    """Merge env vars, .env, and local config.json into one settings dict."""
    _load_env_file()
    local = _read_local_config()

    def pick(env_key: str, local_key: str, default: str = "") -> str:
        return (os.environ.get(env_key) or local.get(local_key) or default or "").strip()

    cfg = {
        "base_url": pick("PF_BASE_URL", "base_url", DEFAULT_BASE_URL),
        "providerflow_username": pick("PF_USERNAME", "providerflow_username"),
        "providerflow_password": pick("PF_PASSWORD", "providerflow_password"),
        "openai_api_key": (pick("OPENAI_API_KEY", "openai_api_key")
                           or _key_file_value() or embedded_openai_api_key()),
        "openai_model": pick("OPENAI_MODEL", "openai_model", DEFAULT_OPENAI_MODEL),
        "output_dir": pick("OUTPUT_DIR", "output_dir", DEFAULT_OUTPUT_DIR),
    }
    headed = (os.environ.get("PF_HEADED") or str(local.get("headed", ""))).strip().lower()
    cfg["headed"] = headed in ("1", "true", "yes", "on")
    return cfg


def save_local_config(
    *,
    base_url: str,
    username: str,
    password: str,
    openai_api_key: str,
    openai_model: str,
    output_dir: str,
    headed: bool,
) -> None:
    """Persist setup choices locally (outside the repo) AND to a .env file.

    Storing the credentials here is a deliberate trade-off so a non-technical
    user does not have to re-enter them every morning. Both targets are
    git-ignored. On a shared machine, restrict access to %LOCALAPPDATA%.
    """
    ensure_appdata()
    payload = {
        "base_url": base_url,
        "providerflow_username": username,
        "providerflow_password": password,
        "openai_api_key": openai_api_key,
        "openai_model": openai_model,
        "output_dir": output_dir,
        "headed": bool(headed),
    }
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    # Also write a .env so advanced users / scheduled tasks can pick it up.
    env_lines = [
        "# Auto-written by setup. Git-ignored. Contains secrets — keep private.",
        f"PF_BASE_URL={base_url}",
        f"PF_USERNAME={username}",
        f"PF_PASSWORD={password}",
        f"OPENAI_API_KEY={openai_api_key}",
        f"OPENAI_MODEL={openai_model}",
        f"OUTPUT_DIR={output_dir}",
        f"PF_HEADED={'1' if headed else '0'}",
        "",
    ]
    ENV_PATH.write_text("\n".join(env_lines), encoding="utf-8")
