"""Real-browser ProviderFlow automation (Playwright / Chromium).

WHY A REAL BROWSER:
ProviderFlow is an old PHP portal that renders its pending-fax list with
client-side JavaScript/AJAX (e.g. batchtable.php). A plain HTTP client
(requests/urllib) downloads the page *shell* before that JS runs, so it sees
zero rows -> "Found 0 pending faxes", even though Chrome clearly shows many.
Driving a real Chromium browser lets the JS run and the rows appear, exactly
as a human sees them. We then read the *rendered* DOM, across all frames
(old PHP loves framesets), which is the robust fix.

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


# Patterns that identify a "real" fax row and its session key.
NAME_RE = re.compile(r"\b([A-Z][A-Za-z'.\-]+\s*,\s*[A-Z][A-Za-z'.\-]+)")
SESSION_PATTERNS = [
    r"loadbatch\(['\"]([A-Fa-f0-9]{12,})['\"]",
    r"displaysession=([A-Fa-f0-9]{12,})",
    r"batchselector_([A-Fa-f0-9]{12,})",
    r"comments_([A-Fa-f0-9]{12,})",
    r"sessionkey=([A-Za-z0-9]{12,})",
]
DETAIL_LINK_HINTS = ("quickflow", "sessionkey", "viewfile", "displaysession", "docid", "document")
PDF_LINK_HINTS = ("pdf", "print", "download", "viewfile", "file=", "getfile")

# JS that returns lightweight info for every table row in a frame.
ROW_SCAN_JS = """
() => {
  const out = [];
  for (const r of Array.from(document.querySelectorAll('tr'))) {
    const text = (r.innerText || '').replace(/\\s+/g, ' ').trim();
    if (text.length < 3 || text.length > 600) continue;
    const a = r.querySelector('a[href]');
    const href = a ? a.getAttribute('href') : '';
    const clickEl = r.matches('[onclick]') ? r : r.querySelector('[onclick]');
    const onclick = clickEl ? (clickEl.getAttribute('onclick') || '') : '';
    let html = r.outerHTML || '';
    if (html.length > 2500) html = html.slice(0, 2500);
    out.push({ text, href, onclick, html });
  }
  return out;
}
"""


@dataclass
class FaxItem:
    fax_id: str
    label: str               # human-readable row text, e.g. "WILSON, NANCY referral"
    session_key: str = ""
    detail_url: str = ""
    frame_url: str = ""
    raw_html: str = field(default="", repr=False)


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

        # GET the portal. ProviderFlow's real login form posts to login.php and
        # carries hidden fields (viewfile/viewmode/indexone) — a real browser
        # submits those automatically, so we only fill the visible inputs.
        self.page.goto(self.base_url + "/index.php", wait_until="domcontentloaded")
        self._safe_screenshot("01_login_page.png")

        user_box = self._first_locator(["input[name='username']", "input#username",
                                        "input[type='text']"])
        pass_box = self._first_locator(["input[name='password']", "input#password",
                                        "input[type='password']"])
        if not user_box or not pass_box:
            self._dump_html("01_login_page.html")
            raise RuntimeError("Could not find the ProviderFlow username/password fields on the login page.")

        user_box.fill(self.username)
        pass_box.fill(self.password)

        # Submit via the obvious button, falling back to Enter in the password box.
        submit = self._first_locator(["input[type='submit']", "button[type='submit']",
                                      "button:has-text('Log')", "input[value*='Log' i]"])
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
        """Read every pending fax across all pages from the rendered DOM."""
        self._ensure_pending_view()
        self._wait_for_rows()

        seen: set[str] = set()
        faxes: list[FaxItem] = []
        for page_no in range(1, max_pages + 1):
            self._dump_html(f"pending_page_{page_no}.html")
            new_on_page = 0
            for item in self._scan_all_frames():
                key = item.session_key or _norm(item.label)
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
        blob = " ".join([row.get("html", ""), row.get("onclick", ""), row.get("href", "")])
        session_key = _first_match(SESSION_PATTERNS, blob)
        name_match = NAME_RE.search(text)
        href = (row.get("href") or "").strip()
        detail_link = href if any(h in href.lower() for h in DETAIL_LINK_HINTS) else ""

        # A row is a fax only if it names a patient, carries a session key, or
        # links to a document. This filters out nav/header/layout rows.
        if not (name_match or session_key or detail_link):
            return None

        detail_url = ""
        if detail_link:
            detail_url = urljoin(frame_url, html_lib.unescape(detail_link))
        elif session_key:
            detail_url = urljoin(self.base_url + "/", f"quickflow/index.php?sessionkey={session_key}")

        if name_match:
            fax_id = re.sub(r"[^A-Za-z0-9]+", "_", name_match.group(1)).strip("_")[:60]
        else:
            fax_id = session_key or str(abs(hash(text)))[:10]

        return FaxItem(fax_id=fax_id, label=text, session_key=session_key,
                       detail_url=detail_url, frame_url=frame_url, raw_html=row.get("html", ""))

    def _ensure_pending_view(self) -> None:
        """If no fax rows are visible yet, try clicking a 'Pending' tab/link."""
        if self._scan_all_frames():
            return
        for frame in self.page.frames:
            try:
                link = frame.get_by_role("link", name=re.compile(r"pending", re.I)).first
                if link and link.count() > 0:
                    link.click()
                    self._wait_idle()
                    return
            except Exception:
                continue

    def _wait_for_rows(self) -> None:
        """Poll for the AJAX-rendered rows to appear."""
        deadline = self.timeout_ms
        step = 750
        waited = 0
        while waited < deadline:
            if self._scan_all_frames():
                return
            self.page.wait_for_timeout(step)
            waited += step
        # No rows found within timeout — caller logs "Found 0"; discovery dump
        # (saved alongside) captures the real DOM so selectors can be confirmed.

    def _go_to_next_page(self) -> bool:
        """Best-effort pagination: click a 'Next' control if one exists."""
        before = self._page_signature()
        for frame in self.page.frames:
            for getter in (
                lambda f: f.get_by_role("link", name=re.compile(r"^\s*(next|more|›|»)\s*$", re.I)),
                lambda f: f.get_by_role("button", name=re.compile(r"^\s*(next|more|›|»)\s*$", re.I)),
                lambda f: f.locator("a[href*='selectedpage'], a[href*='pagenum'], a[href*='page=']"),
            ):
                try:
                    loc = getter(frame).first
                    if loc and loc.count() > 0 and loc.is_enabled():
                        href = (loc.get_attribute("href") or "")
                        if href.strip().endswith("#"):
                            continue
                        loc.click()
                        self._wait_idle()
                        if self._page_signature() != before:
                            return True
                except Exception:
                    continue
        return False

    # -- fax detail + pdf ----------------------------------------------------
    def open_fax_text(self, fax: FaxItem) -> tuple[str, str, str]:
        """Open the fax; return (document_text, final_url, detail_html). Best-effort."""
        url = fax.detail_url
        if not url:
            return fax.label, self.page.url, ""
        try:
            detail = self.context.new_page()
            detail.goto(url, wait_until="domcontentloaded")
            detail.wait_for_timeout(800)
            text_parts: list[str] = []
            html_parts: list[str] = []
            for frame in detail.frames:
                try:
                    for ta in frame.locator("textarea").all():
                        val = (ta.input_value() or "").strip()
                        if len(val) > 20:
                            text_parts.append(val)
                    body = frame.evaluate("() => document.body ? document.body.innerText : ''") or ""
                    body = re.sub(r"\s+\n", "\n", body).strip()
                    if len(body) > 40:
                        text_parts.append(body[:8000])
                    html_parts.append((frame.content() or "")[:20000])
                except Exception:
                    continue
            final_url = detail.url
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", fax.fax_id)[:60]
            self._dump_html_for(detail, f"fax_{safe}.html")
            detail.close()
            text = (fax.label + "\n\n" + "\n\n".join(dict.fromkeys(text_parts))).strip()
            return text, final_url, "\n".join(html_parts)
        except Exception:
            return fax.label, url, ""

    def try_download_pdf(self, fax: FaxItem, detail_html: str = "") -> bytes | None:
        """Fetch the PDF using the authenticated browser session, if findable."""
        candidates: list[str] = []
        base = fax.detail_url or self.page.url
        for m in re.finditer(r"""(?is)(?:href|src|action)\s*=\s*["']([^"']+)["']""", detail_html or ""):
            href = m.group(1)
            if any(h in href.lower() for h in PDF_LINK_HINTS) and "logout" not in href.lower():
                candidates.append(urljoin(base, html_lib.unescape(href)))
        if fax.detail_url:
            candidates.append(fax.detail_url)

        for url in list(dict.fromkeys(candidates))[:8]:
            try:
                resp = self.context.request.get(url, timeout=self.timeout_ms)
                body = resp.body()
                ctype = (resp.headers or {}).get("content-type", "").lower()
                if body[:5] == b"%PDF-" or "application/pdf" in ctype:
                    return body
            except Exception:
                continue
        return None

    # -- discovery -----------------------------------------------------------
    def discovery_dump(self) -> Path:
        """Capture the real post-login structure so selectors can be confirmed.

        This is the artifact to send back if the tool still finds 0 faxes:
        it contains the actual rendered HTML of every frame, table headers,
        sample rows, candidate links, and a screenshot.
        """
        report = self.debug_dir / "discovery_report.txt"
        lines = [
            "ProviderFlow discovery report",
            "=============================",
            f"Generated: {datetime.now():%Y-%m-%d %H:%M:%S}",
            f"Final URL: {_redact(self.page.url)}",
            f"Page title: {self.page.title()!r}",
            f"Login form still present: {self._login_form_present()}",
            f"Frame count: {len(self.page.frames)}",
            "",
        ]
        faxes = self._scan_all_frames()
        lines.append(f"Fax-like rows detected (all frames): {len(faxes)}")
        for fx in faxes[:10]:
            lines.append(f"  - id={fx.fax_id} key={fx.session_key or '-'} :: {fx.label[:90]}")
        lines.append("")

        for i, frame in enumerate(self.page.frames):
            safe = f"frame_{i}"
            try:
                content = frame.content()
                (self.debug_dir / f"{safe}.html").write_text(content, encoding="utf-8", errors="ignore")
            except Exception:
                content = ""
            headers = self._frame_table_headers(frame)
            links = self._frame_candidate_links(frame)
            lines.append(f"[Frame {i}] url={_redact(frame.url)}")
            if headers:
                lines.append("  Table headers: " + " | ".join(headers[:12]))
            lines.append(f"  Candidate links (quickflow/print/pdf/...): {len(links)}")
            for href in links[:15]:
                lines.append(f"    - {_redact(href)}")
            lines.append("")

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

    def _frame_candidate_links(self, frame) -> list[str]:
        try:
            raw = frame.evaluate(
                "() => Array.from(document.querySelectorAll('a[href],[onclick]')).map(e => "
                "(e.getAttribute('href')||'') + ' ' + (e.getAttribute('onclick')||''))"
            ) or []
        except Exception:
            return []
        hints = DETAIL_LINK_HINTS + PDF_LINK_HINTS + ("sessionkey", "batchtable")
        out = []
        for r in raw:
            low = r.lower()
            if any(h in low for h in hints):
                out.append(r.strip())
        return out

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


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def _redact(url: str) -> str:
    return re.sub(r"(?i)(sessionkey|password|username|token|key|docid|id)=([^&\s]+)",
                  r"\1=<redacted>", url or "")
