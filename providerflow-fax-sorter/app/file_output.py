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


DOC_TYPE_LABELS = {
    "referral": "Referral",
    "labs": "Labs",
    "imaging": "Imaging",
    "consult": "Consult",
    "prescription": "Prescription",
    "insurance": "Insurance Information",
    "miscellaneous": "Miscellaneous",
}


def doc_type_label(doc_type: str) -> str:
    """Human folder label for a document type; anything unknown -> Miscellaneous."""
    return DOC_TYPE_LABELS.get((doc_type or "").strip().lower(), "Miscellaneous")


def patient_folder_name(patient_name: str, doc_type: str) -> str:
    """Folder name like 'Bowie, John - Referral' (or '_UNKNOWN' if no patient)."""
    base = safe_folder_name(patient_name)
    if base == UNKNOWN_FOLDER:
        return UNKNOWN_FOLDER
    name = f"{base} - {doc_type_label(doc_type)}"
    name = re.sub(r"[\\/:*?\"<>|]+", " ", name)
    return re.sub(r"\s+", " ", name).strip()


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
    folder = run_dir_for_today(output_base) / patient_folder_name(patient_name, doc_type)
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
        # One CSV per day, appended by each hourly run, so the office has a single
        # daily log of every fax sorted.
        run_dir = run_dir_for_today(self.output_base)
        path = run_dir / f"run_summary_{run_dir.name}.csv"
        new_file = not path.exists()
        with path.open("a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            if new_file:
                writer.writeheader()
            writer.writerows(self.rows)
        return path


def write_today_index(output_base: str) -> Path:
    """Rewrite TODAY_SHORT_MESSAGE.txt listing every patient folder sorted today.

    Rebuilt from the date folder's subfolders so it always reflects the full day,
    no matter which hourly run added what.
    """
    run_dir = run_dir_for_today(output_base)
    folders = sorted(p.name for p in run_dir.iterdir() if p.is_dir())
    msg = run_dir / "TODAY_SHORT_MESSAGE.txt"
    if folders:
        body = f"Sorted faxes for {run_dir.name} ({len(folders)} folder(s)):\n\n" + "\n".join(folders) + "\n"
    else:
        body = f"No faxes sorted yet for {run_dir.name}.\n"
    msg.write_text(body, encoding="utf-8")
    return msg


def write_short_message(output_base: str, lines: list[str]) -> Path:
    """One-line-per-patient summary the VA can glance at."""
    run_dir = run_dir_for_today(output_base)
    msg = run_dir / "TODAY_SHORT_MESSAGE.txt"
    if not lines:
        msg.write_text("No pending faxes found.\n", encoding="utf-8")
    else:
        msg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return msg
