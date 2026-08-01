"""Rebuild a browsable local mirror of the Spider reports site."""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Dict
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .parse import TABLE_ACTION_RE

log = logging.getLogger("updes.site")

_ASSET_ATTRS = {"img": "src", "script": "src"}


class SiteBuilder:
    """Downloads assets and rewrites pages to work offline.

    Layout produced under ``root``::

        index.html                 (sectors landing page)
        sectors/<NN_slug>.html
        tables/table<ID>.html
        assets/<files>
    """

    def __init__(self, root: Path, client, site_host: str,
                 fallback_asset_dirs: list | None = None):
        self.root = root
        self.client = client
        self.host = site_host
        self.fallback_asset_dirs = [Path(p) for p in (fallback_asset_dirs or [])]
        self.assets: Dict[str, str] = {}          # abs_url -> local filename
        self.sector_files: Dict[str, str] = {}    # url key -> sectors/<file>
        self.table_files: Dict[str, str] = {}      # url key -> tables/<file>
        (self.root / "sectors").mkdir(parents=True, exist_ok=True)
        (self.root / "tables").mkdir(parents=True, exist_ok=True)
        (self.root / "assets").mkdir(parents=True, exist_ok=True)

    # -- registration -----------------------------------------------------
    @staticmethod
    def _key(url: str) -> str:
        p = urlparse(url)
        return (p.path.rsplit("/", 1)[-1] or p.path).split("?")[0]

    def register_sector(self, url: str, filename: str) -> None:
        self.sector_files[self._key(url)] = f"sectors/{filename}"

    def register_table(self, url: str, filename: str) -> None:
        self.table_files[self._key(url)] = f"tables/{filename}"

    # -- asset handling ---------------------------------------------------
    def _local_asset_name(self, abs_url: str) -> str:
        if abs_url in self.assets:
            return self.assets[abs_url]
        base = self._key(abs_url) or "asset"
        base = re.sub(r"[^A-Za-z0-9_.-]", "_", base)
        name = base
        # de-dupe distinct URLs that share a basename
        if name in self.assets.values():
            h = hashlib.md5(abs_url.encode()).hexdigest()[:6]
            stem, dot, ext = base.partition(".")
            name = f"{stem}_{h}{dot}{ext}"
        self.assets[abs_url] = name
        return name

    def download_assets(self) -> None:
        for abs_url, name in list(self.assets.items()):
            dest = self.root / "assets" / name
            if dest.exists():
                continue
            try:
                resp = self.client.get(abs_url, label=f"asset {name}",
                                       reselect_on_fail=False)
                dest.write_bytes(resp.content)
            except Exception as exc:  # best effort; site should still render
                if self._copy_local_asset(name, dest):
                    log.info("asset %s taken from local fallback", name)
                else:
                    log.warning("asset download failed %s: %s", abs_url, exc)

    def _copy_local_asset(self, name: str, dest: Path) -> bool:
        for base in self.fallback_asset_dirs:
            cand = base / name
            if cand.exists():
                dest.write_bytes(cand.read_bytes())
                return True
        return False

    # -- page rewriting ---------------------------------------------------
    def localize(self, html: str, page_url: str, depth: int) -> str:
        """Rewrite one page. ``depth`` = folder depth below root (0/1)."""
        prefix = "../" * depth
        soup = BeautifulSoup(html, "lxml")

        # asset resources
        for tag_name, attr in _ASSET_ATTRS.items():
            for tag in soup.find_all(tag_name):
                self._rewrite_asset(tag, attr, page_url, prefix)
        for link in soup.find_all("link", href=True):
            rels = link.get("rel") or []
            if "stylesheet" in rels or link.get("type") == "text/css":
                self._rewrite_asset(link, "href", page_url, prefix)
        for tag in soup.find_all(attrs={"background": True}):
            self._rewrite_asset(tag, "background", page_url, prefix)

        # anchors
        for a in soup.find_all("a", href=True):
            self._rewrite_anchor(a, page_url, prefix)

        # neutralise the toggle-language fetch + print scripts that break offline
        for s in soup.find_all("script"):
            if s.string and ("toggleLang" in s.string or "location.reload" in s.string):
                s.decompose()

        return str(soup)

    def _rewrite_asset(self, tag, attr, page_url, prefix) -> None:
        ref = tag.get(attr)
        if not ref or ref.startswith("data:"):
            return
        abs_url = urljoin(page_url, ref)
        if urlparse(abs_url).netloc != self.host:
            return
        name = self._local_asset_name(abs_url)
        tag[attr] = f"{prefix}assets/{name}"

    def _rewrite_anchor(self, a, page_url, prefix) -> None:
        href = a["href"]
        if href.startswith(("#", "mailto:", "javascript:")):
            return
        abs_url = urljoin(page_url, href)
        key = self._key(abs_url)
        if TABLE_ACTION_RE.search(abs_url) and key in self.table_files:
            a["href"] = prefix + self.table_files[key]
            a["target"] = "_self"
            return
        if key in self.sector_files:
            a["href"] = prefix + self.sector_files[key]
            a["target"] = "_self"
            return
        # keep other same-site links pointing at the live site
        if urlparse(abs_url).netloc == self.host:
            a["href"] = abs_url

    # -- writing ----------------------------------------------------------
    def write(self, rel_path: str, html: str) -> None:
        dest = self.root / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(html, encoding="utf-8")
