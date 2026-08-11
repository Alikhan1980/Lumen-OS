"""Web search and page reading.

Two tools. `web_search` asks a search engine a question and returns ranked
results with links; `web_fetch` opens one of those links and returns the page as
readable text. Between them the agent can answer from the live web and say where
each fact came from.

Search goes through whichever provider is configured — Brave, Tavily, or Google
Programmable Search when a key is present, DuckDuckGo when none is, so search
works on a fresh install with nothing to set up. Every provider is normalised to
the same small record, so the model sees one shape however the answer was
sourced, and a provider that errors or rate-limits falls through to the next one
rather than failing the turn.

Nothing here logs in anywhere or executes page scripts. For a site that needs a
session, a click, or a form, use the browser tools instead.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ..config import Config, load_config
from ..logs import logger
from ..registry import obj, tool

GROUP = "search"
log = logger("search")

# Long enough for a slow search API, short enough that a wedged provider does
# not hold up the turn — there is a fallback waiting behind it.
SEARCH_TIMEOUT = httpx.Timeout(20.0, connect=8.0)
FETCH_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

DEFAULT_RESULTS = 6
MAX_RESULTS = 20
DEFAULT_FETCH_CHARS = 15_000
MAX_FETCH_CHARS = 50_000
# A page bigger than this is not prose; refuse it rather than pull it into RAM.
MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024

# Sites block the default python-httpx agent. Identify honestly as a browser
# engine — this is the same string Playwright's Chromium sends.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

RECENCY = ("day", "week", "month", "year")

# Per-provider spelling of the same idea.
_FRESHNESS = {
    "brave": {"day": "pd", "week": "pw", "month": "pm", "year": "py"},
    "duckduckgo": {"day": "d", "week": "w", "month": "m", "year": "y"},
    "google": {"day": "d1", "week": "w1", "month": "m1", "year": "y1"},
    "tavily": {"day": 1, "week": 7, "month": 30, "year": 365},
}


class SearchError(RuntimeError):
    """A provider could not answer. Another one may still be able to."""


@dataclass
class Result:
    title: str
    url: str
    snippet: str
    published: str | None = None

    def as_dict(self) -> dict:
        out = {"title": self.title, "url": self.url, "snippet": self.snippet}
        if self.published:
            out["published"] = self.published
        return out


def _clean(text: str | None, limit: int = 400) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _client(timeout: httpx.Timeout = SEARCH_TIMEOUT) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
    )


# ------------------------------------------------------------------ providers


def _brave(config: Config, query: str, count: int, recency: str | None) -> list[Result]:
    params: dict = {"q": query, "count": count}
    if recency:
        params["freshness"] = _FRESHNESS["brave"][recency]
    with _client() as client:
        response = client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params=params,
            headers={"Accept": "application/json", "X-Subscription-Token": config.brave_api_key},
        )
    if response.status_code == 401:
        raise SearchError("Brave rejected BRAVE_SEARCH_API_KEY")
    if response.status_code == 429:
        raise SearchError("Brave rate limit reached")
    response.raise_for_status()
    web = (response.json().get("web") or {}).get("results") or []
    return [
        Result(
            title=_clean(item.get("title"), 200),
            url=item.get("url", ""),
            snippet=_clean(item.get("description")),
            published=item.get("page_age") or item.get("age"),
        )
        for item in web
        if item.get("url")
    ]


def _tavily(config: Config, query: str, count: int, recency: str | None) -> list[Result]:
    payload: dict = {"query": query, "max_results": count, "search_depth": "basic"}
    if recency:
        payload["days"] = _FRESHNESS["tavily"][recency]
        payload["topic"] = "news" if recency in {"day", "week"} else "general"
    with _client() as client:
        response = client.post(
            "https://api.tavily.com/search",
            json=payload,
            headers={"Authorization": f"Bearer {config.tavily_api_key}"},
        )
    if response.status_code in {401, 403}:
        raise SearchError("Tavily rejected TAVILY_API_KEY")
    if response.status_code == 429:
        raise SearchError("Tavily rate limit reached")
    response.raise_for_status()
    return [
        Result(
            title=_clean(item.get("title"), 200),
            url=item.get("url", ""),
            snippet=_clean(item.get("content")),
            published=item.get("published_date"),
        )
        for item in response.json().get("results", [])
        if item.get("url")
    ]


def _google(config: Config, query: str, count: int, recency: str | None) -> list[Result]:
    params: dict = {
        "key": config.google_search_api_key,
        "cx": config.google_search_engine_id,
        "q": query,
        # The Custom Search API caps a single page at 10.
        "num": min(count, 10),
    }
    if recency:
        params["dateRestrict"] = _FRESHNESS["google"][recency]
    with _client() as client:
        response = client.get("https://www.googleapis.com/customsearch/v1", params=params)
    if response.status_code in {400, 403}:
        detail = _clean((response.json().get("error") or {}).get("message"), 160)
        raise SearchError(f"Google Programmable Search refused the call: {detail}")
    response.raise_for_status()
    return [
        Result(
            title=_clean(item.get("title"), 200),
            url=item.get("link", ""),
            snippet=_clean(item.get("snippet")),
        )
        for item in response.json().get("items", [])
        if item.get("link")
    ]


def _duckduckgo(config: Config, query: str, count: int, recency: str | None) -> list[Result]:
    """No key, no account. The default, and the last fallback."""
    del config
    try:
        from ddgs import DDGS
    except ImportError as exc:  # pragma: no cover - dependency is in requirements
        raise SearchError(
            "the ddgs package is not installed — run: pip install -r requirements.txt"
        ) from exc

    try:
        rows = DDGS().text(
            query,
            max_results=count,
            timelimit=_FRESHNESS["duckduckgo"][recency] if recency else None,
        )
    except Exception as exc:
        raise SearchError(f"DuckDuckGo did not answer ({type(exc).__name__})") from exc

    return [
        Result(
            title=_clean(row.get("title"), 200),
            url=row.get("href") or row.get("url") or "",
            snippet=_clean(row.get("body")),
        )
        for row in rows
        if row.get("href") or row.get("url")
    ]


_PROVIDERS = {
    "brave": _brave,
    "tavily": _tavily,
    "google": _google,
    "duckduckgo": _duckduckgo,
}


def _configured(config: Config, name: str) -> bool:
    if name == "brave":
        return bool(config.brave_api_key)
    if name == "tavily":
        return bool(config.tavily_api_key)
    if name == "google":
        return bool(config.google_search_api_key and config.google_search_engine_id)
    return True  # duckduckgo needs nothing


def provider_order(config: Config) -> list[str]:
    """Which providers to try, best first.

    An explicit choice goes first but is not exclusive: if the chosen one is
    down, answering from another beats not answering.
    """
    ready = [name for name in _PROVIDERS if _configured(config, name)]
    chosen = config.search_provider
    if chosen and chosen in ready:
        return [chosen] + [name for name in ready if name != chosen]
    if chosen:
        # Named a provider whose key is missing. Say so once, then carry on.
        log.warning("AGENT_SEARCH_PROVIDER=%s has no key configured; falling back", chosen)
    return ready


# ---------------------------------------------------------------- URL safety

# The scheme exactly as the caller wrote it. The lookahead keeps
# "localhost:8080" a host and a port rather than a scheme called "localhost".
_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):(?!\d)")


def _blocked_host(host: str) -> str | None:
    """Refuse anything that is not a public web address.

    Page content, and any URL the agent found inside it, is untrusted input —
    this is what stops a link in an email from turning the agent into a probe of
    the machine's own network.
    """
    if not host:
        return "the URL has no host"
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return f"{host} does not resolve"
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
            return f"{host} resolves to a private address ({address}), which is not reachable here"
    return None


def normalise_url(raw: str, *, allow_private: bool = False) -> str:
    """Validate a URL for fetching, adding https:// when the scheme is missing."""
    candidate = (raw or "").strip()
    if not candidate:
        raise ValueError("no URL given")
    # The scheme has to be judged as written: "javascript:alert(1)" carries no
    # "//", so prefixing https:// would turn it into something that parses.
    scheme = _SCHEME.match(candidate)
    if scheme and scheme.group(1).lower() not in {"http", "https"}:
        raise ValueError(
            f"only http and https URLs can be fetched, not {scheme.group(1)!r}:"
        )
    if "://" not in candidate:
        candidate = "https://" + candidate
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"only http and https URLs can be fetched, not {parsed.scheme!r}")
    if not allow_private:
        reason = _blocked_host(parsed.hostname or "")
        if reason:
            raise ValueError(f"refusing to fetch: {reason}")
    return candidate


