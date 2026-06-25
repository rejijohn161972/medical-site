"""Patient-name / DOB / document-type extraction via the OpenAI API.

HIPAA note: this sends a *minimal* slice of fax text to OpenAI to identify the
patient. Send the least text needed. Use an OpenAI account covered by a signed
Business Associate Agreement (BAA) before processing real PHI. No PHI is logged
to stdout by this module; errors are redacted of any sk- key fragments.
"""
from __future__ import annotations

import json
import re

import requests

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"

# Hard cap on characters sent to OpenAI — keep PHI exposure minimal.
MAX_CHARS = 4500

SYSTEM_PROMPT = (
    "You are a medical records assistant. You receive text from ONE faxed "
    "medical document (a pending-fax list row and/or OCR of the document). "
    "Identify the patient the document is ABOUT — not the doctor, sender, "
    "clinic, or facility. Return the patient name as 'LASTNAME, FIRSTNAME' "
    "when known, otherwise 'UNKNOWN'. Choose document_type only from the "
    "allowed list. document_type guide: referral (a referral or authorization "
    "request to see a provider), labs (laboratory results), imaging (radiology, "
    "X-ray, CT, MRI, ultrasound), consult (consult or progress notes), "
    "prescription (medication/Rx), insurance (insurance card, eligibility, "
    "benefits, coverage, or explanation of benefits/EOB), miscellaneous "
    "(anything else, or when the reason is unclear). Correct an obvious OCR "
    "error only when the correction is very clear. Do not invent a name or DOB."
)

# Strict JSON schema for OpenAI Structured Outputs.
EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "patient_name": {
            "type": "string",
            "description": "Patient name as 'LASTNAME, FIRSTNAME', or 'UNKNOWN'.",
        },
        "date_of_birth": {
            "type": ["string", "null"],
            "description": "Patient DOB as MM/DD/YYYY when found, else null.",
        },
        "document_type": {
            "type": "string",
            "enum": ["labs", "referral", "imaging", "consult", "prescription", "insurance", "miscellaneous"],
        },
        "confidence": {
            "type": "string",
            "enum": ["high", "medium", "low"],
        },
    },
    "required": ["patient_name", "date_of_birth", "document_type", "confidence"],
    "additionalProperties": False,
}

RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "providerflow_fax_extraction",
        "strict": True,
        "schema": EXTRACTION_SCHEMA,
    },
}


def extract_patient_info(text: str, api_key: str, model: str = "gpt-4o", timeout: int = 60) -> dict:
    """Return {patient_name, date_of_birth, document_type, confidence[, ai_error]}.

    Never raises for a model/network failure: falls back to local regex parsing
    so the overall run keeps going.
    """
    trimmed = (text or "")[:MAX_CHARS].strip()
    if not trimmed:
        return {"patient_name": "UNKNOWN", "date_of_birth": None,
                "document_type": "miscellaneous", "confidence": "low"}

    if not (api_key or "").strip():
        raise RuntimeError("OpenAI API key is missing. Run setup again and enter the OpenAI API key.")

    payload = {
        "model": model or "gpt-4o",
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "DOCUMENT TEXT:\n" + trimmed},
        ],
        "response_format": RESPONSE_FORMAT,
    }

    try:
        content = _post_chat(payload, api_key, timeout)
        parsed = _parse_json_from_text(content)
        if parsed:
            return _normalize(parsed)
        raise RuntimeError("OpenAI response did not contain parseable JSON.")
    except Exception as e:  # noqa: BLE001 — deliberately resilient
        fallback = fallback_extract(text)
        fallback["ai_error"] = _short_error(e)
        return fallback


def test_openai_connection(api_key: str, model: str = "gpt-4o") -> None:
    """Raise if the key/model can't perform a trivial extraction."""
    sample = "Patient Name: Doe, Jane\nDOB: 01/02/1970\nBasic metabolic panel results attached."
    result = extract_patient_info(sample, api_key, model=model)
    if result.get("ai_error"):
        raise RuntimeError(result["ai_error"])


def _post_chat(payload: dict, api_key: str, timeout: int) -> str:
    resp = requests.post(
        OPENAI_CHAT_URL,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key.strip(),
        },
        data=json.dumps(payload),
        timeout=timeout,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"OpenAI API HTTP {resp.status_code}: {resp.text[:600]}")
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("OpenAI API returned no choices.")
    message = choices[0].get("message") or {}
    if message.get("refusal"):
        raise RuntimeError("OpenAI model refused the extraction request.")
    return message.get("content") or ""


