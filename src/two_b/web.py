"""Fetch a URL and extract its readable content — stdlib only (urllib + html.parser),
no lxml and no third-party scraper, so 2B keeps its minimal footprint. Used by the /web
command (load a page into the model's context) and by installer model discovery
(ollama.com). Everything here is best-effort and NEVER raises — a network or parse
failure yields None / whatever text was recovered, not an exception.

The readable-extraction heuristics — drop script/style/noscript/svg/iframe/template and
hidden / aria-hidden subtrees, strip HTML comments and zero-width characters, prefer the
<main>/<article> region over the whole <body>, collapse whitespace, and optionally emit
light markdown — are adapted from Scrapling's Convertor (scrapling/core/shell.py,
BSD-3-Clause) and reimplemented over the stdlib html.parser.
"""
import ipaddress
import re
import socket
import urllib.parse
import urllib.request
from html.parser import HTMLParser

_UA = "2b-agent"
_FETCH_TIMEOUT = 6
_MAX_BYTES = 5_000_000           # cap the download so a hostile/huge page can't blow up memory
_PARSE_CAP = 400_000             # hard bound on raw emitted chars (hostile-input guard); the
                                 # caller's max_chars trims the final text, decoupled from this
                                 # so structural whitespace can't starve real content early
_SCAN_LIMIT = 500                # how far up the tag stack a close searches for its match:
                                 # bounds the per-endtag scan (no O(n²)) while still recovering
                                 # from realistic crossing/mismatched nesting (deeper → over-skip)

# Subtrees whose text is never content. Kept deliberately NARROW: structural tags like
# form/nav/header/footer legitimately wrap real content on many pages (e.g. ollama.com
# wraps its whole model list in a <form>), so skipping them structurally would drop the
# page. The <main>/<article> preference is what trims site chrome when it's available.
_SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "iframe", "template", "head"})
_VOID = frozenset({"br", "hr", "img", "input", "meta", "link", "source", "col", "area", "wbr"})
_BLOCK = frozenset({"p", "div", "section", "article", "main", "header", "li", "ul", "ol",
                    "tr", "table", "pre", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "dl", "dd"})
_HEADING = {"h1": "# ", "h2": "## ", "h3": "### ", "h4": "#### ", "h5": "##### ", "h6": "###### "}
_MAIN = frozenset({"main", "article"})
_ZERO_WIDTH = re.compile("[\u200b\u200c\u200d\u2060\ufeff]")
_INLINE_WS = re.compile(r"[ \t\f\v\r]+")
_BLANKS = re.compile(r"\n{3,}")


def _host_is_public(host: str) -> bool:
    """True only if `host` resolves entirely to public IPs — blocks SSRF to loopback /
    private / link-local / reserved / metadata addresses (e.g. 127.0.0.1, 10.x, 169.254.*).
    Note: this resolves independently of the later connect, so a DNS-rebinding attacker
    (public IP now, private moments later) isn't fully closed — accepted residual risk for a
    personal tool; a full fix needs a pinned-IP connection (custom HTTPConnection)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    if not infos:
        return False
    for *_, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def _ok_url(url: str) -> bool:
    if not url.startswith("https://"):
        return False
    host = urllib.parse.urlsplit(url).hostname
    return bool(host) and _host_is_public(host)


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Re-apply the https-only + public-host checks to every redirect target, since
    urllib would otherwise follow a 3xx to http:// or an internal address unchecked."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _ok_url(newurl):
            return None                      # refuse the redirect (urllib stops here)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, timeout: int = _FETCH_TIMEOUT) -> str | None:
    """GET `url`, returning the decoded body or None on any failure (never raises). HTTP is
    upgraded to HTTPS; non-HTTPS and non-public hosts are refused, on the initial URL AND on
    every redirect (SSRF guard). Mirrors update.py's never-raise fetch style."""
    try:
        if url.startswith("http://"):
            url = "https://" + url[len("http://"):]
        if not _ok_url(url):
            return None
        opener = urllib.request.build_opener(_SafeRedirect())
        req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "text/html,*/*"})
        with opener.open(req, timeout=timeout) as r:
            raw = r.read(_MAX_BYTES)
            charset = r.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")
    except Exception:
        return None


def _hidden(attrs) -> bool:
    d = dict(attrs)
    if "hidden" in d:
        return True
    if (d.get("aria-hidden") or "").lower() == "true":
        return True
    style = (d.get("style") or "").replace(" ", "").lower()
    return "display:none" in style or "visibility:hidden" in style


