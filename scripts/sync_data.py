#!/usr/bin/env python3
"""
sync_data.py
------------
Scheduled data pull for the Onsale Page View Comparison Dashboard.

What it does, each time it runs (via GitHub Action on a cron schedule):
  1. Auths to Google Drive with a service account.
  2. Walks the "Live Nation Onsale Reports" folder (and its year subfolders)
     looking for spreadsheets (native Google Sheets or uploaded .xlsx).
  3. Skips any file whose Drive `modifiedTime` hasn't changed since the last
     run (tracked in data/_sync_manifest.json), to avoid re-billing Claude
     API calls for unchanged sheets.
  4. For each new/changed file, pulls every tab as a text grid and sends it
     to Claude to extract normalized page-view records (these sheets are
     hand-maintained with merged header blocks, inconsistent hour columns,
     and mixed-in "comparable artist" reference tables -- regex/pandas
     parsing breaks constantly on this shape, so an LLM extraction step is
     used deliberately).
  5. Merges extracted records into data/onsales.json, replacing whatever
     records previously came from that same source file.
  6. Asks Claude to write a short set of precomputed insights (pacing vs
     venue benchmark, notable anomalies, cross-show comparisons) into
     data/insights.json, using data/onsales.json + data/venue-baselines.json
     as context. This is what the dashboard shows in its "Claude analysis"
     panel -- computed at sync time, not live in-browser, so no API key
     needs to be exposed to the client.

Required environment variables (set as GitHub Actions secrets):
  GDRIVE_SERVICE_ACCOUNT_JSON  - full JSON key for a Google service account
                                 that has been shared view access on the
                                 "Live Nation Onsale Reports" Drive folder
  GDRIVE_FOLDER_ID             - Drive folder ID of "Live Nation Onsale Reports"
  ANTHROPIC_API_KEY            - Claude API key
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from io import BytesIO

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

import anthropic

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
ONSALES_PATH = os.path.join(DATA_DIR, "onsales.json")
BASELINES_PATH = os.path.join(DATA_DIR, "venue-baselines.json")
INSIGHTS_PATH = os.path.join(DATA_DIR, "insights.json")
MANIFEST_PATH = os.path.join(DATA_DIR, "_sync_manifest.json")

CLAUDE_MODEL = "claude-sonnet-4-5"

GOOGLE_SHEET_MIME = "application/vnd.google-apps.spreadsheet"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"


# --------------------------------------------------------------------------
# Drive helpers
# --------------------------------------------------------------------------

def get_drive_service():
    info = json.loads(os.environ["GDRIVE_SERVICE_ACCOUNT_JSON"])
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


def get_sheets_service():
    info = json.loads(os.environ["GDRIVE_SERVICE_ACCOUNT_JSON"])
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    return build("sheets", "v4", credentials=creds)


def walk_drive_folder(drive, folder_id, depth=0, max_depth=6):
    """Recursively list spreadsheet files under a Drive folder."""
    files = []
    page_token = None
    while True:
        resp = drive.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name, mimeType, modifiedTime, shortcutDetails)",
            pageToken=page_token,
            pageSize=200,
        ).execute()
        for f in resp.get("files", []):
            mime = f["mimeType"]
            if mime == SHORTCUT_MIME:
                target = f.get("shortcutDetails", {})
                mime = target.get("targetMimeType", "")
                f = {**f, "id": target.get("targetId", f["id"]), "mimeType": mime}
            if mime == FOLDER_MIME and depth < max_depth:
                files.extend(walk_drive_folder(drive, f["id"], depth + 1, max_depth))
            elif mime in (GOOGLE_SHEET_MIME, XLSX_MIME):
                files.append(f)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return files


def forward_fill_header_row(row):
    """Approximate merged header cells: carry the last non-blank value
    across blank cells in a row (Sheets API returns merged cells as blank
    except the top-left one)."""
    filled = []
    last = ""
    for cell in row:
        if cell.strip():
            last = cell.strip()
            filled.append(last)
        else:
            filled.append(f"[same: {last}]" if last else "")
    return filled


def sheet_to_text(sheets_service, spreadsheet_id):
    """Return a text blob covering every tab of a native Google Sheet."""
    meta = sheets_service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    blocks = []
    for sheet in meta.get("sheets", []):
        title = sheet["properties"]["title"]
        try:
            result = sheets_service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id, range=title
            ).execute()
        except Exception as e:
            blocks.append(f"--- TAB: {title} (could not read: {e}) ---")
            continue
        rows = result.get("values", [])
        lines = [f"--- TAB: {title} ---"]
        for i, row in enumerate(rows):
            row = [str(c) for c in row]
            if i < 3:
                row = forward_fill_header_row(row)
            lines.append(" | ".join(row))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def xlsx_to_text(drive, file_id):
    """Download an uploaded .xlsx and render every sheet as text."""
    import openpyxl

    request = drive.files().get_media(fileId=file_id)
    buf = BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buf.seek(0)
    wb = openpyxl.load_workbook(buf, data_only=True)
    blocks = []
    for ws in wb.worksheets:
        lines = [f"--- TAB: {ws.title} ---"]
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            cells = ["" if c is None else str(c) for c in row]
            if i < 3:
                cells = forward_fill_header_row(cells)
            if any(c.strip() for c in cells):
                lines.append(" | ".join(cells))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# --------------------------------------------------------------------------
# Claude extraction
# --------------------------------------------------------------------------

EXTRACTION_SYSTEM_PROMPT = """You extract structured page-view data from messy, hand-maintained \
"onsale report" spreadsheets used by a ticketing company to track demand for concert onsales.

