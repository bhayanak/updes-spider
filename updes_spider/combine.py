"""Build a single searchable page (and PDF) from all scraped tables.

Reads the per-table HTML already saved under ``website/tables`` (so it works
even on a ``--resume`` run where most tables were skipped), and emits:

* ``all-tables.html`` — one self-contained page with every table inline, a live
  search box, and a table of contents. Browser Ctrl+F / Cmd+F works too.
* ``all-tables.pdf``  — the same content as a searchable PDF (rendered via a
  headless browser or WeasyPrint if available).
"""

from __future__ import annotations

import html as _html
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

from .parse import parse_table_page

log = logging.getLogger("updes.combine")


def _esc(s) -> str:
    return _html.escape("" if s is None else str(s))


def _render_grid(rows: List[List[str]]) -> str:
    if not rows:
        return ""
    out = ["<table class='data'>"]
    for i, row in enumerate(rows):
        tag = "th" if i == 0 else "td"
        cells = "".join(f"<{tag}>{_esc(c)}</{tag}>" for c in row)
        out.append(f"<tr>{cells}</tr>")
    out.append("</table>")
    return "".join(out)


def _collect(out_root: Path, manifest: dict) -> List[dict]:
    """Return ordered blocks: {sector, table_id, description, grids}."""
    site_tables = out_root / "website"
    blocks: List[dict] = []
    for sec in manifest.get("sectors", []):
        for t in sec.get("tables", []):
            rel = t.get("html")
            if not rel:
                continue
            fpath = site_tables / rel
            if not fpath.exists():
                continue
            try:
                page = parse_table_page(fpath.read_text(encoding="utf-8", errors="replace"))
            except Exception as exc:
                log.warning("could not parse %s: %s", fpath, exc)
                continue
            grids = [dt.rows for dt in page.tables if not dt.is_empty]
            if not grids:
                continue
            blocks.append({
                "sector": sec.get("name", ""),
                "table_id": t.get("table_id", ""),
                "description": t.get("description") or t.get("label") or "",
                "grids": grids,
            })
    return blocks


def build_combined_html(out_root: Path, manifest: dict) -> Optional[Path]:
    blocks = _collect(out_root, manifest)
    if not blocks:
        log.warning("no table data found to combine")
        return None

    year = _esc(manifest.get("year"))
    district = _esc(manifest.get("district"))
    generated = _esc(manifest.get("generated"))

    # table of contents grouped by sector
    toc_parts = []
    current = None
    for i, b in enumerate(blocks):
        if b["sector"] != current:
            if current is not None:
                toc_parts.append("</ul>")
            current = b["sector"]
            toc_parts.append(f"<li class='sec'>{_esc(current)}</li><ul>")
        toc_parts.append(
            f"<li><a href='#t{i}'>Table {_esc(b['table_id'])} — {_esc(b['description'])}</a></li>"
        )
    if current is not None:
        toc_parts.append("</ul>")

    body_parts = []
    current = None
    for i, b in enumerate(blocks):
        if b["sector"] != current:
            current = b["sector"]
            body_parts.append(f"<h2 class='sector'>{_esc(current)}</h2>")
        grids_html = "".join(_render_grid(g) for g in b["grids"])
        body_parts.append(
            f"<section class='tbl' id='t{i}'>"
            f"<h3>Table {_esc(b['table_id'])}: {_esc(b['description'])}</h3>"
            f"<div class='sec-note'>{_esc(b['sector'])}</div>"
            f"{grids_html}</section>"
        )

    doc = _TEMPLATE.format(
        year=year, district=district, generated=generated,
        count=len(blocks),
        toc="".join(toc_parts),
        body="".join(body_parts),
    )
    dest = out_root / "all-tables.html"
    dest.write_text(doc, encoding="utf-8")
    log.info("combined page: %s (%d tables)", dest, len(blocks))
    return dest


# --------------------------------------------------------------------------
# PDF rendering
# --------------------------------------------------------------------------
_CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
]
_CHROME_ON_PATH = ["google-chrome", "google-chrome-stable", "chromium",
                   "chromium-browser", "chrome", "microsoft-edge", "brave-browser"]


def _find_chrome() -> Optional[str]:
    for p in _CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    for name in _CHROME_ON_PATH:
        found = shutil.which(name)
        if found:
            return found
    return None


def _pdf_via_chrome(html_path: Path, pdf_path: Path) -> bool:
    chrome = _find_chrome()
    if not chrome:
        return False
    # A workspace-local profile dir avoids the shared-profile SingletonLock and
    # works even where the system temp dir is not writable.
    profile = pdf_path.parent / ".chrome-profile"
    profile.mkdir(parents=True, exist_ok=True)
    if pdf_path.exists():
        pdf_path.unlink()
    argv = [
        chrome, "--headless", "--disable-gpu", "--no-sandbox",
        f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
        "--disable-extensions", "--disable-crash-reporter",
        "--no-pdf-header-footer",
        f"--print-to-pdf={pdf_path}",
        html_path.resolve().as_uri(),
    ]
    ok = False
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # Some Chrome builds write the PDF but never exit; poll for a stable file
        # then stop the process ourselves instead of waiting on it.
        import time
        deadline = time.time() + 90
        last = -1
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            if pdf_path.exists():
                sz = pdf_path.stat().st_size
                if sz > 0 and sz == last:
                    break
                last = sz
            time.sleep(1)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        ok = pdf_path.exists() and pdf_path.stat().st_size > 0
    except Exception as exc:
        log.warning("chrome PDF failed: %s", exc)
    shutil.rmtree(profile, ignore_errors=True)
    return ok


