"""Robust HTTP session for the UP DES Spider reports site.

The government site is slow and flaky: individual table pages can take 15+
seconds and sometimes fail outright. This client wraps :mod:`requests` with:

* urllib3 connection-level retries (for transient network errors / 5xx),
* an application-level retry loop that can re-establish the server session
  (JSESSIONID + selected year/district) when a response looks invalid, and
* generous, separately-configurable connect/read timeouts.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urljoin

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("updes.client")

# The site presents an incomplete certificate chain. Verification is disabled
# deliberately for this public, read-only government data source.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass
class ClientConfig:
    base_url: str = "https://updes.up.nic.in/spiderreports"
    connect_timeout: float = 30.0
    read_timeout: float = 240.0
    max_attempts: int = 6          # application-level attempts per request
    backoff_base: float = 3.0      # seconds; grows linearly per attempt
    backoff_max: float = 30.0
    verify_tls: bool = False
    user_agent: str = DEFAULT_UA
    headers: dict = field(default_factory=dict)


class SpiderClient:
    """A resilient session against a single year/district selection."""

    def __init__(self, cfg: ClientConfig):
        self.cfg = cfg
        self.session = self._build_session()
        self._reselect: Optional[Callable[["SpiderClient"], None]] = None
        self.selected = False

    # -- session plumbing -------------------------------------------------
    def _build_session(self) -> requests.Session:
        s = requests.Session()
        s.verify = self.cfg.verify_tls
        s.headers.update({"User-Agent": self.cfg.user_agent})
        # The server is finicky about reused keep-alive sockets and often
        # closes them mid-request (RemoteDisconnected). A fresh connection per
        # request is slower but far more reliable here.
        s.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "close",
            "Referer": self.cfg.base_url.rstrip("/") + "/intialisePage.action",
        })
        s.headers.update(self.cfg.headers)
        retry = Retry(
            total=4,
            connect=4,
            read=4,
            status=4,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        return s

    @property
    def timeout(self):
        return (self.cfg.connect_timeout, self.cfg.read_timeout)

    def url(self, path: str) -> str:
        # Resolve against the base dir so that absolute (http...), root-relative
        # ("/spiderreports/x.jsp") and bare ("x.jsp") hrefs all map correctly.
        if path.startswith("http"):
            return path
        return urljoin(self.cfg.base_url.rstrip("/") + "/", path)

    def set_reselect(self, fn: Callable[["SpiderClient"], None]) -> None:
        """Register a callback that re-establishes year/district selection."""
        self._reselect = fn

    # -- core request with app-level retry --------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        data: Optional[dict] = None,
        validate: Optional[Callable[[requests.Response], bool]] = None,
        label: str = "",
        reselect_on_fail: bool = True,
    ) -> requests.Response:
        url = self.url(path)
        label = label or url
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.cfg.max_attempts + 1):
            try:
                resp = self.session.request(
                    method, url, data=data, timeout=self.timeout, allow_redirects=True
                )
                ok = resp.status_code == 200 and bool(resp.content)
                if ok and validate is not None:
                    ok = validate(resp)
                if ok:
                    if attempt > 1:
                        log.info("  %s succeeded on attempt %d", label, attempt)
                    return resp
                reason = f"HTTP {resp.status_code}, {len(resp.content)} bytes, validate={validate is not None}"
                log.warning("  %s invalid response (attempt %d/%d): %s",
                            label, attempt, self.cfg.max_attempts, reason)
            except requests.RequestException as exc:
                last_exc = exc
                log.warning("  %s error (attempt %d/%d): %s",
                            label, attempt, self.cfg.max_attempts, exc)

            if attempt < self.cfg.max_attempts:
                # Re-establish the session selection on later attempts, as a
                # dropped/expired JSESSIONID is a common failure mode here.
                if reselect_on_fail and self._reselect and attempt >= 2:
                    try:
                        log.info("  re-establishing session before retry ...")
                        self._reselect(self)
                    except Exception as exc:  # pragma: no cover - best effort
                        log.warning("  reselect failed: %s", exc)
                delay = min(self.cfg.backoff_base * attempt, self.cfg.backoff_max)
                time.sleep(delay)

        msg = f"{label}: exhausted {self.cfg.max_attempts} attempts"
        if last_exc:
            raise RuntimeError(msg) from last_exc
        raise RuntimeError(msg)

    def get(self, path: str, **kw) -> requests.Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw) -> requests.Response:
        return self.request("POST", path, **kw)