Each sheet tracks one tour/artist's page views and queue sizes since announcement, broken out \
by show (date/venue). Sheets often ALSO contain smaller "comparable stats" reference blocks for \
OTHER artists, pasted in purely for benchmarking -- these are not part of the primary tour and \
must be flagged with is_comparable_reference = true.

Known quirks to handle:
- Header rows use merged cells; blank cells right after a label (or cells you see annotated \
"[same: X]") belong to the same merged group as the label to their left.
- Column groups are usually "EDP Views" (Event/Ticket page views) and "ADP Views" (Artist page \
views), each broken into elapsed-time buckets like ".+12 hours", ".+24 hours", ".+72 hours", \
".+96 hours/4 days" -- these vary sheet to sheet, extract whatever buckets are actually present.
- Some rows have a secondary breakdown by market/country code (e.g. TM_UK, TM_DE, TM_FR) -- \
capture this if present as market_breakdown, otherwise omit it.
- Cells sometimes contain text instead of a number, e.g. "FR on different system, stats not \
available" -- treat that as null and put a short note instead of guessing a number.
- Dates appear in many formats ("6-October-2026", "Mon Sep 29", "Weds 7-Oct-26"). Resolve to \
ISO 8601 (YYYY-MM-DD) using the year context given in the file name/path; if the year is truly \
ambiguous, use your best judgement from surrounding dates in the same table and note the guess.
- Presale/onsale peak queue figures may appear in a separate small table elsewhere in the sheet \
(not per-hour) -- attach them to the matching show by venue+date if you can find a match.

Return ONLY a JSON array (no prose, no markdown fences). Each element:
{
  "artist": string,
  "tour": string | null,
  "is_comparable_reference": boolean,
  "venue": string,
  "city": string,
  "country": string,
  "date": string | null,       // ISO 8601 if resolvable, else null
  "year": number | null,
  "edp_views_by_hour": { "<hours>": number, ... },
  "adp_views_by_hour": { "<hours>": number, ... },
  "market_breakdown": { "<market_code>": { "<hours>": number, ... }, ... } | null,
  "presale_peak_queue": number | null,
  "onsale_peak_queue": number | null,
  "notes": string
}

