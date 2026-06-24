"""Entry point: log in, read all pending faxes, sort them into patient folders.

Run modes:
  python -m app.main                 # full run
  python -m app.main --discovery     # log in + dump real page structure only
  python -m app.main --limit 5       # process at most 5 faxes (testing)
  python -m app.main --headed        # show the browser window
"""
from __future__ import annotations

import argparse
import sys
import traceback
from datetime import datetime
from pathlib import Path

from config_store import load_config, APPDATA_DIR, DEFAULT_OPENAI_MODEL
from file_output import save_fax, write_short_message, display_name, RunSummary
from openai_extract import extract_patient_info


def log_line(msg: str) -> None:
    APPDATA_DIR.mkdir(parents=True, exist_ok=True)
    with (APPDATA_DIR / "last_run.log").open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def _new_session(cfg: dict, debug_dir: Path):
    """Import Playwright lazily so a missing install gives a friendly message."""
    try:
        from browser_session import BrowserSession
    except ImportError as e:
        raise RuntimeError(
            "Playwright is not installed. Run 1_FIRST_TIME_SETUP.bat, or run:\n"
            "    pip install -r requirements.txt\n"
            "    python -m playwright install chromium\n"
            f"(import error: {e})"
        ) from e
    return BrowserSession(
        base_url=cfg["base_url"],
        username=cfg["providerflow_username"],
        password=cfg["providerflow_password"],
        debug_dir=debug_dir,
        headed=cfg.get("headed", False),
    )


def run(limit: int | None = None, discovery_only: bool = False, headed: bool | None = None) -> int:
    APPDATA_DIR.mkdir(parents=True, exist_ok=True)
    (APPDATA_DIR / "last_run.log").write_text("", encoding="utf-8")
    start = datetime.now()
    log_line(f"Started: {start}")

    cfg = load_config()
    if headed is not None:
        cfg["headed"] = headed
    output_dir = cfg["output_dir"]
    debug_dir = APPDATA_DIR / "debug" / start.strftime("%Y%m%d_%H%M%S")
    debug_dir.mkdir(parents=True, exist_ok=True)

    print("Logging into ProviderFlow (real browser)...")
    with _new_session(cfg, debug_dir) as session:
        session.login()
        print("Login OK.")

        if discovery_only:
            report = session.discovery_dump()
            print("\nDiscovery report saved:")
            print(f"  {report}")
            print(f"  (full folder: {debug_dir})")
            print("No faxes were processed. Send that folder back if rows look wrong.")
            log_line(f"Discovery report: {report}")
            return 0

        if not cfg.get("openai_api_key"):
            raise RuntimeError("OpenAI API key is missing. Run 1_FIRST_TIME_SETUP.bat and enter it.")

        # Always capture a discovery snapshot so a 0-result run is diagnosable.
        session.discovery_dump()

        print("Reading pending faxes...")
        faxes = session.collect_pending_faxes(limit=limit)
        print(f"Found {len(faxes)} pending fax item(s).")
        log_line(f"Found faxes: {len(faxes)}")

        if not faxes:
            print("\nNo rows were read from the pending list.")
            print(f"A discovery snapshot was saved to: {debug_dir}")
            print("Send that folder back so the row selectors can be confirmed.")

        summary = RunSummary(output_dir)
        short_lines: list[str] = []

        for idx, fax in enumerate(faxes, start=1):
            print(f"Processing fax {idx} of {len(faxes)}...")
            try:
                text, final_url, detail_html = session.open_fax_text(fax)
                info = extract_patient_info(
                    text, cfg["openai_api_key"], model=cfg.get("openai_model", DEFAULT_OPENAI_MODEL)
                )
                patient = info.get("patient_name") or "UNKNOWN"
                doc_type = info.get("document_type") or "miscellaneous"
                source = "fallback" if info.get("ai_error") else "ai"

                pdf = session.try_download_pdf(fax, detail_html)
                folder, saved = save_fax(output_dir, patient, doc_type, fax.fax_id, pdf, text)

                line = f"{display_name(patient)} — {doc_type}"
                if line not in short_lines:
                    short_lines.append(line)
                print("  " + line + ("  [pdf]" if pdf else "  [text only]"))

                summary.add(
                    fax_id=fax.fax_id, patient_name=patient, date_of_birth=info.get("date_of_birth"),
                    document_type=doc_type, confidence=info.get("confidence"), source=source,
                    pdf_downloaded="yes" if pdf else "no", folder=str(folder), status="ok",
                    detail=info.get("ai_error", ""),
                )
                log_line(f"OK fax_id={fax.fax_id} patient={patient} type={doc_type} pdf={bool(pdf)} saved={saved}")
            except Exception as e:  # noqa: BLE001 — one bad fax must not stop the run
                summary.add(fax_id=getattr(fax, "fax_id", "?"), patient_name="UNKNOWN",
                            document_type="miscellaneous", status="error", detail=str(e)[:300])
                log_line(f"ERROR fax_id={getattr(fax, 'fax_id', '?')}: {e}")
                log_line(traceback.format_exc())
                print(f"  Could not process this fax. Continuing. ({e})")

        csv_path = summary.write_csv()
        msg_path = write_short_message(output_dir, short_lines)

    print()
    print("DONE.")
    print(f"Sorted folders: {output_dir}")
    print(f"CSV summary:    {csv_path}")
    print(f"Short message:  {msg_path}")
    log_line(f"Finished: {datetime.now()} csv={csv_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="ProviderFlow Fax Sorter")
    parser.add_argument("--manual", action="store_true", help="(compat) run now")
    parser.add_argument("--scheduled", action="store_true", help="(compat) scheduled run")
    parser.add_argument("--limit", type=int, default=None, help="Process at most N faxes.")
    parser.add_argument("--discovery", action="store_true",
                        help="Log in and dump the real page structure without processing.")
    parser.add_argument("--headed", action="store_true", help="Show the browser window.")
    args = parser.parse_args()
    try:
        return run(limit=args.limit, discovery_only=args.discovery,
                   headed=True if args.headed else None)
    except FileNotFoundError as e:
        print(str(e))
        return 2
    except Exception as e:  # noqa: BLE001
        print("ERROR:", e)
        print("Details saved in:", APPDATA_DIR / "last_run.log")
        log_line("FATAL: " + repr(e))
        log_line(traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
