"""Live check of the web-search and browser tools.

Search and page reading run against the real internet. The browser runs against
a throwaway site this script serves on 127.0.0.1 — a page with a link, a form, a
dropdown, a download and an upload — so clicking and typing are checked against
something that cannot change under us. Nothing is bought, sent or deleted.

    .venv\\Scripts\\python.exe scripts\\verify_web.py           headless
    .venv\\Scripts\\python.exe scripts\\verify_web.py --headed  watch it work
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import approvals
from agent.config import load_config
from agent.tools import browser, localfiles
from agent.tools import websearch as ws

PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(label)
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{f' — {detail}' if detail else ''}")


def section(title: str) -> None:
    print(f"\n{title}")


# ------------------------------------------------------------- the test site

PAGE_ONE = """<!doctype html><html><head><title>Lumen browser test</title></head>
<body>
<h1>Report library</h1>
<p>This page exists so the browser tools can be checked against something stable.</p>
<a href="/page2">Open the archive</a>
<form action="/search" method="get">
  <label for="q">Search reports</label>
  <input id="q" name="q" type="text" placeholder="Type a report name">
  <label for="year">Year</label>
  <select id="year" name="year">
    <option value="2024">2024</option>
    <option value="2025">2025</option>
    <option value="2026">2026</option>
  </select>
  <button type="submit">Search</button>
</form>
<a href="/report.csv" download>Download the latest report</a>
<button onclick="document.title='bought'">Buy now</button>
<form action="/upload" method="post" enctype="multipart/form-data">
  <label for="file">Attach a file</label>
  <input id="file" name="file" type="file">
