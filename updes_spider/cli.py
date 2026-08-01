"""Command-line orchestration: scrape a year/district and build outputs."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse

from .client import ClientConfig, SpiderClient
from .excel import write_table_workbook
from .parse import (
    parse_districts,
    parse_sectors,
    parse_table_links,
    parse_table_page,
)
from .site import SiteBuilder

log = logging.getLogger("updes")

INIT_PATH = "intialisePage.action"
SELECT_PATH = "getGenralNews.action"
SELECT_SUBMIT = "action:getGenralNews"


def slugify(text: str, maxlen: int = 60) -> str:
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip()
    text = re.sub(r"[\s]+", "_", text)
    return (text[:maxlen] or "item").strip("_")


# --------------------------------------------------------------------------
# Session selection (also used as the re-establish callback)
# --------------------------------------------------------------------------
def make_selection(client: SpiderClient, year: str, dist: str,
                   lang: str | None = None) -> str:
    """Warm the session and select year/district. Returns sectors-page HTML."""
    client.get(INIT_PATH, label="initialise page",
               validate=lambda r: "year_tab00" in r.text, reselect_on_fail=False)
    if lang:
        # Best-effort language selection; the site persists it in the session.
        try:
            client.post(INIT_PATH, data={"lang": lang}, label=f"set lang={lang}",
                        reselect_on_fail=False)
        except Exception as exc:  # pragma: no cover - non-fatal
            log.warning("  language selection failed (continuing): %s", exc)
    data = {
        "year_tab00": year,
        "dist": dist,
        SELECT_SUBMIT: "Sector wise district table",
    }
    resp = client.post(
        SELECT_PATH, data=data, label=f"select year={year} dist={dist}",
        validate=lambda r: ("gettable" in r.text.lower() or "td1" in r.text.lower())
        and len(r.content) > 3000,
        reselect_on_fail=False,
    )
    client.selected = True
    return resp.text


# --------------------------------------------------------------------------
# Table fetch with data validation
# --------------------------------------------------------------------------
def _table_has_data(resp) -> bool:
    txt = resp.text
    if len(resp.content) < 1500:
        return False
    if "<table" not in txt.lower():
        return False
    page = parse_table_page(txt)
    return page.has_data


def fetch_table(client: SpiderClient, url: str, table_id: str):
    resp = client.get(url, label=f"table {table_id}", validate=_table_has_data)
    return resp.text, parse_table_page(resp.text)


# --------------------------------------------------------------------------
# Main run
# --------------------------------------------------------------------------
def run(args) -> int:
    cfg = ClientConfig(
        base_url=args.base_url,
        connect_timeout=args.connect_timeout,
        read_timeout=args.read_timeout,
        max_attempts=args.attempts,
        verify_tls=args.verify_tls,
    )
    client = SpiderClient(cfg)
    host = urlparse(cfg.base_url).netloc

    lang = args.lang or None
    client.set_reselect(lambda c: make_selection(c, args.year, args.dist, lang))

    log.info("Selecting year=%s district=%s ...", args.year, args.dist)
    sectors_html = make_selection(client, args.year, args.dist, lang)

    # District display name from the init page dropdown.
    init_html = client.get(INIT_PATH, label="init (districts)",
                           reselect_on_fail=False).text
    districts = parse_districts(init_html)
    dist_label = districts.get(args.dist, args.dist)
    dist_name = dist_label.split("-", 1)[-1].strip() if "-" in dist_label else dist_label

    run_slug = f"{args.year}_{args.dist}_{slugify(dist_name)}"
    out_root = Path(args.out) / run_slug
    excel_root = out_root / "excel"
    site_root = out_root / "website"
    out_root.mkdir(parents=True, exist_ok=True)
    log.info("Output folder: %s", out_root)

    sectors = parse_sectors(sectors_html)
    if args.sectors:
        wanted = {s.lower() for s in args.sectors}
        sectors = [s for s in sectors
                   if any(w in s.name.lower() for w in wanted)]
    log.info("Found %d sectors", len(sectors))

    builder = SiteBuilder(site_root, client, host,
                          fallback_asset_dirs=args.assets_dir)

    manifest = {
        "year": args.year,
        "district_code": args.dist,
        "district": dist_name,
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sectors": [],
    }

    # -- pass 1: discover per-sector table links, register site files ------
    sector_records = []
    for sec in sectors:
        sec_file = f"{sec.index:02d}_{slugify(sec.name)}.html"
        builder.register_sector(sec.url, sec_file)
        try:
            html = client.get(sec.url, label=f"sector '{sec.name}'",
                              validate=lambda r: len(r.content) > 1500).text
        except Exception as exc:
            log.error("  sector '%s' failed: %s", sec.name, exc)
            html = None
        links = parse_table_links(html) if html else []
        for tl in links:
            builder.register_table(tl.url, f"table{tl.table_id}.html")
        sector_records.append((sec, sec_file, html, links))
        log.info("  sector '%s' -> %d tables", sec.name, len(links))

    # -- pass 2: fetch each table, export excel + html --------------------
    total = ok = skipped = failed = 0
    for sec, sec_file, sec_html, links in sector_records:
        sec_dir = excel_root / f"{sec.index:02d}_{slugify(sec.name)}"
        sec_manifest = {"name": sec.name, "url": sec.url, "tables": []}
        for tl in links:
            total += 1
            xlsx_name = f"Table_{tl.table_id}_{slugify(tl.description or tl.label, 40)}.xlsx"
            xlsx_path = sec_dir / xlsx_name
            html_path = site_root / "tables" / f"table{tl.table_id}.html"
            entry = {
                "table_id": tl.table_id,
                "description": tl.description,
                "url": tl.url,
                "excel": str(xlsx_path.relative_to(out_root)),
                "html": f"tables/table{tl.table_id}.html",
                "status": "pending",
            }
            if args.resume and xlsx_path.exists() and html_path.exists():
                skipped += 1
                entry["status"] = "skipped"
                sec_manifest["tables"].append(entry)
                log.info("  [skip] table %s (already done)", tl.table_id)
                continue
            try:
                raw_html, page = fetch_table(client, tl.url, tl.table_id)
                meta = [
                    ("Year", args.year),
                    ("District", dist_name),
                    ("Sector", sec.name),
                    ("Table", tl.table_id),
                    ("Description", tl.description or tl.label),
                ]
                write_table_workbook(xlsx_path, page.tables, meta=meta)
                local = builder.localize(raw_html, client.url(tl.url), depth=1)
                builder.write(f"tables/table{tl.table_id}.html", local)
                ok += 1
                entry["status"] = "ok"
                entry["rows"] = sum(len(t.rows) for t in page.tables)
                log.info("  [ok]   table %s -> %s", tl.table_id, xlsx_name)
            except Exception as exc:
                failed += 1
                entry["status"] = "failed"
                entry["error"] = str(exc)
                log.error("  [fail] table %s: %s", tl.table_id, exc)
            sec_manifest["tables"].append(entry)
            if args.delay:
                time.sleep(args.delay)
        manifest["sectors"].append(sec_manifest)

        # write the sector page into the mirror
        if sec_html:
            local = builder.localize(sec_html, client.url(sec.url), depth=1)
            builder.write(f"sectors/{sec_file}", local)

    # -- website landing + assets -----------------------------------------
    index_html = builder.localize(sectors_html, client.url(SELECT_PATH), depth=0)
    builder.write("index.html", index_html)
    builder.download_assets()

    (out_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    log.info("-" * 60)
    log.info("Done. tables: %d total, %d ok, %d skipped, %d failed",
             total, ok, skipped, failed)
    log.info("Website : %s/index.html", site_root)
    log.info("Excel   : %s", excel_root)
    if failed:
        log.warning("Some tables failed. Re-run with --resume to retry only those.")
    return 0 if failed == 0 else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="updes-spider",
        description="Scrape UP DES Spider district reports into a local "
                    "website mirror and sector-wise Excel files.",
    )
    p.add_argument("--year", default="2025", help="Year to select (default 2025)")
    p.add_argument("--dist", default="34",
                   help="District code, e.g. 34 = FARRUKHABAD (default 34)")
    p.add_argument("--out", default="updes", help="Output root folder (default: updes)")
    p.add_argument("--lang", default="en",
                   help="Language to request, e.g. 'en' or 'hi'. Empty = server default.")
    p.add_argument("--base-url", default="https://updes.up.nic.in/spiderreports")
    p.add_argument("--attempts", type=int, default=6,
                   help="Max attempts per request (default 6)")
    p.add_argument("--connect-timeout", type=float, default=30.0)
    p.add_argument("--read-timeout", type=float, default=240.0)
    p.add_argument("--delay", type=float, default=0.0,
                   help="Polite delay between table fetches, seconds")
    p.add_argument("--sectors", nargs="*", default=None,
                   help="Only scrape sectors whose name contains these substrings")
    p.add_argument("--assets-dir", nargs="*",
                   default=["assets/intialisePage.action_files"],
                   help="Local folders to source page assets from if the server "
                        "blocks asset downloads")
    p.add_argument("--resume", action="store_true", default=True,
                   help="Skip tables already exported (default on)")
    p.add_argument("--no-resume", dest="resume", action="store_false",
                   help="Re-fetch everything even if outputs exist")
    p.add_argument("--verify-tls", action="store_true",
                   help="Enable TLS verification (site chain is usually broken)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        return run(args)
    except KeyboardInterrupt:
        log.warning("Interrupted. Re-run with --resume to continue.")
        return 130
    except RuntimeError as exc:
        log.error("Could not reach the server: %s", exc)
        log.error("The site is slow and sometimes blocks rapid access. "
                  "Wait a few minutes and re-run the same command "
                  "(already-saved tables are skipped). Consider adding --delay 2.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
