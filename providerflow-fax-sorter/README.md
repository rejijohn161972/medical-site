# ProviderFlow Fax Sorter

Automates the morning fax triage in [ProviderFlow](https://secure.providerflow.com):
it logs in, reads **all** rows of the **Pending Documents** table, names each one
from the patient in the *Description / Fulltext* column (refined by OpenAI),
**opens each document and copies the file** into a `LASTNAME, FIRSTNAME` folder,
and writes a CSV summary — so the VA can drag the already-sorted documents into
Epic Citrix.

The real Pending Documents table has these columns, and the patient name is in
the Description column (`WILSON, NANCY - Referral`, `KELLEY, TONI - Referral`, …):

```
Site | Source | Status | Created By | Assigned To | Created Date | Description / Fulltext | Pages
```

A new dated folder is created each day, and each patient folder is named
`LASTNAME, FIRSTNAME - Type`. If the reason can't be determined it's
`- Miscellaneous`. ProviderFlow is re-scanned **every hour from 8:45 AM to
4:45 PM, Mon–Fri**, and only *new* faxes are added (no duplicates).

```
C:\FaxOutput\2026-06-24\                    (new dated folder each day)
├── Wilson, Nancy - Referral\              Wilson,_Nancy_referral_<id>.pdf
├── Kelley, Toni - Referral\               ...
├── James, Rick - Insurance Information\   ...
├── Fry, David - Miscellaneous\            (reason was unclear)
├── _UNKNOWN\                              (patient couldn't be identified)
├── run_summary_2026-06-24.csv             (one daily log, appended each hour)
└── TODAY_SHORT_MESSAGE.txt                (today's folder list)
```

---

## Why earlier versions found "0 pending faxes"

ProviderFlow is an old PHP app that builds its pending-fax table **with
JavaScript/AJAX** (`batchtable.php`) *after* the page loads. A plain HTTP
scraper (`requests`/`urllib`) downloads the empty page shell before that
JavaScript runs, so it sees **zero rows** — even though Chrome clearly shows
the faxes.

This version drives a **real Chromium browser** (via Playwright). The
JavaScript runs, the rows appear, and the tool reads the **rendered** rows —
across every frame — exactly as a human sees them. That is the fix.

---

## What you need (once)

1. **Python 3.10+** — install from [python.org](https://www.python.org/downloads/)
   and check **“Add Python to PATH”** during install.
2. An **OpenAI API key** (`sk-...`) on an account with billing enabled.
3. The **ProviderFlow username/password** (the same ones used in Chrome).

## Setup

1. Unzip this folder somewhere permanent (e.g. `C:\ProviderFlowFaxSorter`).
   Do **not** run it from inside the ZIP.
2. Double-click **`1_FIRST_TIME_SETUP.bat`**. It will:
   - install the Python dependencies and the Chromium browser engine,
   - ask for the ProviderFlow login, OpenAI key, model, and output folder,
   - optionally open a browser to **test the login**,
   - schedule the automatic **Monday–Friday 8:45 AM** run.

Your credentials are saved **only on this machine** (under
`%LOCALAPPDATA%\ProviderFlowFaxSorter` and a local `.env`). They are **never**
written into the code or committed to git.

## Daily use

| Button | What it does |
| --- | --- |
| **`2_RUN_NOW.bat`** | Run the sorter immediately. |
| **`3_OPEN_SORTED_FAXES.bat`** | Open the sorted-output folder. |
| **`4_DISCOVERY_TEST.bat`** | Log in and save a snapshot of the page structure (no faxes touched). Use this if something looks wrong. |

After the 8:45 AM run, open today's folder and drag the patient subfolders into
Epic Citrix.

---

## If it still reads 0 faxes — run discovery first

Because ProviderFlow's exact page markup can only be confirmed against the live
site, the project ships a **discovery mode**. Run **`4_DISCOVERY_TEST.bat`**.
It saves, to a timestamped folder under
`%LOCALAPPDATA%\ProviderFlowFaxSorter\debug\`:

- the post-login URL, page title, and whether the login form is still present,
- the **rendered HTML of every frame** (`frame_0.html`, `frame_1.html`, …),
- every table's headers and the count of fax-like rows detected,
- all candidate links (`quickflow` / `sessionkey` / `print` / `pdf` / …),
- a full-page screenshot.

That folder also contains **`SHARE_THIS_diagnostics.txt`** — a PHI-safe summary
(no patient names; URLs/ids redacted) listing the row count, table headers, the
detected Export-menu items, and whether the first document downloaded. **Paste
that one back** for a quick fix. Send the full zipped folder only if deeper
detail is needed. (A discovery snapshot is also saved automatically on every
normal run, so a 0-result morning is always diagnosable after the fact.)

---

## The automatic hourly schedule

Setup creates a Windows Task Scheduler job named **“ProviderFlow Fax Sorter”**
that runs `app\run_hidden.vbs` (invisibly) **every hour at :45, from 08:45
through 16:45 (4:45 PM), Monday–Friday** — so faxes that arrive during the day
get sorted within the hour.

- Each run sorts only **new** faxes; a persistent ledger
  (`%LOCALAPPDATA%\ProviderFlowFaxSorter\processed_faxes.json`) prevents
  re-creating folders for faxes already handled earlier that day.
- View/edit the schedule in **Task Scheduler** (`taskschd.msc`).
- The machine must be **on, online, and awake** during 8:45 AM–4:45 PM.
- Scheduled-run output is logged to
  `%LOCALAPPDATA%\ProviderFlowFaxSorter\scheduled.log`.

Re-create or change the schedule any time by re-running `1_FIRST_TIME_SETUP.bat`.

---

## HIPAA / PHI notes

- This tool handles **PHI**. Run it only on the trusted, access-controlled
  clinic machine.
- Only a **minimal text slice** (capped at ~4,500 characters) is sent to OpenAI,
  solely to identify the patient name/DOB/document type. Use an OpenAI account
  covered by a signed **Business Associate Agreement (BAA)** before processing
  real PHI.
- All sorted output stays **local** (default `C:\FaxOutput`). Nothing is uploaded
  anywhere except that minimal OpenAI request.
- Output, debug snapshots, logs, and `.env`/`config.json` are **git-ignored** so
  PHI and secrets never reach the repository.

## Security

- **Rotate any password or API key that was ever pasted into a chat, screenshot,
  or shared file.** Treat it as compromised.
- Secrets live only in `%LOCALAPPDATA%\ProviderFlowFaxSorter` and a local `.env`.
  On a shared machine, restrict access to that folder.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `'python' is not recognized` | Reinstall Python with “Add to PATH”, reopen the window. |
| Login test fails | Log into ProviderFlow in Chrome with the same username/password. If MFA appears, the script can't pass it unattended. |
| `Found 0 pending fax item(s)` | Run `4_DISCOVERY_TEST.bat` and send the snapshot folder. |
| Folders are created but contain a `.txt` instead of the document | The document opened but the file link wasn't recognized. The discovery snapshot now opens the first document and saves `first_document_viewer.html` + how it behaved — send that so the exact download path can be wired in. |
| OpenAI error in the CSV | Check the API key has billing/quota; the run still continues using a local best-guess. |
| Want to watch it work | Run `2_RUN_NOW.bat` after setting `PF_HEADED=1` in `.env`, or run `python app\main.py --headed`. |

## Layout

```
providerflow-fax-sorter/
├── 1_FIRST_TIME_SETUP.bat   2_RUN_NOW.bat   3_OPEN_SORTED_FAXES.bat   4_DISCOVERY_TEST.bat
├── requirements.txt   .env.example   .gitignore   README.md
└── app/
    ├── main.py              # orchestrates a run
    ├── browser_session.py   # Playwright login + read rendered rows + discovery
    ├── openai_extract.py    # patient/DOB/type via OpenAI (with local fallback)
    ├── file_output.py       # patient folders, PDFs, CSV summary
    ├── config_store.py      # loads settings from env / .env / local config
    ├── setup.py             # interactive setup + scheduling
    ├── run_hidden.vbs   run_scheduled_internal.bat
    └── __init__.py
```