</form>
<div style="height:1800px"></div>
<p id="deep">You reached the bottom of the page.</p>
</body></html>"""

PAGE_TWO = """<!doctype html><html><head><title>Archive</title></head>
<body><h1>Archive</h1><p>Nothing has been archived yet.</p>
<a href="/">Back to the library</a></body></html>"""

REPORT_CSV = "quarter,revenue\nQ1,120000\nQ2,148000\n"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    def _send(self, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/page2":
            self._send(PAGE_TWO.encode(), "text/html; charset=utf-8")
        elif path == "/report.csv":
            self._send(
                REPORT_CSV.encode(),
                "text/csv",
                {"Content-Disposition": 'attachment; filename="report.csv"'},
            )
        elif path == "/search":
            self._send(
                f"<h1>Results</h1><p>You searched: {self.path}</p>".encode(),
                "text/html; charset=utf-8",
            )
        else:
            self._send(PAGE_ONE.encode(), "text/html; charset=utf-8")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        self.rfile.read(length)
        self._send(b"<h1>Uploaded</h1>", "text/html; charset=utf-8")


# --------------------------------------------------------------------- search


def test_search() -> None:
    section("Web search (live)")
    config = load_config()
    order = ws.provider_order(config)
    print(f"  info  provider order: {' -> '.join(order)}")

    try:
        result = ws.web_search("anthropic claude api pricing", max_results=5)
    except Exception as exc:
        check("a plain search returns results", False, f"{type(exc).__name__}: {exc}")
        return

    check("a plain search returns results", result["count"] > 0, f"{result['count']} via {result['provider']}")
    first = result["results"][0]
    check("every result carries a URL", all(r["url"].startswith("http") for r in result["results"]))
    check("results carry a title and snippet", bool(first["title"]))
    check(
        "the result says the facts came off the web",
        "web" in result.get("source", "").lower(),
    )

    try:
        scoped = ws.web_search("release notes", max_results=3, site="docs.python.org")
        hosts = {r["url"].split("/")[2] for r in scoped["results"]}
        check("site: narrows to one domain", all("python.org" in h for h in hosts), str(hosts))
    except Exception as exc:
        check("site: narrows to one domain", False, f"{type(exc).__name__}: {exc}")

    try:
        recent = ws.web_search("ai news", max_results=3, recency="week")
        check("a recency window is accepted", recent["count"] > 0 and recent["recency"] == "week")
    except Exception as exc:
        check("a recency window is accepted", False, f"{type(exc).__name__}: {exc}")

    try:
        ws.web_search("anything", recency="fortnight")
        check("an unknown recency window is refused", False)
    except ValueError:
        check("an unknown recency window is refused", True)


def test_fetch() -> None:
    section("Reading pages (live)")
    try:
        page = ws.web_fetch("https://example.com", max_chars=2_000)
    except Exception as exc:
        check("a page can be read as text", False, f"{type(exc).__name__}: {exc}")
        return

    check("a page can be read as text", "example domain" in page["content"].lower())
    check("the title comes back", "Example" in page["title"], page["title"])
    check("the final URL is reported", page["url"].startswith("https://example.com"))

    linked = ws.web_fetch("https://example.com", include_links=True)
    check("links can be listed for navigation", isinstance(linked.get("links"), list))

    for label, url in (
        ("a private address is refused", "http://192.168.0.1/admin"),
        ("localhost is refused", "http://127.0.0.1:9/"),
        ("a file:// URL is refused", "file:///C:/Windows/win.ini"),
    ):
        try:
            ws.web_fetch(url)
            check(label, False, "it was fetched")
        except ValueError:
            check(label, True)
        except Exception as exc:
            check(label, False, f"{type(exc).__name__}: {exc}")

    try:
        ws.web_fetch("https://example.com/definitely-not-here-4831")
        check("a 404 is reported, not swallowed", False)
    except RuntimeError as exc:
        check("a 404 is reported, not swallowed", "404" in str(exc), str(exc)[:80])


# -------------------------------------------------------------------- browser


def test_url_policy() -> None:
    section("Browser URL policy")
    check("a bare host gets https://", browser.check_url("example.com") == "https://example.com")
    for label, url in (
        ("javascript: URLs are refused", "javascript:alert(1)"),
        ("file:// URLs are refused", "file:///C:/Windows/win.ini"),
    ):
        try:
            browser.check_url(url)
            check(label, False)
        except ValueError:
            check(label, True)

    os.environ["AGENT_BROWSER_ALLOWED_DOMAINS"] = "example.com"
    try:
        browser.check_url("https://docs.example.com/x")
        check("subdomains of an allowed domain pass", True)
        try:
            browser.check_url("https://elsewhere.test/")
            check("a domain outside the allowlist is refused", False)
        except ValueError:
            check("a domain outside the allowlist is refused", True)
    finally:
        del os.environ["AGENT_BROWSER_ALLOWED_DOMAINS"]


def test_sensitivity() -> None:
    section("Consequential-action detection")
    risky = [
        ("Buy now", "https://shop.test/item"),
        ("Place order", "https://shop.test/item"),
        ("Delete my account", "https://app.test/settings"),
        ("Submit application", "https://jobs.test/apply"),
        ("Confirm payment", "https://bank.test/transfer"),
        ("Continue", "https://shop.test/checkout/step2"),
    ]
    check(
        "purchases, deletions and submissions are caught",
        all(browser.sensitivity(text, url) for text, url in risky),
        str([t for t, u in risky if not browser.sensitivity(t, u)]),
    )
    routine = [
        ("Next page", "https://news.test/"),
        ("Open the archive", "https://news.test/"),
        ("Accept cookies", "https://news.test/"),
        ("Search", "https://news.test/"),
        ("Sign in", "https://news.test/"),
    ]
    check(
        "ordinary clicks are not flagged",
        not any(browser.sensitivity(text, url) for text, url in routine),
        str([t for t, u in routine if browser.sensitivity(t, u)]),
    )


def test_browser(base: str) -> None:
    section("Browser automation (local test site)")
    workspace = load_config().workspace

    try:
        page = browser.browser_open(base)
    except browser.BrowserUnavailable as exc:
        check("the browser starts", False, str(exc).splitlines()[0])
        print("     run: .venv\\Scripts\\python.exe -m playwright install chromium")
        return
    check("the browser opens a page", page["title"] == "Lumen browser test", page.get("title", ""))
    check("page text is returned", "Report library" in page["text"])
    check("interactive elements are numbered", page["element_count"] >= 6, str(page["element_count"]))

    names = {e["name"] for e in page["elements"]}
    check("the link is listed", any("archive" in n.lower() for n in names))
    check("the dropdown lists its options", any("2026" in (e.get("options") or []) for e in page["elements"]))

    # --- navigating -------------------------------------------------------
    clicked = browser.browser_click("Open the archive")
    check("clicking a link navigates", clicked["title"] == "Archive", clicked.get("title", ""))
    back = browser.browser_navigate("back")
    check("going back returns to the first page", back["title"] == "Lumen browser test")

    # --- forms ------------------------------------------------------------
    typed = browser.browser_type("Search reports", "quarterly revenue")
    field = next((e for e in typed["elements"] if e.get("tag") == "input" and e.get("type") == "text"), {})
    check("typing lands in the field", field.get("value") == "quarterly revenue", str(field.get("value")))

    chosen = browser.browser_select("Year", "2026")
    check("a dropdown option can be chosen", "2026" in chosen["action"], chosen.get("action", ""))

    submitted = browser.browser_type("Search reports", "q3", submit=True)
    check("pressing Enter submits the form", "Results" in submitted["text"], submitted.get("title", ""))
    browser.browser_navigate("back")

    # --- scrolling --------------------------------------------------------
    top = browser.browser_read(max_chars=500)
    check("the page starts at the top", top["scroll"]["y"] == 0, str(top["scroll"]))
    bottom = browser.browser_scroll("bottom")
    check("scrolling moves down the page", bottom["scroll"]["y"] > 0, str(bottom["scroll"]["y"]))
    browser.browser_scroll("top")

    # --- refs, and what happens when the page moves on --------------------
    fresh = browser.browser_read()
    ref = next(e["ref"] for e in fresh["elements"] if "archive" in e["name"].lower())
    by_ref = browser.browser_click(str(ref))
    check("an element can be clicked by ref", by_ref["title"] == "Archive")
    try:
        browser.browser_click(str(ref))
        check("a stale ref is reported, not clicked blind", False, "it clicked something")
    except browser.PageChanged as exc:
        check("a stale ref is reported, not clicked blind", "browser_read" in str(exc))
    browser.browser_navigate("back")

    try:
        browser.browser_click("a button that does not exist")
        check("a missing target is reported", False)
    except browser.PageChanged:
        check("a missing target is reported", True)

    # --- downloading ------------------------------------------------------
    got = browser.browser_download("Download the latest report")
    check("a download reaches the workspace", got.get("status") == "downloaded", str(got)[:90])
    if got.get("status") == "downloaded":
        saved = workspace / got["path"]
        check("the downloaded file is on disk", saved.exists() and saved.stat().st_size > 0)
        check("it can be read back with file_read", "Q2,148000" in localfiles.file_read(got["path"])["content"])

    direct = browser.browser_download(url=f"{base}/report.csv", filename="direct.csv")
    check("a direct URL can be downloaded", direct.get("status") == "downloaded", str(direct)[:90])

    # --- uploading --------------------------------------------------------
    localfiles.file_write("verify-web/upload-me.txt", "hello from the agent\n")
    uploaded = browser.browser_upload("Attach a file", "verify-web/upload-me.txt")
    check("a workspace file can be attached", uploaded.get("status") == "attached", str(uploaded)[:90])
    try:
        browser.browser_upload("Attach a file", "../../../Windows/win.ini")
        check("uploading from outside the workspace is refused", False)
    except (ValueError, FileNotFoundError):
        check("uploading from outside the workspace is refused", True)

    # --- screenshot -------------------------------------------------------
    shot = browser.browser_screenshot("verify-web.png")
    check("a screenshot is saved", (workspace / shot["path"]).exists(), shot.get("path", ""))

    # --- the confirmation gate -------------------------------------------
    asked: list[tuple[str, dict]] = []

    def deny(action: str, details: dict) -> bool:
        asked.append((action, details))
        return False

    with approvals.bind(deny):
        result = browser.browser_click("Buy now")
    check("a purchase-looking click asks first", len(asked) == 1, str(asked))
    check("refusing it stops the click", result.get("status") == "cancelled", str(result)[:80])
    check(
        "the reason is shown to the user",
        bool(asked) and "purchase" in (asked[0][1].get("why") or ""),
        str(asked[0][1].get("why")) if asked else "",
    )

    approved: list[str] = []

    def allow(action: str, details: dict) -> bool:
        approved.append(action)
        return True

    with approvals.bind(allow):
        browser.browser_click("Open the archive")
    check("an ordinary click is not gated", approved == [], str(approved))

    with approvals.bind(allow):
        browser.browser_navigate("back")
        bought = browser.browser_click("Buy now")
    check("approving it lets the click through", approved == ["browser_click"], str(approved))
    check("and the click actually happened", bought.get("title") == "bought", str(bought.get("title")))

    check("with nobody to ask, it refuses", browser.browser_click("Buy now")["status"] == "cancelled")

    # --- shutting down ----------------------------------------------------
    closed = browser.browser_close()
    check("the browser closes", closed["status"] == "closed")
    check("closing an already-closed browser is fine", browser.browser_close()["status"] == "closed")

    _clean_up(workspace, [got, direct, shot])


def _clean_up(workspace: Path, artefacts: list[dict]) -> None:
    """Leave the workspace as we found it — these were props, not the user's files."""
    for item in artefacts:
        path = item.get("path")
        if path:
            (workspace / path).unlink(missing_ok=True)
    (workspace / "verify-web" / "upload-me.txt").unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        (workspace / "verify-web").rmdir()


def main() -> int:
    print("Lumen OS — web search and browser check")
    # Read per call by load_config(), so setting it here is early enough.
    os.environ.setdefault(
        "AGENT_BROWSER_HEADLESS", "false" if "--headed" in sys.argv else "true"
    )
    test_search()
    test_fetch()
    test_url_policy()
    test_sensitivity()

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        test_browser(f"http://127.0.0.1:{server.server_port}")
    finally:
        browser.reset_session()
        server.shutdown()
        server.server_close()

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for failure in FAILED:
        print(f"  FAILED: {failure}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
