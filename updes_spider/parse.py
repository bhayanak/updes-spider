"""HTML parsing for the Spider reports pages."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from bs4 import BeautifulSoup

EXCLUDE_CLASS = "exclude-from-export"
TABLE_ACTION_RE = re.compile(r"gettable([0-9A-Za-z]+)Report\.action", re.I)


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


# --------------------------------------------------------------------------
# Dropdowns on the initialise page
# --------------------------------------------------------------------------
def parse_districts(html: str) -> dict:
    """Return {code: label} from the district <select> on the init page."""
    soup = _soup(html)
    sel = soup.find("select", id="dist") or soup.find("select", attrs={"name": "dist"})
    out: dict = {}
    if not sel:
        return out
    for opt in sel.find_all("option"):
        val = (opt.get("value") or "").strip()
        if val:
            out[val] = opt.get_text(strip=True)
    return out


def parse_years(html: str) -> List[str]:
    soup = _soup(html)
    sel = soup.find("select", id="year_tab00") or soup.find(
        "select", attrs={"name": "year_tab00"}
    )
    if not sel:
        return []
    return [o.get("value", "").strip() for o in sel.find_all("option") if o.get("value")]


# --------------------------------------------------------------------------
# Sector list (left navigation pane)
# --------------------------------------------------------------------------
@dataclass
class Sector:
    name: str
    url: str          # absolute or site-relative jsp/action link
    index: int = 0


def parse_sectors(html: str) -> List[Sector]:
    """Extract the left-pane sector list.

    Sector links live in ``<td class="td1"><a href="...jsp">Name</a></td>``.
    """
    soup = _soup(html)
    sectors: List[Sector] = []
    seen = set()
    for td in soup.find_all("td", class_="td1"):
        a = td.find("a", href=True)
        if not a:
            continue
        href = a["href"].strip()
        name = a.get_text(" ", strip=True)
        if not href or not name:
            continue
        key = href.split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        sectors.append(Sector(name=name, url=href, index=len(sectors) + 1))
    return sectors


# --------------------------------------------------------------------------
# Table links (right pane, per sector)
# --------------------------------------------------------------------------
@dataclass
class TableLink:
    table_id: str      # e.g. "1", "2", "3B"
    url: str           # gettableNReport.action
    label: str         # anchor text, e.g. "Table 2 (A) And Table 2 (B)"
    description: str    # description cell text


def parse_table_links(html: str) -> List[TableLink]:
    soup = _soup(html)
    links: List[TableLink] = []
    seen = set()
    for a in soup.find_all("a", href=True):
        m = TABLE_ACTION_RE.search(a["href"])
        if not m:
            continue
        url = a["href"].strip()
        if url in seen:
            continue
        seen.add(url)
        table_id = m.group(1)
        label = a.get_text(" ", strip=True)
        description = _nearest_description(a)
        links.append(TableLink(table_id=table_id, url=url, label=label,
                               description=description))
    return links


def _nearest_description(anchor) -> str:
    """The description sits in the sibling <td> of the anchor's <td>."""
    cell = anchor.find_parent("td")
    if not cell:
        return ""
    parts = []
    for sib in cell.find_next_siblings("td"):
        txt = sib.get_text(" ", strip=True)
        if txt:
            parts.append(txt)
    return " | ".join(parts)


# --------------------------------------------------------------------------
# Table data page
# --------------------------------------------------------------------------
@dataclass
class DataTable:
    rows: List[List[str]] = field(default_factory=list)  # merged/expanded grid

    @property
    def is_empty(self) -> bool:
        return not any(any(c.strip() for c in r) for r in self.rows)


@dataclass
class TablePage:
    title: str
    tables: List[DataTable]

    @property
    def has_data(self) -> bool:
        return any(not t.is_empty and len(t.rows) >= 2 for t in self.tables)


def _has_exclude_class(tag) -> bool:
    cls = tag.get("class") or []
    return EXCLUDE_CLASS in cls


def _cell_grid(table) -> List[List[str]]:
    """Convert a <table> to a rectangular grid, honouring rowspan/colspan."""
    grid: List[List[Optional[str]]] = []
    occupied: dict = {}
    row_idx = 0
    for tr in table.find_all("tr", recursive=True):
        # skip rows that belong to a nested table
        if tr.find_parent("table") is not table:
            continue
        cells = [c for c in tr.find_all(["td", "th"], recursive=False)]
        if not cells:
            continue
        col = 0
        while len(grid) <= row_idx:
            grid.append([])
        for cell in cells:
            while (row_idx, col) in occupied:
                col += 1
            text = cell.get_text(" ", strip=True)
            text = re.sub(r"\s+", " ", text)
            try:
                rowspan = int(cell.get("rowspan", 1))
            except ValueError:
                rowspan = 1
            try:
                colspan = int(cell.get("colspan", 1))
            except ValueError:
                colspan = 1
            for dr in range(rowspan):
                for dc in range(colspan):
                    r = row_idx + dr
                    c = col + dc
                    while len(grid) <= r:
                        grid.append([])
                    row = grid[r]
                    while len(row) <= c:
                        row.append("")
                    row[c] = text if (dr == 0 and dc == 0) else ""
                    if rowspan > 1 or colspan > 1:
                        occupied[(r, c)] = True
            col += colspan
        row_idx += 1
    # normalise width
    width = max((len(r) for r in grid), default=0)
    return [[(c if c is not None else "") for c in r] + [""] * (width - len(r))
            for r in grid]


def parse_table_page(html: str) -> TablePage:
    soup = _soup(html)
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()

    # data tables = tables without the exclude class that are not merely
    # wrappers around another (nested) table.
    candidates = []
    for t in soup.find_all("table"):
        if _has_exclude_class(t):
            continue
        if t.find("table") is not None:
            continue  # wrapper; the inner table will be captured separately
        candidates.append(t)

    data_tables: List[DataTable] = []
    for t in candidates:
        grid = _cell_grid(t)
        dt = DataTable(rows=grid)
        if not dt.is_empty and len(dt.rows) >= 2:
            data_tables.append(dt)

    # Fallback: pick the largest table on the page if nothing matched.
    if not data_tables:
        best = None
        best_rows = 0
        for t in soup.find_all("table"):
            rows = len(t.find_all("tr"))
            if rows > best_rows:
                best, best_rows = t, rows
        if best is not None:
            data_tables.append(DataTable(rows=_cell_grid(best)))

    return TablePage(title=title, tables=data_tables)
