"""Real-browser ProviderFlow automation (Playwright / Chromium).

WHY A REAL BROWSER:
ProviderFlow renders its "Pending Documents" table with client-side
JavaScript/AJAX (DataTables-style: column filters + a live count badge). A
plain HTTP client (requests/urllib) downloads the page *shell* before that JS
runs, so it sees zero rows -> "Found 0 pending faxes", even though Chrome
clearly shows them. Driving a real Chromium browser lets the JS run and the
rows appear, exactly as a human sees them. We read the *rendered* table rows,
across all frames, which is the robust fix.

REAL DOM (confirmed from the live portal, logged in as the clinic user):
  - Login: /login.php with text fields `username` / `password` and a Submit
    button (also reachable from /index.php).
  - Pending list on /index.php, tab "Pending Documents", columns:
      Site | Source | Status | Created By | Assigned To | Created Date |
      Description / Fulltext | Pages
  - The patient name is in the Description column as "LASTNAME, FIRSTNAME - ..."
    so most folders can be named directly from the row text.
  - Each row has a Pages icon / actions that open the document viewer.

PHI note: this logs into a live medical portal. Run it on the trusted clinic
machine only. Nothing here transmits PHI anywhere except (separately) the
minimal text slice sent to OpenAI for name extraction.
"""
from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

try:  # Imported lazily so the module's pure logic is usable without the browser.
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - resolved at first real run / setup
    sync_playwright = None


# Patient name in the Description column, e.g. "WILSON, NANCY - Referral".
NAME_RE = re.compile(r"\b([A-Z][A-Za-z'.\-]+\s*,\s*[A-Z][A-Za-z'.\-]+)")
# Session / document identifiers seen in links and onclick handlers.
ID_PATTERNS = [
    r"loadbatch\(['\"]([A-Za-z0-9]{8,})['\"]",
    r"displaysession\(['\"]?([A-Za-z0-9]{8,})",
    r"displaysession=([A-Za-z0-9]{8,})",
    r"sessionkey=([A-Za-z0-9]{8,})",
    r"(?:docid|documentid|docguid|fileid)=([A-Za-z0-9]{6,})",
    r"opendoc\(['\"]([A-Za-z0-9]{6,})['\"]",
]
# href fragments that look like "open this document".
OPEN_LINK_HINTS = ("quickflow", "sessionkey", "displaysession", "viewfile",
                   "viewdoc", "docid", "documentid", "openfile", "getfile",
                   "showfile", "file=", "fileid", "pdf", "print", "page=")
# Things that are never the document (avoid clicking these).
LINK_DENY = ("logout", "log out", "javascript:void", "#", "mailto:", "preferences")
# Likely file endpoints when fetching the actual document bytes.
FILE_LINK_HINTS = ("pdf", "viewfile", "getfile", "showfile", "downloadfile",
                   "download", "file=", "fileid", "print", "page=", "image", "tiff")

# Returns rich, lightweight info for every table row in a frame.
ROW_SCAN_JS = r"""
() => {
  const out = [];
  for (const r of Array.from(document.querySelectorAll('table tr'))) {
    const text = (r.innerText || '').replace(/\s+/g, ' ').trim();
    if (!text) continue;
    const tdCount = r.querySelectorAll('td').length;
    const hasTh = !!r.querySelector('th');
    const hasFilter = !!r.querySelector('select, input[type=text], input:not([type]), textarea');
    const anchors = Array.from(r.querySelectorAll('a[href]')).map(a => a.getAttribute('href'));
    const clicks = Array.from(r.querySelectorAll('[onclick]')).map(e => e.getAttribute('onclick'));
    let html = r.outerHTML || '';
    if (html.length > 4000) html = html.slice(0, 4000);
    out.push({ text, tdCount, hasTh, hasFilter, anchors, clicks, html });
  }
  return out;
}
"""