class _Extract(HTMLParser):
    """Collect readable text (optionally light markdown). Tolerant of malformed HTML and
    bounded on hostile input. A single tag stack of (name, is_skip, is_main) tracks nesting;
    running counters (`_skip_n`, `_main_n`) make "am I skipping / in main" an O(1) check at
    ANY depth (no depth cap → no filter leak). A close pops to the nearest matching open
    within a bounded scan window (`_SCAN_LIMIT`), which recovers from crossing/mismatched
    nesting like `<a hidden><b hidden>…</a></b>` without the O(n²) of an unbounded scan and
    without the "stuck-skipping" wedge of a top-match-only pop. (Known limit, shared with
    browsers: text inside an UNCLOSED <script>/<style> is CDATA attributed to that skipped
    element, so it's dropped, not recovered as markup.)"""

    def __init__(self, as_markdown: bool, cap: int):
        super().__init__(convert_charrefs=True)
        self.md = as_markdown
        self.cap = cap
        self.out: list[str] = []       # whole-document text
        self.main: list[str] = []       # text within <main>/<article> only
        self.n = 0                      # chars emitted (bound)
        self._stack: list[tuple[str, bool, bool]] = []   # (tag, is_skip, is_main)
        self._skip_n = 0                # open skip/hidden elements (skipping iff > 0)
        self._main_n = 0                # open main/article elements
        self._a_href: str | None = None
        self._a_buf: list[str] = []
        self._a_len = 0                 # running len(_a_buf) — avoids O(n²) re-summing

    # --- helpers ---
    def _skipping(self) -> bool:
        return self._skip_n > 0

    def _in_main(self) -> bool:
        return self._main_n > 0

    def _full(self) -> bool:
        return self.n >= self.cap                # hit the hostile-input bound — stop all work

    def _emit(self, s: str) -> None:
        room = self.cap - self.n                 # bound even a single giant text node
        if not s or room <= 0:
            return
        if len(s) > room:
            s = s[:room]
        self.out.append(s)
        if self._in_main():
            self.main.append(s)
        self.n += len(s)

    # --- parser callbacks ---
    def handle_starttag(self, tag, attrs):
        if self._full():
            return
        if tag in _VOID:
            if tag in ("br", "hr"):
                self._emit("\n")
            return
        skip, main = (tag in _SKIP_TAGS or _hidden(attrs)), (tag in _MAIN)
        self._stack.append((tag, skip, main))
        self._skip_n += skip
        self._main_n += main
        if self._skipping():
            return
        if tag in _BLOCK:
            self._emit("\n")
        if self.md and tag in _HEADING:
            self._emit("\n" + _HEADING[tag])
        elif self.md and tag == "li":
            self._emit("- ")
        if tag == "a":
            self._a_href = dict(attrs).get("href") if self.md else None
            self._a_buf, self._a_len = [], 0

    def handle_endtag(self, tag):
        if self._full():
            return
        if tag == "a" and self._a_href is not None and not self._skipping():
            href, text = self._a_href, "".join(self._a_buf).strip()
            self._a_href, self._a_buf, self._a_len = None, [], 0
            if text:
                self._emit(f"[{text}]({href})" if href else text)   # light markdown link
        # pop to the nearest matching open within the scan window (recovers crossing nesting;
        # bounded so it can't go O(n²); a deeper/absent match is left alone → safe over-skip)
        lo = max(0, len(self._stack) - _SCAN_LIMIT)
        for i in range(len(self._stack) - 1, lo - 1, -1):
            if self._stack[i][0] == tag:
                for _, s, m in self._stack[i:]:
                    self._skip_n -= s
                    self._main_n -= m
                del self._stack[i:]
                break
        if tag in _BLOCK:
            self._emit("\n")

    def handle_data(self, data):
        if self._full() or self._skipping():
            return
        if self._a_href is not None:
            if self._a_len < self.cap:           # bound anchor-text buffer (O(1) check)
                self._a_buf.append(data)
                self._a_len += len(data)
        else:
            self._emit(data)


def _collapse(text: str, cap: int | None) -> str:
    text = _ZERO_WIDTH.sub("", text)
    lines = [_INLINE_WS.sub(" ", ln).strip() for ln in text.split("\n")]
    text = _BLANKS.sub("\n\n", "\n".join(lines)).strip()
    if cap and len(text) > cap:
        text = text[:cap].rsplit("\n", 1)[0] + "\n… [content truncated]"
    return text


def extract_readable(html: str, *, as_markdown: bool = False, max_chars: int | None = None) -> str:
    """Readable text (or light markdown) from an HTML string. Prefers the <main>/<article>
    region if present, else the whole document. Never raises; returns best-effort on
    malformed input. `max_chars` caps the result on a line boundary."""
    if not html:
        return ""
    p = _Extract(as_markdown, cap=_PARSE_CAP)
    try:
        p.feed(html)
        p.close()                                  # flush any script/style CDATA buffer so an
    except Exception:                              # unclosed <script> can't swallow the whole page
        pass                                       # keep whatever was parsed so far
    if p._a_buf:                                   # unclosed <a> at EOF — don't drop its text
        p._emit("".join(p._a_buf))
    chosen = "".join(p.main) if any(s.strip() for s in p.main) else "".join(p.out)
    return _collapse(chosen, max_chars)
