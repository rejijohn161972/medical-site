"""Writes sorted output: patient folders, PDFs/text, and a CSV run summary.

PHI note: everything written here can contain PHI. Output lives only on the
local machine (default C:\\FaxOutput) and is git-ignored. It is never uploaded
anywhere by this tool.
"""
from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path

UNKNOWN_FOLDER = "_UNKNOWN"

CSV_FIELDS = [
    "timestamp",
    "fax_id",
    "patient_name",
    "date_of_birth",
    "document_type",
    "confidence",
    "source",          # ai | fallback | row
    "pdf_downloaded",  # yes | no
    "folder",
    "status",          # ok | error
    "detail",          # error message or note
]


def display_name(patient_name: str) -> str:
    name = (patient_name or "UNKNOWN").strip()
    if name.upper() in ("UNKNOWN", "_UNKNOWN"):
        return "UNKNOWN"
    parts = [p.strip() for p in name.split(",", 1)]
    if len(parts) == 2 and parts[0] and parts[1]:
        return f"{parts[0].title()}, {parts[1].title()}"
    return name.title()


def safe_folder_name(name: str) -> str:
    name = (name or "").strip()
    if not name or name.upper() in ("UNKNOWN", "_UNKNOWN"):
        return UNKNOWN_FOLDER
    name = display_name(name)
    name = re.sub(r"[\\/:*?\"<>|]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or UNKNOWN_FOLDER


def safe_file_part(s: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|]+", "_", s or "")
    s = re.sub(r"\s+", "_", s).strip("_")
    return s[:80] or "fax"


def run_dir_for_today(output_base: str) -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    d = Path(output_base) / today
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_fax(output_base: str, patient_name: str, doc_type: str, fax_id: str,
             pdf_bytes: bytes | None, text_fallback: str,
             file_ext: str = "pdf") -> tuple[Path, Path]:
    """Create the patient folder and write the document (or text fallback)."""
    folder = run_dir_for_today(output_base) / safe_folder_name(patient_name)
    folder.mkdir(parents=True, exist_ok=True)

    base = safe_file_part(f"{safe_folder_name(patient_name)}_{doc_type}_{fax_id}")
    if pdf_bytes:
        ext = (file_ext or "pdf").lstrip(".") or "pdf"
        path = folder / f"{base}.{ext}"
        path.write_bytes(pdf_bytes)
    else:
        path = folder / f"{base}.txt"
        path.write_text(
            "PDF was not downloaded automatically for this fax.\n\n"
            "Document text captured from ProviderFlow:\n\n" + (text_fallback or ""),
            encoding="utf-8",
            errors="ignore",
        )
    return folder, path


class RunSummary:
    """Accumulates one CSV row per fax and writes the summary report."""

    def __init__(self, output_base: str):
        self.rows: list[dict] = []
        self.output_base = output_base

    def add(self, **row) -> None:
        full = {k: "" for k in CSV_FIELDS}
        full["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        full.update({k: v for k, v in row.items() if k in CSV_FIELDS})
        self.rows.append(full)

    def write_csv(self) -> Path:
        run_dir = run_dir_for_today(self.output_base)
        stamp = datetime.now().strftime("%H%M%S")
        path = run_dir / f"run_summary_{stamp}.csv"
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(self.rows)
        return path


def write_short_message(output_base: str, lines: list[str]) -> Path:
    """One-line-per-patient summary the VA can glance at."""
    run_dir = run_dir_for_today(output_base)
    msg = run_dir / "TODAY_SHORT_MESSAGE.txt"
    if not lines:
        msg.write_text("No pending faxes found.\n", encoding="utf-8")
    else:
        msg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return msg