If a row genuinely has no usable numeric data at all, skip it rather than emitting an empty record."""


def extract_records_with_claude(client, source_file, sheet_text):
    # Guard against sending an empty or absurdly large blob
    if not sheet_text.strip():
        return []
    text = sheet_text[:60000]  # keep well within context budget per file

    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=8000,
        system=EXTRACTION_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Source file: {source_file}\n\n{text}",
        }],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```(json)?", "", raw).strip()
    raw = re.sub(r"```$", "", raw).strip()
    try:
        records = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"  ! Claude output for {source_file} wasn't valid JSON ({e}); skipping file")
        return []
    return records if isinstance(records, list) else []


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def generate_insights_with_claude(client, onsales, baselines):
    system = """You are a demand analyst for a ticketing company. Given normalized onsale \
page-view/queue records and venue benchmark bands, write a short list of precomputed insights \
for a dashboard. Focus on: which shows are pacing above/below their venue's typical benchmark, \
notable anomalies, and useful cross-show comparisons. Be concise and concrete, cite numbers. \
Return ONLY a JSON array of objects: {"headline": string, "detail": string, "related_ids": [string]} \
Return at most 12 insights, ranked by how notable they are."""

    payload = {"onsales": onsales, "venue_baselines": baselines}
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4000,
        system=system,
        messages=[{"role": "user", "content": json.dumps(payload)[:180000]}],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```(json)?", "", raw).strip()
    raw = re.sub(r"```$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def main():
    folder_id = os.environ["GDRIVE_FOLDER_ID"]
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    drive = get_drive_service()
    sheets_service = get_sheets_service()

    manifest = load_json(MANIFEST_PATH, {})
    onsales = load_json(ONSALES_PATH, [])
    baselines = load_json(BASELINES_PATH, [])

    print("Walking Drive folder...")
    files = walk_drive_folder(drive, folder_id)
    print(f"Found {len(files)} spreadsheet(s)")

    # Keep records from files we're not touching this run
    by_source = {}
    for rec in onsales:
        by_source.setdefault(rec.get("source_file_id") or rec.get("source_file"), []).append(rec)

    changed_any = False
    for f in files:
        fid, name, mtime, mime = f["id"], f["name"], f["modifiedTime"], f["mimeType"]
        if manifest.get(fid) == mtime:
            continue  # unchanged since last sync

        print(f"Parsing changed file: {name}")
        try:
            if mime == GOOGLE_SHEET_MIME:
                text = sheet_to_text(sheets_service, fid)
            else:
                text = xlsx_to_text(drive, fid)
        except Exception as e:
            print(f"  ! Failed to read {name}: {e}")
            continue

        records = extract_records_with_claude(client, name, text)
        now = datetime.now(timezone.utc).isoformat()
        for r in records:
            r["id"] = slugify(f"{r.get('artist')}-{r.get('venue')}-{r.get('date') or r.get('year')}")
            r["source_file"] = name
            r["source_file_id"] = fid
            r["last_synced"] = now
            r.setdefault("tier", None)
            r.setdefault("presale_peak_queue", None)
            r.setdefault("onsale_peak_queue", None)

        by_source[fid] = records
        manifest[fid] = mtime
        changed_any = True
        print(f"  -> extracted {len(records)} record(s)")

    if changed_any:
        onsales = [rec for group in by_source.values() for rec in group]
        save_json(ONSALES_PATH, onsales)
        save_json(MANIFEST_PATH, manifest)
        print(f"Wrote {len(onsales)} total records to {ONSALES_PATH}")

        print("Generating precomputed insights...")
        insights = generate_insights_with_claude(client, onsales, baselines)
        save_json(INSIGHTS_PATH, {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "insights": insights,
        })
        print(f"Wrote {len(insights)} insight(s) to {INSIGHTS_PATH}")
    else:
        print("No changed files since last sync -- nothing to do.")


if __name__ == "__main__":
    sys.exit(main())