@dataclass
class FaxItem:
    fax_id: str
    label: str                       # full row text (includes the patient name)
    doc_id: str = ""                 # session/document id if found
    open_url: str = ""               # best URL to open the document viewer
    links: list[str] = field(default_factory=list)   # all resolved row links
    onclick: str = ""                # combined onclick handlers from the row
    frame_url: str = ""
    raw_html: str = field(default="", repr=False)


@dataclass
class DocResult:
    text: str
    url: str
    html: str = field(default="", repr=False)
    pdf_bytes: bytes | None = field(default=None, repr=False)
    file_ext: str = "pdf"
    note: str = ""


class BrowserSession:
    def __init__(self, base_url: str, username: str, password: str, debug_dir: Path,
                 headed: bool = False, timeout_ms: int = 45000):
        self.base_url = base_url.rstrip("/")
        self.username = (username or "").strip()
        self.password = (password or "").strip()
        self.debug_dir = debug_dir
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        self.headed = headed
        self.timeout_ms = timeout_ms
        self._pw = None
        self._browser = None
        self.context = None
        self.page = None

    # -- lifecycle -----------------------------------------------------------
    def __enter__(self) -> "BrowserSession":
        if sync_playwright is None:
            raise RuntimeError(
                "Playwright is not installed. Run 1_FIRST_TIME_SETUP.bat, or:\n"
                "    pip install -r requirements.txt\n"
                "    python -m playwright install chromium"
            )
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=not self.headed)
        self.context = self._browser.new_context(
            accept_downloads=True,
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126 Safari/537.36"),
        )
        self.context.set_default_timeout(self.timeout_ms)
        self.page = self.context.new_page()
        return self

    def __exit__(self, *exc) -> None:
        for closer in (self.context, self._browser):
            try:
                if closer:
                    closer.close()
            except Exception:
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    # -- login ---------------------------------------------------------------
    def login(self) -> None:
        if not self.username or not self.password:
            raise RuntimeError("ProviderFlow username/password are blank. Run setup again.")

        # The real login form posts to login.php with visible username/password
        # plus hidden fields; a real browser submits the hidden fields for us.
        self.page.goto(self.base_url + "/index.php", wait_until="domcontentloaded")
        self._safe_screenshot("01_login_page.png")

        user_box = self._first_locator(["input[name='username']", "input#username", "input[type='text']"])
        pass_box = self._first_locator(["input[name='password']", "input#password", "input[type='password']"])
        if not user_box or not pass_box:
            self._dump_html("01_login_page.html")
            raise RuntimeError("Could not find the ProviderFlow username/password fields on the login page.")

        user_box.fill(self.username)
        pass_box.fill(self.password)

        submit = self._first_locator(["input[type='submit']", "button[type='submit']",
                                      "input[value*='Submit' i]", "button:has-text('Submit')",
                                      "button:has-text('Log')"])
        try:
            if submit:
                submit.click()
            else:
                pass_box.press("Enter")
        except Exception:
            pass_box.press("Enter")

        self._wait_idle()
        self._safe_screenshot("02_after_login.png")

        if self._login_form_present():
            self._dump_html("02_after_login.html")
            raise RuntimeError(
                "ProviderFlow login did not succeed (login form still present). "
                "Verify the username/password by logging in with Chrome, then run setup again."
            )

    # -- pending list --------------------------------------------------------
    def collect_pending_faxes(self, limit: int | None = None, max_pages: int = 30) -> list[FaxItem]:
        """Read every pending fax across all pages from the rendered table."""
        self._ensure_pending_view()
        self._wait_for_rows()

        seen: set[str] = set()
        faxes: list[FaxItem] = []
        for page_no in range(1, max_pages + 1):
            self._load_all_rows()  # scroll so lazy/long tables render fully
            self._dump_html(f"pending_page_{page_no}.html")
            new_on_page = 0
            for item in self._scan_all_frames():
                key = item.doc_id or _norm(item.label)
                if not key or key in seen:
                    continue
                seen.add(key)
                faxes.append(item)
                new_on_page += 1
                if limit and len(faxes) >= limit:
                    return faxes

            if not self._go_to_next_page():
                break
            if new_on_page == 0:
                break  # safety: a page that added nothing means pagination looped
        return faxes

    def _scan_all_frames(self) -> list[FaxItem]:
        items: list[FaxItem] = []
        for frame in self.page.frames:
            try:
                rows = frame.evaluate(ROW_SCAN_JS)
            except Exception:
                continue
            frame_url = frame.url or self.page.url
            for row in rows or []:
                item = self._row_to_fax(row, frame_url)
                if item:
                    items.append(item)
        return items

    def _row_to_fax(self, row: dict, frame_url: str) -> FaxItem | None:
        text = (row.get("text") or "").strip()
        if row.get("hasTh") or row.get("hasFilter"):
            return None  # header row or the column-filter row

        anchors = [a for a in (row.get("anchors") or []) if a]
        clicks = [c for c in (row.get("clicks") or []) if c]
        blob = " ".join([row.get("html", "")] + clicks + anchors)
        doc_id = _first_match(ID_PATTERNS, blob)
        name_match = NAME_RE.search(text)
        td_count = int(row.get("tdCount") or 0)

        # A pending-document row has the full set of columns (Site..Pages) and is
        # neither a header nor the filter row. Fall back to name/id for safety.
        is_data_row = td_count >= 5
        if not (is_data_row or name_match or doc_id):
            return None

        resolved = []
        for href in anchors:
            low = (href or "").strip().lower()
            if not low or any(d in low for d in LINK_DENY):
                continue
            resolved.append(urljoin(frame_url, html_lib.unescape(href)))

        open_url = ""
        for url in resolved:
            if any(h in url.lower() for h in OPEN_LINK_HINTS):
                open_url = url
                break
        if not open_url and doc_id:
            open_url = urljoin(self.base_url + "/", f"quickflow/index.php?sessionkey={doc_id}")
        if not open_url and resolved:
            open_url = resolved[0]

        if name_match:
            fax_id = re.sub(r"[^A-Za-z0-9]+", "_", name_match.group(1)).strip("_")[:60]
        elif doc_id:
            fax_id = doc_id
        else:
            fax_id = "fax_" + str(abs(hash(text)) % 10_000_000)

        return FaxItem(fax_id=fax_id, label=text, doc_id=doc_id, open_url=open_url,
                       links=resolved, onclick=" ".join(clicks), frame_url=frame_url,
                       raw_html=row.get("html", ""))

    def _ensure_pending_view(self) -> None:
        """Make sure the 'Pending Documents' tab is the active view."""
        for frame in self.page.frames:
            try:
                tab = frame.get_by_text(re.compile(r"pending\s+documents", re.I)).first
                if tab and tab.count() > 0:
                    tab.click()
                    self._wait_idle()
                    return
            except Exception:
                continue

    def _wait_for_rows(self) -> None:
        """Poll for the AJAX-rendered rows to appear."""
        waited, step = 0, 750
        while waited < self.timeout_ms:
            if self._scan_all_frames():
                return
            self.page.wait_for_timeout(step)
            waited += step

    def _load_all_rows(self) -> None:
        """Scroll to the bottom repeatedly so lazy/long tables render every row."""
        last = -1
        for _ in range(20):
            total = 0
            for frame in self.page.frames:
                try:
                    total += frame.evaluate("() => document.querySelectorAll('table tr').length") or 0
                except Exception:
                    pass
            try:
                for frame in self.page.frames:
                    frame.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass
            self.page.wait_for_timeout(400)
            if total == last:
                break
            last = total

    def _go_to_next_page(self) -> bool:
        """Best-effort pagination: click a 'Next' control if one exists."""
        before = self._page_signature()
        for frame in self.page.frames:
            for getter in (
                lambda f: f.locator("a.paginate_button.next:not(.disabled), li.next:not(.disabled) a"),
                lambda f: f.get_by_role("link", name=re.compile(r"^\s*(next|more|›|»)\s*$", re.I)),
                lambda f: f.get_by_role("button", name=re.compile(r"^\s*(next|more|›|»)\s*$", re.I)),
                lambda f: f.locator("a[href*='selectedpage'], a[href*='pagenum'], a[href*='page=']"),
            ):
                try:
                    loc = getter(frame).first
                    if loc and loc.count() > 0 and loc.is_enabled():
                        href = (loc.get_attribute("href") or "")
                        if href.strip().endswith("#") and "page" not in href.lower():
                            pass  # DataTables uses href="#"; still clickable
                        loc.click()
                        self._wait_idle()
                        self.page.wait_for_timeout(600)
                        if self._page_signature() != before:
                            return True
                except Exception:
                    continue
        return False

    # -- document open + copy into folder ------------------------------------
    def fetch_document(self, fax: FaxItem) -> DocResult:
        """Get the document file into the patient folder, plus text for naming.

        Strategy, in order:
          1. Per-row Export/Download action (the gear/⚙ menu) -> capture the
             browser download. This is how a human gets the file, per the user.
          2. Open the document viewer and download the embedded file (PDF/image)
             via the authenticated session.
          3. Headless print-to-PDF of the viewer so *something* lands in the folder.
        Text for OpenAI naming comes from the row label (which already holds the
        patient name for triaged rows) and, when needed, the opened viewer.
        """
        pdf_bytes, ext, how = self._download_via_row_actions(fax)

        # Fast path: file in hand AND the row already names the patient -> done,
        # no need to open the viewer (keeps a 36-row run fast).
        if pdf_bytes and NAME_RE.search(fax.label):
            return DocResult(text=fax.label, url=fax.open_url or self.page.url,
                             pdf_bytes=pdf_bytes, file_ext=ext, note=f"file via {how}")

        viewer, opened_via = self._open_viewer(fax)
        text, final_url, full_html = fax.label, (fax.open_url or self.page.url), ""
        if viewer is not None:
            try:
                viewer.wait_for_timeout(1000)
                text_parts, html_parts, file_links = [], [], []
                for frame in viewer.frames:
                    try:
                        for ta in frame.locator("textarea").all():
                            val = (ta.input_value() or "").strip()
                            if len(val) > 20:
                                text_parts.append(val)
                        body = frame.evaluate("() => document.body ? document.body.innerText : ''") or ""
                        body = re.sub(r"[ \t]+\n", "\n", body).strip()
                        if len(body) > 40:
                            text_parts.append(body[:8000])
                        html = frame.content() or ""
                        html_parts.append(html[:30000])
                        file_links += self._file_links_in(html, frame.url or viewer.url)
                    except Exception:
                        continue

                final_url = viewer.url
                full_html = "\n".join(html_parts)
                if not pdf_bytes:
                    pdf_bytes, ext = self._download_first_file(file_links)
                    if pdf_bytes:
                        how = "viewer-file"
                    elif not self.headed:
                        try:
                            pdf_bytes, ext, how = viewer.pdf(), "pdf", "print-pdf"
                        except Exception:
                            pdf_bytes = None

                safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", fax.fax_id)[:60]
                self._dump_html_for(viewer, f"fax_{safe}.html")
                text = (fax.label + "\n\n" + "\n\n".join(dict.fromkeys(text_parts))).strip()
            finally:
                try:
                    if viewer is not self.page:
                        viewer.close()
                except Exception:
                    pass

        note = (f"file via {how}" if pdf_bytes else "no file obtained") + f"; viewer={opened_via or 'n/a'}"
        return DocResult(text=text, url=final_url, html=full_html,
                         pdf_bytes=pdf_bytes, file_ext=ext, note=note)

    def _download_via_row_actions(self, fax: FaxItem) -> tuple[bytes | None, str, str]:
        """Click the row's gear/Export action and capture the downloaded file."""
        row = self._locate_full_row(fax)
        if row is None:
            return None, "pdf", ""
        toggle = self._find_actions_toggle(row)
        if toggle is None:
            return None, "pdf", ""
        try:
            toggle.scroll_into_view_if_needed(timeout=3000)
            toggle.click()
        except Exception:
            return None, "pdf", ""
        self.page.wait_for_timeout(450)

        item = None
        for frame in self.page.frames:
            try:
                cand = frame.get_by_text(
                    re.compile(r"\b(export|download|save\s*as\s*pdf|print\s*to\s*pdf|pdf)\b", re.I)
                ).first
                if cand.count() > 0 and cand.is_visible():
                    item = cand
                    break
            except Exception:
                continue
        if item is None:
            return None, "pdf", ""

        try:
            with self.page.expect_download(timeout=15000) as dl:
                item.click()
            download = dl.value
            dest = self.debug_dir / ("dl_" + re.sub(r"[^A-Za-z0-9]+", "_", fax.fax_id)[:40])
            download.save_as(str(dest))
            data = Path(dest).read_bytes()
            return data, _ext_from_bytes(data, download.suggested_filename), "row-export"
        except Exception:
            # Some Export actions open the file in a new tab instead of downloading.
            try:
                pg = self.context.pages[-1]
                if "pdf" in (pg.url or "").lower():
                    body = self.context.request.get(pg.url).body()
                    if body[:5] == b"%PDF-":
                        if pg is not self.page:
                            pg.close()
                        return body, "pdf", "row-export-tab"
            except Exception:
                pass
            return None, "pdf", ""

    def _locate_full_row(self, fax: FaxItem):
        """Locate the <tr> for this fax by its most distinctive text."""
        m = NAME_RE.search(fax.label or "")
        key = m.group(1) if m else ""
        if not key:
            m2 = re.search(r"Fax from \d+", fax.label or "")
            key = m2.group(0) if m2 else (fax.label or "")[:25]
        if not key.strip():
            return None
        for frame in self.page.frames:
            try:
                row = frame.locator("table tr", has_text=key).first
                if row and row.count() > 0:
                    return row
            except Exception:
                continue
        return None

    def _find_actions_toggle(self, row):
        """Find the per-row actions/gear control inside a row locator."""
        selectors = [
            "[class*=cog]", "[class*=gear]", "[class*=ellipsis]", "[class*=fa-bars]",
            "a.dropdown-toggle", "button.dropdown-toggle", "[data-toggle='dropdown']",
            "[title*='action' i]", "[title*='option' i]", "[title*='menu' i]",
            "img[src*='gear']", "img[src*='cog']", "img[src*='setting']",
        ]
        for sel in selectors:
            try:
                c = row.locator(sel).first
                if c.count() > 0:
                    return c
            except Exception:
                continue
        # Fallback: a clickable element in the last couple of cells (the gear column).
        try:
            c = row.locator("td:last-child a, td:last-child button, td:last-child [onclick], "
                            "td:nth-last-child(2) a, td:nth-last-child(2) [onclick]").first
            if c.count() > 0:
                return c
        except Exception:
            pass
        return None

    def _open_viewer(self, fax: FaxItem):
        """Return (page, how) for the opened document, or (None, '')."""
        # 1) Direct navigation to a known open URL.
        if fax.open_url:
            try:
                viewer = self.context.new_page()
                viewer.goto(fax.open_url, wait_until="domcontentloaded")
                return viewer, "url"
            except Exception:
                pass
        # 2) Click the row in the main page; capture a popup/new tab if it opens.
        try:
            loc = self._locate_row(fax)
            if loc is not None:
                try:
                    with self.context.expect_page(timeout=6000) as pop:
                        loc.click()
                    return pop.value, "click-popup"
                except Exception:
                    self.page.wait_for_timeout(800)
                    return self.page, "click-inline"
        except Exception:
            pass
        return None, ""

    def _locate_row(self, fax: FaxItem):
        snippet = (fax.label or "").split(" - ")[0][:30].strip()
        if not snippet:
            return None
        for frame in self.page.frames:
            try:
                row = frame.locator("table tr", has_text=snippet).first
                if row and row.count() > 0:
                    link = row.locator("a[href], [onclick]").first
                    return link if link.count() > 0 else row
            except Exception:
                continue
        return None

    def _file_links_in(self, html: str, base: str) -> list[str]:
        out = []
        for m in re.finditer(r"""(?is)(?:href|src|data|action)\s*=\s*["']([^"']+)["']""", html or ""):
            href = m.group(1)
            low = href.lower()
            if any(h in low for h in FILE_LINK_HINTS) and not any(d in low for d in LINK_DENY):
                out.append(urljoin(base, html_lib.unescape(href)))
        return out

    def _download_first_file(self, urls: list[str]) -> tuple[bytes | None, str]:
        for url in list(dict.fromkeys(urls))[:12]:
            try:
                resp = self.context.request.get(url, timeout=self.timeout_ms)
                body = resp.body()
                ctype = (resp.headers or {}).get("content-type", "").lower()
                if body[:5] == b"%PDF-" or "application/pdf" in ctype:
                    return body, "pdf"
                if "image/tiff" in ctype or body[:2] == b"II" or body[:2] == b"MM":
                    return body, "tiff"
                if "image/png" in ctype or body[:8] == b"\x89PNG\r\n\x1a\n":
                    return body, "png"
                if "image/jpeg" in ctype or body[:3] == b"\xff\xd8\xff":
                    return body, "jpg"
            except Exception:
                continue
        return None, "pdf"

    # -- discovery -----------------------------------------------------------
    def discovery_dump(self, probe_document: bool = True) -> Path:
        """Capture the real post-login structure (and how a document opens)."""
        report = self.debug_dir / "discovery_report.txt"
        self._load_all_rows()
        faxes = self._scan_all_frames()
        lines = [
            "ProviderFlow discovery report",
            "=============================",
            f"Generated: {datetime.now():%Y-%m-%d %H:%M:%S}",
            f"Final URL: {_redact(self.page.url)}",
            f"Page title: {self.page.title()!r}",
            f"Login form still present: {self._login_form_present()}",
            f"Frame count: {len(self.page.frames)}",
            f"Fax-like rows detected: {len(faxes)}",
            "",
            "First rows detected:",
        ]
        for fx in faxes[:8]:
            lines.append(f"  - id={fx.fax_id} doc_id={fx.doc_id or '-'} open={_redact(fx.open_url) or '-'}")
            lines.append(f"      label: {fx.label[:110]}")

        # Save the raw HTML of the first few rows so the open/link pattern is visible.
        for i, fx in enumerate(faxes[:5]):
            (self.debug_dir / f"row_{i}.html").write_text(fx.raw_html, encoding="utf-8", errors="ignore")

        for i, frame in enumerate(self.page.frames):
            try:
                (self.debug_dir / f"frame_{i}.html").write_text(frame.content(), encoding="utf-8", errors="ignore")
            except Exception:
                pass
            headers = self._frame_table_headers(frame)
            if headers:
                lines.append("")
                lines.append(f"[Frame {i}] headers: " + " | ".join(headers[:12]))

        # Probe the per-row gear/Export menu (how the user downloads the file).
        if probe_document and faxes:
            lines += ["", "Row actions (gear/Export) probe (first row):"]
            try:
                row = self._locate_full_row(faxes[0])
                toggle = self._find_actions_toggle(row) if row is not None else None
                lines.append(f"  gear/actions control found: {toggle is not None}")
                if toggle is not None:
                    toggle.click()
                    self.page.wait_for_timeout(500)
                    items = []
                    for frame in self.page.frames:
                        try:
                            items += frame.evaluate(
                                "() => Array.from(document.querySelectorAll('a,button,li,span,div'))"
                                ".filter(e => e.offsetParent !== null)"
                                ".map(e => ((e.innerText||'').trim().slice(0,40) + ' :: ' + "
                                "(e.getAttribute('href')||e.getAttribute('onclick')||'')))"
                                ".filter(t => /export|download|pdf|print|save|view/i.test(t))"
                            ) or []
                        except Exception:
                            continue
                    for it in list(dict.fromkeys(items))[:25]:
                        lines.append("    - " + _redact(it))
                    self._dump_html("row_actions_menu.html")
            except Exception as e:
                lines.append(f"  actions probe error: {e}")

        # Open the first document and record exactly how it behaves.
        if probe_document and faxes:
            lines += ["", "Document-open probe (first row):"]
            try:
                doc = self.fetch_document(faxes[0])
                lines.append(f"  note: {doc.note}")
                lines.append(f"  viewer url: {_redact(doc.url)}")
                lines.append(f"  file downloaded: {bool(doc.pdf_bytes)} ({doc.file_ext})")
                (self.debug_dir / "first_document_viewer.html").write_text(doc.html, encoding="utf-8", errors="ignore")
                if doc.pdf_bytes:
                    (self.debug_dir / f"first_document.{doc.file_ext}").write_bytes(doc.pdf_bytes)
            except Exception as e:
                lines.append(f"  probe error: {e}")

        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._safe_screenshot("discovery_fullpage.png", full_page=True)
        return report

    def _frame_table_headers(self, frame) -> list[str]:
        try:
            return frame.evaluate(
                "() => Array.from(document.querySelectorAll('th')).map(e => "
                "(e.innerText||'').replace(/\\s+/g,' ').trim()).filter(Boolean)"
            ) or []
        except Exception:
            return []

    # -- helpers -------------------------------------------------------------
    def _first_locator(self, selectors: list[str]):
        for sel in selectors:
            try:
                loc = self.page.locator(sel).first
                if loc.count() > 0:
                    return loc
            except Exception:
                continue
        return None

    def _login_form_present(self) -> bool:
        try:
            for frame in self.page.frames:
                if frame.locator("input[type='password']").count() > 0:
                    return True
        except Exception:
            pass
        return False

    def _page_signature(self) -> str:
        rows = self._scan_all_frames()
        return "|".join(_norm(r.label) for r in rows[:5])

    def _wait_idle(self) -> None:
        for state in ("load", "networkidle"):
            try:
                self.page.wait_for_load_state(state, timeout=self.timeout_ms)
            except Exception:
                pass

    def _dump_html(self, name: str) -> None:
        self._dump_html_for(self.page, name)

    def _dump_html_for(self, page, name: str) -> None:
        try:
            (self.debug_dir / name).write_text(page.content(), encoding="utf-8", errors="ignore")
        except Exception:
            pass

    def _safe_screenshot(self, name: str, full_page: bool = False) -> None:
        try:
            self.page.screenshot(path=str(self.debug_dir / name), full_page=full_page)
        except Exception:
            pass


def _first_match(patterns: list[str], text: str) -> str:
    for p in patterns:
        m = re.search(p, text or "", flags=re.I)
        if m:
            return m.group(1)
    return ""


def _ext_from_bytes(data: bytes, suggested: str = "") -> str:
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:2] in (b"II", b"MM"):
        return "tiff"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if suggested and "." in suggested:
        ext = suggested.rsplit(".", 1)[-1].lower()
        if 1 <= len(ext) <= 5 and ext.isalnum():
            return ext
    return "pdf"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def _redact(url: str) -> str:
    return re.sub(r"(?i)(sessionkey|password|username|token|key|docid|documentid|id)=([^&\s]+)",
                  r"\1=<redacted>", url or "")