def _parse_json_from_text(content: str) -> dict | None:
    if not content:
        return None
    content = content.strip()
    try:
        return json.loads(content)
    except Exception:
        pass
    m = re.search(r"\{.*\}", content, flags=re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def _normalize(d: dict) -> dict:
    name = str(d.get("patient_name") or "UNKNOWN").strip()
    if not name or name.lower() in ("none", "null", "unknown patient", "n/a"):
        name = "UNKNOWN"

    doc = str(d.get("document_type") or "miscellaneous").strip().lower()
    doc_map = {
        "lab": "labs", "labs": "labs", "laboratory": "labs",
        "referral": "referral", "refer": "referral",
        "image": "imaging", "imaging": "imaging", "radiology": "imaging",
        "consult": "consult", "consultation": "consult", "note": "consult",
        "prescription": "prescription", "rx": "prescription",
        "insurance": "insurance", "insurance information": "insurance",
        "insurance card": "insurance", "eob": "insurance", "eligibility": "insurance",
        "benefits": "insurance", "coverage": "insurance",
        "other": "miscellaneous", "misc": "miscellaneous", "miscellaneous": "miscellaneous",
    }
    doc = doc_map.get(doc, "miscellaneous")

    conf = str(d.get("confidence") or "low").lower()
    if conf not in ("high", "medium", "low"):
        conf = "low"

    return {
        "patient_name": name,
        "date_of_birth": d.get("date_of_birth") or None,
        "document_type": doc,
        "confidence": conf,
    }


def fallback_extract(text: str) -> dict:
    """Pure-Python best guess when the API call fails."""
    name = "UNKNOWN"
    patterns = [
        # "LASTNAME, FIRSTNAME" appearing on its own (e.g. a pending-list row).
        r"\b([A-Z][A-Za-z'\-]+\s*,\s*[A-Z][A-Za-z'\-]+)\b",
        r"Patient\s*Name\s*[:\-]\s*([A-Z][A-Za-z'\-]+\s+[A-Z][A-Za-z'\-]+)",
        r"Patient\s*[:\-]\s*([A-Z][A-Za-z'\-]+\s+[A-Z][A-Za-z'\-]+)",
        r"Name\s*[:\-]\s*([A-Z][A-Za-z'\-]+\s+[A-Z][A-Za-z'\-]+)",
        r"Re\s*[:\-]\s*([A-Z][A-Za-z'\-]+\s+[A-Z][A-Za-z'\-]+)",
    ]
    for p in patterns:
        m = re.search(p, text or "")
        if m:
            name = to_last_first(m.group(1))
            break

    dob = None
    m = re.search(r"(?:DOB|Date\s*of\s*Birth)\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{2,4})", text or "", flags=re.I)
    if m:
        dob = m.group(1)

    return {
        "patient_name": name,
        "date_of_birth": dob,
        "document_type": guess_doc_type(text),
        "confidence": "low" if name == "UNKNOWN" else "medium",
    }


def to_last_first(s: str) -> str:
    s = re.sub(r"\s+", " ", (s or "").strip(" .,:;\n\t"))
    if "," in s:
        parts = [p.strip() for p in s.split(",", 1)]
        return (parts[0] + ", " + parts[1]).upper()
    parts = s.split()
    if len(parts) >= 2:
        return (parts[-1] + ", " + " ".join(parts[:-1])).upper()
    return s.upper() if s else "UNKNOWN"


def guess_doc_type(text: str) -> str:
    low = (text or "").lower()
    if any(x in low for x in ["creatinine", "bun", "glucose", "hemoglobin", "potassium",
                              "sodium", "specimen", "labcorp", "quest", "reference range"]):
        return "labs"
    if any(x in low for x in ["insurance", "policy number", "member id", "group number",
                              "subscriber", "explanation of benefits", "eob", "eligibility",
                              "coverage", "payer", "copay", "deductible"]):
        return "insurance"
    if any(x in low for x in ["refer", "referral", "authorization", "consult requested"]):
        return "referral"
    if any(x in low for x in ["ct ", "mri", "ultrasound", "x-ray", "radiology", "impression", "findings"]):
        return "imaging"
    if any(x in low for x in ["progress note", "consultation", "assessment", "plan"]):
        return "consult"
    if any(x in low for x in ["prescription", "rx", "sig:", "dispense"]):
        return "prescription"
    return "miscellaneous"


def _short_error(e: Exception) -> str:
    text = str(e)
    text = re.sub(r"sk-[A-Za-z0-9_\-]+", "sk-<redacted>", text)
    return text[:500]
