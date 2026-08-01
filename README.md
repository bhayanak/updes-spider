# UP DES Spider — District Reports Scraper

Downloads the **Zila Spider Reports** published at
<https://updes.up.nic.in/spiderreports/intialisePage.action> for a chosen
**year** and **district**, and produces two things:

1. **A browsable offline copy of the website** — same layout as the live site
   (sector navigation on the left, table lists on the right, and every data
   table), with assets and links rewritten to work locally.
2. **Sector-wise Excel files** — one `.xlsx` per table, grouped into a folder
   per sector.

The live site is slow (individual tables can take 15+ seconds) and frequently
fails or drops the session. This tool is built to survive that: it retries at
the network level and the application level, re-establishes the
year/district session when it detects a bad response, validates that a table
actually contains data before accepting it, and can **resume** a partial run.

## Output layout

```
updes/                                   # --out (default: updes)
  2025_34_FARRUKHABAD/                   # <year>_<distcode>_<districtname>
    website/
      index.html                         # sectors landing page (open this)
      sectors/01_..._.html               # one page per sector
      tables/table1.html ...             # one page per table
      assets/                            # css, images, js
    excel/
      01_<sector>/Table_1_....xlsx       # one workbook per table
      02_<sector>/...
    manifest.json                        # machine-readable run summary
```

Tables with multiple sub-tables (e.g. Table 2A / 2B) become multiple sheets in
one workbook.

## Setup

```bash
For linux:
python3 -m venv .venv
.\.venv\bin\activate.bat
pip install -r requirements.txt

Get data:
python -m updes_spider --year 2025 --dist 23  --delay 1
```

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

Default run (year 2025, district 34 = FARRUKHABAD):

```bash
python -m updes_spider
```

Any year / district:

```bash
python -m updes_spider --year 2024 --dist 45  --delay 1    # 45 = PRAYAGRAJ
```

Useful options:

| Option | Meaning | Default |
| --- | --- | --- |
| `--year` | Year to select | `2025` |
| `--dist` | District code (see the dropdown on the site) | `34` |
| `--out` | Output root folder | `updes` |
| `--lang` | Request language (`en` / `hi`; empty = server default) | `en` |
| `--attempts` | Max attempts per request | `6` |
| `--read-timeout` | Read timeout in seconds (tables are slow) | `240` |
| `--connect-timeout` | Connect timeout in seconds | `30` |
| `--delay` | Polite delay between table fetches (seconds) | `0` |
| `--sectors` | Only sectors whose name contains these words | all |
| `--resume` / `--no-resume` | Skip / re-fetch already-saved tables | resume on |
| `--verify-tls` | Enable TLS verification (site chain is usually broken) | off |
| `-v` | Verbose logging | off |

### Robustness / recovering from failures

- **Re-run to continue.** By default the tool *resumes*: tables that already
  have both an `.xlsx` and an HTML page are skipped, so re-running only
  re-fetches what failed. If the server rate-limits you (connections start
  failing instantly), wait a few minutes and re-run the same command.
- **Be gentle** if the site keeps dropping you: add `--delay 2` and lower
  load, e.g. scrape one sector at a time with `--sectors कृषि` (or the English
  name if `--lang en`).
- **Force a clean re-fetch** of everything with `--no-resume`.

## How it works

1. `GET intialisePage.action` — warm the session (JSESSIONID) and read the
   district dropdown for the display name.
2. `POST getGenralNews.action` with `year_tab00` + `dist` — selects the
   year/district for the session and returns the sectors page.
3. For each sector link (left pane) `GET <sector>.jsp` — read that sector's
   table list.
4. For each table `GET gettable<ID>Report.action` — the actual data. The
   response is validated to contain a real data table before it is accepted;
   otherwise the session is re-established and the request retried.

## Notes

- TLS verification is disabled by default because the government server serves
  an incomplete certificate chain. Use `--verify-tls` to turn it back on.
- The site's default content language is Hindi (Devanagari). `--lang en` asks
  for English; if the server ignores it, data is still captured in whatever
  language is returned — the layout is identical either way.
- This scrapes **public** government data for read-only archival. Please run it
  politely (use `--delay`) to avoid overloading the server.