def _pdf_via_weasyprint(html_path: Path, pdf_path: Path) -> bool:
    try:
        from weasyprint import HTML  # type: ignore
    except Exception:
        return False
    try:
        HTML(filename=str(html_path)).write_pdf(str(pdf_path))
        return pdf_path.exists() and pdf_path.stat().st_size > 0
    except Exception as exc:
        log.warning("weasyprint failed (missing system libs?): %s", exc)
        return False


def _pdf_via_wkhtmltopdf(html_path: Path, pdf_path: Path) -> bool:
    exe = shutil.which("wkhtmltopdf")
    if not exe:
        return False
    try:
        subprocess.run([exe, "--enable-local-file-access", str(html_path),
                        str(pdf_path)], capture_output=True, timeout=180)
    except Exception as exc:
        log.warning("wkhtmltopdf failed: %s", exc)
        return False
    return pdf_path.exists() and pdf_path.stat().st_size > 0


def build_pdf(html_path: Path, pdf_path: Path, engine: str = "auto") -> bool:
    engines = {
        "chrome": _pdf_via_chrome,
        "weasyprint": _pdf_via_weasyprint,
        "wkhtmltopdf": _pdf_via_wkhtmltopdf,
    }
    order = ["chrome", "weasyprint", "wkhtmltopdf"] if engine == "auto" else [engine]
    for name in order:
        fn = engines.get(name)
        if not fn:
            continue
        if fn(html_path, pdf_path):
            log.info("PDF written via %s: %s", name, pdf_path)
            return True
    log.warning(
        "Could not generate a PDF automatically. Open %s in a browser and use "
        "Print -> Save as PDF (that also gives a fully searchable file).",
        html_path,
    )
    return False


def build_outputs(out_root: Path, manifest: dict, *, engine: str = "auto",
                  make_pdf: bool = True) -> None:
    html_path = build_combined_html(out_root, manifest)
    if not html_path:
        return
    if make_pdf:
        build_pdf(html_path, out_root / "all-tables.pdf", engine=engine)


_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>All Tables — {district} {year}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif;
         margin: 0; color: #1b2631; }}
  header {{ position: sticky; top: 0; z-index: 10; background: #172598;
           color: #fff; padding: 12px 20px; box-shadow: 0 2px 6px rgba(0,0,0,.3); }}
  header h1 {{ margin: 0 0 8px; font-size: 20px; }}
  .meta {{ font-size: 13px; opacity: .9; }}
  .searchbar {{ margin-top: 10px; display: flex; gap: 10px; align-items: center; }}
  #q {{ flex: 1; max-width: 640px; padding: 10px 12px; font-size: 15px;
        border: 0; border-radius: 6px; }}
  #count {{ font-size: 13px; white-space: nowrap; }}
  .wrap {{ display: flex; gap: 20px; align-items: flex-start; padding: 20px;
          max-width: 1400px; margin: 0 auto; }}
  nav {{ position: sticky; top: 120px; width: 300px; max-height: 80vh;
        overflow: auto; font-size: 13px; flex: 0 0 auto;
        border: 1px solid #dfe4ea; border-radius: 8px; padding: 10px; }}
  nav ul {{ list-style: none; margin: 0 0 6px; padding-left: 12px; }}
  nav li.sec {{ font-weight: 700; margin-top: 8px; color: #172598; }}
  nav a {{ color: #2c3e50; text-decoration: none; }}
  nav a:hover {{ text-decoration: underline; }}
  main {{ flex: 1; min-width: 0; }}
  h2.sector {{ color: #172598; border-bottom: 3px solid #8080ff;
              padding-bottom: 4px; margin-top: 28px; }}
  section.tbl {{ margin: 18px 0 28px; }}
  section.tbl h3 {{ margin: 0 0 2px; font-size: 16px; }}
  .sec-note {{ font-size: 12px; color: #7f8c8d; margin-bottom: 8px; }}
  table.data {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
  table.data th, table.data td {{ border: 1px solid #b2bec3; padding: 4px 7px;
                                 text-align: left; vertical-align: top; }}
  table.data th {{ background: #8080ff; color: #fff; position: sticky; }}
  table.data tr:nth-child(even) td {{ background: #f4f6f8; }}
  .hidden {{ display: none !important; }}
  @media print {{
    header {{ position: static; }} nav {{ display: none; }}
    .wrap {{ display: block; padding: 0; }}
    section.tbl {{ page-break-inside: avoid; }}
    table.data th {{ position: static; }}
  }}
</style>
</head>
<body>
<header>
  <h1>All Tables — {district}, {year}</h1>
  <div class="meta">{count} tables &middot; generated {generated} &middot;
       tip: type to filter, or use Ctrl/Cmd+F</div>
  <div class="searchbar">
    <input id="q" type="search" placeholder="Search across all tables…" autofocus>
    <span id="count"></span>
  </div>
</header>
<div class="wrap">
  <nav><strong>Contents</strong><ul>{toc}</ul></nav>
  <main>{body}</main>
</div>
<script>
  const q = document.getElementById('q');
  const count = document.getElementById('count');
  const blocks = Array.prototype.slice.call(document.querySelectorAll('section.tbl'));
  const texts = blocks.map(function(b) {{ return b.textContent.toLowerCase(); }});
  const total = blocks.length;
  function apply() {{
    const v = q.value.trim().toLowerCase();
    let shown = 0;
    for (let i = 0; i < blocks.length; i++) {{
      const hit = v === '' || texts[i].indexOf(v) !== -1;
      blocks[i].classList.toggle('hidden', !hit);
      if (hit) shown++;
    }}
    count.textContent = v === '' ? (total + ' tables')
      : (shown + ' of ' + total + ' tables match');
  }}
  q.addEventListener('input', apply);
  apply();
</script>
</body>
</html>"""