# ------------------------------------------------------------ text extraction

_BLANKS = re.compile(r"\n\s*\n\s*\n+")
_STRIP_TAGS = ("script", "style", "noscript", "template", "svg", "iframe", "form")
_CHROME_TAGS = ("nav", "header", "footer", "aside")


def extract_text(html: str) -> tuple[str, str, list[dict]]:
    """Return (title, readable text, links) for a page.

    Prefers <main>/<article> when the page has one, which drops navigation,
    cookie banners and footers without guessing at class names.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:  # pragma: no cover - dependency is in requirements
        raise RuntimeError(
            "beautifulsoup4 is not installed — run: pip install -r requirements.txt"
        ) from exc

    soup = BeautifulSoup(html, "html.parser")
    title = _clean(soup.title.get_text() if soup.title else "", 200)

    for tag in soup(list(_STRIP_TAGS)):
        tag.decompose()

    body = soup.find("main") or soup.find("article") or soup.body or soup
    # Only trust main/article when it actually holds the page: some sites wrap a
    # sidebar in <main> and put the story beside it.
    if body is not soup.body and len(body.get_text(strip=True)) < 200 and soup.body:
        body = soup.body

    links = [
        {"text": _clean(a.get_text(), 120), "href": a["href"]}
        for a in body.find_all("a", href=True)
        if a.get_text(strip=True) and not a["href"].startswith("javascript:")
    ]

    for tag in body(list(_CHROME_TAGS)):
        tag.decompose()

    text = body.get_text("\n", strip=True)
    return title, _BLANKS.sub("\n\n", text).strip(), links


def _save_binary(response: httpx.Response, url: str, filename: str | None) -> dict:
    """A non-text response is a file, not a page. Put it where file_read can see it."""
    from .browser import DOWNLOAD_DIR, unique_path  # shared destination and naming

    folder = load_config().workspace / DOWNLOAD_DIR
    folder.mkdir(parents=True, exist_ok=True)
    name = filename or Path(urlparse(url).path).name or "download"
    target = unique_path(folder, name)
    target.write_bytes(response.content)
    log.info("fetched binary %s -> %s (%d bytes)", url, target.name, target.stat().st_size)
    return {
        "status": "saved",
        "url": str(response.url),
        "content_type": response.headers.get("content-type", ""),
        "path": f"{DOWNLOAD_DIR}/{target.name}",
        "size_bytes": target.stat().st_size,
        "note": (
            "This is not a web page, so it was saved to the workspace instead of "
            "being read. Text formats can be opened with file_read."
        ),
    }


# ---------------------------------------------------------------------- tools


@tool(
    group=GROUP,
    name="web_search",
    description=(
        "Search the public web and get back ranked results with titles, links "
        "and snippets. Use this for anything you cannot answer from your own "
        "knowledge or from the user's Google account: current events, prices, "
        "release notes, product comparisons, anything after your training cut-off, "
        "or whenever the user wants sources. Do not use it for the user's own "
        "mail, files or calendar — those have their own tools. Snippets are short; "
        "call web_fetch on the results that matter to read the actual page before "
        "drawing conclusions. Run several searches with different wording when one "
        "set of results is thin."
    ),
    schema=obj(
        {
            "query": {
                "type": "string",
                "description": "What to search for. Plain words work best; keep it under about 15 words.",
            },
            "max_results": {
                "type": "integer",
                "description": f"How many results to return (1-{MAX_RESULTS}). Default {DEFAULT_RESULTS}.",
            },
            "recency": {
                "type": "string",
                "enum": list(RECENCY),
                "description": (
                    "Only return pages published within this window. Use it when "
                    "the user asks what is happening now or wants the latest."
                ),
            },
            "site": {
                "type": "string",
                "description": "Restrict to one domain, e.g. 'docs.python.org'.",
            },
        },
        required=["query"],
    ),
)
def web_search(
    query: str,
    max_results: int = DEFAULT_RESULTS,
    recency: str | None = None,
    site: str | None = None,
) -> dict:
    config = load_config()
    count = max(1, min(int(max_results), MAX_RESULTS))
    if recency and recency not in RECENCY:
        raise ValueError(f"recency must be one of {'|'.join(RECENCY)}")

    phrase = query.strip()
    if site:
        phrase = f"{phrase} site:{site.strip().removeprefix('https://').removeprefix('http://')}"

    order = provider_order(config)
    notes: list[str] = []
    for name in order:
        try:
            results = _PROVIDERS[name](config, phrase, count, recency)
        except (SearchError, httpx.HTTPError) as exc:
            detail = str(exc) if isinstance(exc, SearchError) else f"{type(exc).__name__}: {exc}"
            log.warning("search via %s failed: %s", name, detail)
            notes.append(f"{name}: {detail}")
            continue

        log.info("search %r via %s -> %d result(s)", phrase, name, len(results))
        if not results:
            notes.append(f"{name}: no results")
            continue
        return {
            "query": phrase,
            "provider": name,
            "recency": recency,
            "count": len(results),
            "results": [r.as_dict() for r in results[:count]],
            **({"notes": notes} if notes else {}),
            "source": "live web search — cite these URLs for anything you take from them",
        }

    raise RuntimeError(
        "no search provider could answer. Tried: "
        + ("; ".join(notes) if notes else ", ".join(order) or "none configured")
    )


@tool(
    group=GROUP,
    name="web_fetch",
    description=(
        "Open one web page and read it as text. Use this after web_search to read "
        "a result properly rather than trusting its snippet, or directly when the "
        "user gives you a URL. Returns the page title and its main content with "
        "navigation and boilerplate stripped. Pages that are files rather than "
        "documents (PDF, images, archives) are saved into the workspace instead "
        "and the path is returned. This reads the page as an anonymous visitor: "
        "for anything behind a login, or a page that only renders after scripts "
        "run, use browser_open instead."
    ),
    schema=obj(
        {
            "url": {"type": "string", "description": "Full URL of the page to read."},
            "max_chars": {
                "type": "integer",
                "description": f"Cap on returned text (default {DEFAULT_FETCH_CHARS}, max {MAX_FETCH_CHARS}).",
            },
            "include_links": {
                "type": "boolean",
                "description": "Also return the page's links, for finding the next page to open. Default false.",
            },
        },
        required=["url"],
    ),
)
def web_fetch(
    url: str, max_chars: int = DEFAULT_FETCH_CHARS, include_links: bool = False
) -> dict:
    target = normalise_url(url)
    limit = max(500, min(int(max_chars), MAX_FETCH_CHARS))

    try:
        with _client(FETCH_TIMEOUT) as client:
            response = client.get(target)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"could not reach {target} ({type(exc).__name__}: {exc})") from exc

    if response.status_code >= 400:
        raise RuntimeError(
            f"{target} returned HTTP {response.status_code}. "
            "The page may be gone, or may be blocking automated readers — "
            "browser_open can often still open it."
        )

    content_type = response.headers.get("content-type", "").lower()
    if len(response.content) > MAX_DOWNLOAD_BYTES:
        raise RuntimeError(f"{target} is larger than {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB")
    if "html" not in content_type and not content_type.startswith("text/"):
        return _save_binary(response, target, None)

    if content_type.startswith("text/") and "html" not in content_type:
        title, text, links = "", response.text, []
    else:
        title, text, links = extract_text(response.text)

    truncated = len(text) > limit
    if truncated:
        text = text[:limit] + f"\n\n[truncated at {limit} characters]"

    log.info("fetched %s (%d chars%s)", target, len(text), ", truncated" if truncated else "")
    payload = {
        "url": str(response.url),
        "title": title,
        "content_type": content_type.split(";")[0],
        "content": text,
        "truncated": truncated,
        "source": "live web page — cite this URL for anything you take from it",
    }
    if include_links:
        payload["links"] = links[:80]
    return payload
