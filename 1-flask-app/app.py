import csv
import json
import math
import os
import re
import sqlite3
import sys
import time
from urllib.parse import urlencode
from urllib.request import urlopen

from flask import Flask, jsonify, render_template, request, send_from_directory

import pandas as pd

app = Flask(__name__)

BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, "data")
TEYYUB_DIR = os.path.join(DATA_DIR, "teyyub_data")
RAGU_DIR = os.path.join(DATA_DIR, "ragu_nlp_10k_classification")
MARYAM_DIR = os.path.join(DATA_DIR, "maryam_matching")
NOLAN_DIR = os.path.join(DATA_DIR, "nolan")

DATA_PATH = os.path.join(DATA_DIR, "crsp_public_companies.csv")
RAGU_PATH = os.path.join(RAGU_DIR, "predictions_for_Entire_10K_Fillings.csv")
RAGU_LABEL = "NLP 10-K Classification (Ragu)"

# ~1.7 GB JSONL: one JSON object per line, {cik, adsh, filing_date, sentences}.
# Too large to hold in memory, so a byte-offset index is built once and each
# filing is read back on demand with seek().
NOLAN_PATH = os.path.join(NOLAN_DIR, "full_10k_sentences.jsonl")
NOLAN_INDEX_PATH = os.path.join(NOLAN_DIR, "full_10k_sentences.index.json")
NOLAN_LABEL = "Full 10-K Sentences (Nolan)"
NOLAN_SENTENCE_PAGE = 200

# Per-item sentence CSVs (~1.6 GB in total). They are loaded once into a SQLite
# database so the pages can filter/sort/paginate without holding them in RAM.
RAGHAVENDRA_DIR = os.path.join(DATA_DIR, "raghavendra")
RAGHAVENDRA_DB = os.path.join(RAGHAVENDRA_DIR, "raghavendra.sqlite")

RAGHAVENDRA_FILES = {
    "item_1a": "layer0_predictions_Item_1A_10k_sentences.csv",
    "item_7": "layer0_predictions_Item_7_10k_sentences.csv",
    "item_8": "layer0_predictions_Item_8_10k_sentences.csv",
    "item_9a": "layer0_predictions_Item_9A_10k_sentences.csv",
    "item_9b": "layer0_predictions_Item_9B_10k_sentences.csv",
}

RAGHAVENDRA_LABELS = {
    "item_1a": "Item 1A – Risk Factors",
    "item_7": "Item 7 – MD&A",
    "item_8": "Item 8 – Financial Statements",
    "item_9a": "Item 9A – Controls & Procedures",
    "item_9b": "Item 9B – Other Information",
}

RAGHAVENDRA_COLUMNS = [
    "cik", "adsh", "filing_date", "section", "sentence", "word_count",
    "layer0_prediction",
]
RAGHAVENDRA_NUMERIC = {"cik", "filing_date", "word_count"}
RAGHAVENDRA_CATEGORICAL = {"layer0_prediction"}
RAGHAVENDRA_BATCH = 50000
# Filtered CSV exports are capped so the browser never receives millions of rows.
RAGHAVENDRA_MAX_EXPORT = 100000

# Extracted from 10k_and_spicework.xlsx (that workbook is a truncated download,
# so the rows it still contains were exported to CSV).
MARYAM_PATH = os.path.join(MARYAM_DIR, "10k_and_spicework.csv")
MARYAM_LABEL = "10-K \u2194 Spiceworks Matching (Maryam)"
MARYAM6_PATH = os.path.join(MARYAM_DIR, "final_merge_swbd_10k_6dbs.csv")
MARYAM6_LABEL = "10-K \u2194 SWDB \u2194 6 Incident DBs"
MARYAM6_DB_COLS = ["ransomwarelive", "maryland", "grained", "eu", "veris", "temple"]
MARYAM6_DB_LABELS = {
    "ransomwarelive": "Ransomware.live",
    "maryland": "Maryland AG",
    "grained": "Grained",
    "eu": "EU",
    "veris": "VERIS (VCDB)",
    "temple": "Temple",
}

TEYYUB_FILES = {
    "companies_2plus_10k": "companies_2plus_10k.csv",
    "matched_score100_wide": "matched_score100_wide.csv",
    "sub_merged": "sub_merged.csv",
}

TEYYUB_LABELS = {
    "companies_2plus_10k": "Companies (2+ 10-K filings)",
    "matched_score100_wide": "Matched Score 100 (wide)",
    "sub_merged": "Sub Merged",
}

# When a dataset has more rows than this, the unfiltered/unsorted initial
# view is capped to PREVIEW_LIMIT rows. Filters/sorts trigger the full scan.
LARGE_THRESHOLD = 20000
PREVIEW_LIMIT = 5000

# Per-dataset stat cards: list of (label, column-name-for-nunique).
# A None column means "use total row count".
TEYYUB_STATS = {
    "companies_2plus_10k": [
        ("Records", None),
        ("Unique Companies (CIK)", "cik"),
        ("Unique Names", "name"),
        ("SIC Codes", "sic"),
        ("Quarters", "quarter"),
        ("Fiscal Years", "fy"),
        ("Form Types", "form"),
        ("Countries (Biz Addr)", "countryba"),
        ("States (Biz Addr)", "stprba"),
    ],
    "matched_score100_wide": [
        ("Records", None),
        ("Unique Companies (CIK)", "cik"),
        ("Unique Company Names", "company"),
        ("Event Dates", "event_date"),
        ("EUREPOC IDs", "eurepoc_id"),
        ("Maryland IDs", "maryland_id"),
        ("Ransomware IDs", "ransomware_id"),
        ("Temple IDs", "temple_id"),
        ("VERIS IDs", "veris_id"),
    ],
    "sub_merged": [
        ("Records", None),
        ("Unique Companies (CIK)", "cik"),
        ("Unique Names", "name"),
        ("SIC Codes", "sic"),
        ("Quarters", "quarter"),
        ("Fiscal Years", "fy"),
        ("Form Types", "form"),
        ("Countries (Biz Addr)", "countryba"),
        ("States (Biz Addr)", "stprba"),
    ],
}

# Public Zotero group library (read-only, no API key needed).
ZOTERO_GROUP_ID = "6550970"
ZOTERO_BASE_URL = f"https://api.zotero.org/groups/{ZOTERO_GROUP_ID}"
ZOTERO_PAGE_SIZE = 100
ZOTERO_CACHE_TTL = 900  # seconds

# Cache loaded DataFrames to avoid re-reading CSVs on every request.
_df_cache = {}
_stats_cache = {}
_crsp_cache = {"df": None, "records": None}
_zotero_cache = {"records": None, "time": 0.0, "group_url": ""}
_nolan_cache = {"df": None, "stats": None}
_nolan_filing_cache = {"row": None, "sentences": None, "items": None}
_raghavendra_cache = {"ready": False, "totals": {}, "stats": {}}


def _load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, low_memory=False)
    return df.where(pd.notnull(df), "")


def get_teyyub_df(key: str) -> pd.DataFrame:
    if key not in TEYYUB_FILES:
        raise KeyError(key)
    if key not in _df_cache:
        path = os.path.join(TEYYUB_DIR, TEYYUB_FILES[key])
        _df_cache[key] = _load_csv(path)
    return _df_cache[key]


def _nunique_nonblank(series: pd.Series) -> int:
    s = series.astype(str)
    s = s[s != ""]
    return int(s.nunique())


def get_teyyub_stats(key: str):
    if key not in TEYYUB_STATS:
        return []
    if key in _stats_cache:
        return _stats_cache[key]
    df = get_teyyub_df(key)
    out = []
    total = int(len(df))
    for label, col in TEYYUB_STATS[key]:
        if col is None:
            out.append({"label": label, "value": total})
        elif col in df.columns:
            out.append({"label": label, "value": _nunique_nonblank(df[col])})
        else:
            out.append({"label": label, "value": 0})
    _stats_cache[key] = out
    return out


def get_crsp_records():
    if _crsp_cache["records"] is None:
        df = _load_csv(DATA_PATH)
        _crsp_cache["df"] = df
        _crsp_cache["records"] = df.to_dict(orient="records")
    return _crsp_cache["records"]


def get_ragu_df() -> pd.DataFrame:
    if "__ragu__" not in _df_cache:
        _df_cache["__ragu__"] = _load_csv(RAGU_PATH)
    return _df_cache["__ragu__"]


def get_maryam_df() -> pd.DataFrame:
    if "__maryam__" not in _df_cache:
        _df_cache["__maryam__"] = _load_csv(MARYAM_PATH)
    return _df_cache["__maryam__"]


def get_maryam6_df() -> pd.DataFrame:
    """SWDB accounts matched to 10-K filers plus 6 incident databases.
    Adds a computed `incidents` column (number of DBs with a match)."""
    if "__maryam6__" not in _df_cache:
        df = _load_csv(MARYAM6_PATH)
        df["incidents"] = (
            df[MARYAM6_DB_COLS].astype(str).ne("").sum(axis=1).astype(int)
        )
        cols = ["swdb_acc_id", "swdb_acc_name", "sec10k", "incidents"] + MARYAM6_DB_COLS
        _df_cache["__maryam6__"] = df[[c for c in cols if c in df.columns]]
    return _df_cache["__maryam6__"]


def build_nolan_index():
    """Single pass over the JSONL recording the byte offset of every filing."""
    records = []
    offset = 0
    with open(NOLAN_PATH, "rb") as fh:
        for row, raw in enumerate(fh):
            length = len(raw)
            stripped = raw.strip()
            if stripped:
                obj = json.loads(stripped.decode("utf-8"))
                sentences = obj.get("sentences") or []
                filing_date = str(obj.get("filing_date", ""))
                records.append({
                    "row": row,
                    "cik": str(obj.get("cik", "")),
                    "adsh": str(obj.get("adsh", "")),
                    "filing_date": filing_date,
                    "year": filing_date[:4],
                    "sentences": len(sentences),
                    "characters": sum(len(s) for s in sentences),
                    "offset": offset,
                    "length": length,
                })
            offset += length
    return records


def load_nolan_index():
    """Returns the offset index, rebuilding it when the JSONL has changed."""
    stat = os.stat(NOLAN_PATH)
    signature = {"size": stat.st_size, "mtime": int(stat.st_mtime)}
    if os.path.exists(NOLAN_INDEX_PATH):
        try:
            with open(NOLAN_INDEX_PATH, "r", encoding="utf-8") as fh:
                cached = json.load(fh)
            if cached.get("signature") == signature:
                return cached["records"]
        except Exception:
            pass
    records = build_nolan_index()
    try:
        with open(NOLAN_INDEX_PATH, "w", encoding="utf-8") as fh:
            json.dump({"signature": signature, "records": records}, fh)
    except Exception:
        pass
    return records


def get_nolan_df() -> pd.DataFrame:
    """Filing-level metadata table (one row per 10-K filing)."""
    if _nolan_cache["df"] is None:
        _nolan_cache["df"] = pd.DataFrame(load_nolan_index())
    return _nolan_cache["df"]


# Columns exposed in the UI; offset/length are internal bookkeeping.
NOLAN_COLUMNS = ["row", "cik", "adsh", "filing_date", "year", "sentences", "characters"]


def get_nolan_stats(df: pd.DataFrame):
    total = int(len(df))
    if not total:
        return [{"label": "Filings", "value": 0}]
    sentences = pd.to_numeric(df["sentences"], errors="coerce").fillna(0)
    characters = pd.to_numeric(df["characters"], errors="coerce").fillna(0)
    return [
        {"label": "Filings", "value": total},
        {"label": "Unique Companies (CIK)", "value": _nunique_nonblank(df["cik"])},
        {"label": "Years Covered", "value": _nunique_nonblank(df["year"])},
        {"label": "Total Sentences", "value": int(sentences.sum())},
        {"label": "Avg Sentences / Filing", "value": int(round(sentences.mean()))},
        {"label": "Max Sentences", "value": int(sentences.max())},
        {"label": "Min Sentences", "value": int(sentences.min())},
        {"label": "Total Characters (M)", "value": round(characters.sum() / 1e6, 1)},
    ]


def read_nolan_sentences(row: int):
    """Reads one filing back from disk using its indexed byte offset."""
    if _nolan_filing_cache["row"] == row:
        return _nolan_filing_cache["sentences"]
    df = get_nolan_df()
    match = df[df["row"] == row]
    if match.empty:
        raise KeyError(row)
    entry = match.iloc[0]
    with open(NOLAN_PATH, "rb") as fh:
        fh.seek(int(entry["offset"]))
        raw = fh.read(int(entry["length"]))
    obj = json.loads(raw.decode("utf-8"))
    sentences = [str(s) for s in (obj.get("sentences") or [])]
    _nolan_filing_cache["row"] = row
    _nolan_filing_cache["sentences"] = sentences
    _nolan_filing_cache["items"] = assign_nolan_items(sentences)
    return sentences


def read_nolan_filing(row: int):
    """Returns (sentences, per-sentence item labels) for one filing."""
    sentences = read_nolan_sentences(row)
    return sentences, _nolan_filing_cache["items"]


# Matches an "Item N" / "Item NA" token anywhere in a sentence. Sentence
# tokenisation often glues headings to surrounding text, so headings cannot be
# assumed to start a sentence.
NOLAN_ITEM_RE = re.compile(
    r"item\s+(\d{1,2})\s*\(?([abc])?\)?\s*[.:\-\u2013\u2014\x96\x97\s]",
    re.IGNORECASE,
)
# Cross-references look like "in Part II, Item 7 …", "described in \x93Part
# I\x97Item 1 …" or quoted, e.g. see "Item 7. …" — a reference word
# (optionally followed by a quoted/dashed Part number), a comma-terminated
# Part prefix, a comma or a quote right before the token, or a quote /
# "of this Form…" tail right after it. Quotes and dashes appear both as
# Unicode and as raw CP1252 bytes (\x91-\x94, \x96, \x97).
_Q = "[\"\u201c\u201d'\u2018\u2019\x91-\x94`]"
_PART = r"part\s+[ivx]+\s*[,.]?\s*[-\u2013\u2014\x96\x97]?\s*"
NOLAN_XREF_BEFORE_RE = re.compile(
    r"(?:"
    r"(?:\bsee|\bin|\bunder|\brefer\s+to|\bas\s+well\s+as|\band)\s*"
    + _Q + r"*\s*(?:" + _PART + r")?"
    r"|" + _Q + r"\s*" + _PART +
    r"|part\s+[ivx]+\s*,\s*"
    r"|,\s*"
    r")" + _Q + r"*\s*$"
    r"|" + _Q + r"\s*$",
    re.IGNORECASE,
)
NOLAN_XREF_AFTER_RE = re.compile(
    r"^\s*(?:(?:of|to|in|under)\s+(?:this|the|our|form)\b"
    r"|[\"\u201c\u201d\u2018\u2019\x91-\x94])",
    re.IGNORECASE,
)

# Canonical item titles. A candidate heading only counts when its own title
# appears right after the "Item N" token — bare fragments like "Item 8." are
# cross-references, not section starts.
NOLAN_ITEM_TITLES = {
    "1": r"business",
    "1A": r"risk\s+factors",
    "1B": r"unresolved\s+staff",
    "1C": r"cybersecurity",
    "2": r"propert",
    "3": r"legal\s+proceedings",
    "4": r"mine\s+safety|submission\s+of\s+matters|removed\s+and\s+reserved|reserved",
    "5": r"market\s+for",
    "6": r"selected\s+financial|reserved",
    # Apostrophes survive as many byte forms (\u2019, \x92, ', `) — allow any
    # 0-2 non-word chars. Titles are sometimes split around the token, so
    # "discussion and analysis" alone also confirms Item 7.
    "7": r"management\W{0,2}s?\s+discussion|discussion\s+and\s+analysis",
    "7A": r"quantitative\s+and\s+qualitative",
    "8": r"financial\s+statements",
    "9": r"changes\s+in\s+and\s+disagreements",
    "9A": r"controls\s+and\s+procedures",
    "9B": r"other\s+information",
    "9C": r"disclosure\s+regarding\s+foreign",
    "10": r"directors|trustees",
    "11": r"executive\s+compensation",
    "12": r"security\s+ownership",
    "13": r"certain\s+relationships",
    "14": r"principal\s+account",
    "15": r"exhibit",
    "16": r"form\s*10-?k\s+summary",
}
NOLAN_TITLE_RES = {
    # Compiled against whitespace-compacted text: OCR artefacts often break
    # words ("Stateme nts") or space out letters ("R i s k"), so all spacing
    # is removed from both the window and the pattern before matching.
    label: re.compile(pattern.replace(r"\s+", ""), re.IGNORECASE)
    for label, pattern in NOLAN_ITEM_TITLES.items()
}

# Some filings head sections with the bare title, no "Item N" token. Only
# long, unambiguous titles are trusted, and only at the very start of a
# sentence (compacted match at position 0).
NOLAN_TITLE_ONLY_RES = {
    label: re.compile(pattern.replace(r"\s+", ""), re.IGNORECASE)
    for label, pattern in {
        "1A": r"risk\s+factors",
        "7": r"management\W{0,2}s?\s+discussion\s+and\s+analysis\s+of\s+"
             r"financial\s+condition",
        "7A": r"quantitative\s+and\s+qualitative\s+disclosures?\s+about\s+"
              r"market\s+risk",
        "8": r"financial\s+statements\s+and\s+supplementary\s+data",
    }.items()
}


def _nolan_item_order(label: str) -> int:
    m = re.match(r"(\d+)([ABC]?)", label)
    if not m:
        return -1
    return int(m.group(1)) * 10 + {"": 0, "A": 1, "B": 2, "C": 3}[m.group(2)]


# Friendly metadata for the reader view: official title + a plain-language
# one-liner for people who don't read SEC filings for a living.
NOLAN_ITEM_META = {
    "": {"title": "Cover page & contents",
         "plain": "Registrant details, checkboxes and the table of contents."},
    "1": {"title": "Business",
          "plain": "What the company does: products, customers, competition."},
    "1A": {"title": "Risk Factors", "cyber": True,
           "plain": "Everything management thinks could go wrong."},
    "1B": {"title": "Unresolved Staff Comments",
           "plain": "Open questions from SEC reviews — usually \u201cNone\u201d."},
    "1C": {"title": "Cybersecurity", "cyber": True,
           "plain": "How the company manages hacking and data-breach risk."},
    "2": {"title": "Properties",
          "plain": "Offices, plants and real estate the company uses."},
    "3": {"title": "Legal Proceedings",
          "plain": "Lawsuits and other legal trouble."},
    "4": {"title": "Mine Safety Disclosures",
          "plain": "Mining-safety data — for most companies \u201cNot applicable\u201d."},
    "5": {"title": "Market for the Company\u2019s Stock",
          "plain": "Share price, shareholders, dividends and buybacks."},
    "6": {"title": "Selected Financial Data",
          "plain": "Key figures snapshot — in recent filings \u201c[Reserved]\u201d."},
    "7": {"title": "Management\u2019s Discussion & Analysis (MD&A)", "cyber": True,
          "plain": "Management explains the year in its own words — the heart of the report."},
    "7A": {"title": "Market Risk",
           "plain": "Exposure to interest rates, currencies and commodity prices."},
    "8": {"title": "Financial Statements", "cyber": True,
          "plain": "The audited numbers: income, balance sheet, cash flow, notes."},
    "9": {"title": "Disagreements with Accountants",
          "plain": "Fights or switches with the auditors — usually \u201cNone\u201d."},
    "9A": {"title": "Controls and Procedures", "cyber": True,
           "plain": "How reliable the company\u2019s internal reporting checks are."},
    "9B": {"title": "Other Information",
           "plain": "Anything that didn\u2019t fit elsewhere."},
    "9C": {"title": "Foreign Audit Inspections",
           "plain": "Disclosure about audit inspections blocked abroad."},
    "10": {"title": "Directors & Governance",
           "plain": "Who runs the company and how."},
    "11": {"title": "Executive Compensation",
           "plain": "How much the executives get paid."},
    "12": {"title": "Security Ownership",
           "plain": "Who owns the shares, including insiders."},
    "13": {"title": "Related-Party Transactions",
           "plain": "Deals with insiders and director independence."},
    "14": {"title": "Accountant Fees",
           "plain": "What the auditors were paid."},
    "15": {"title": "Exhibits & Schedules",
           "plain": "Attached contracts and formal paperwork."},
    "16": {"title": "Form 10-K Summary",
           "plain": "Optional recap — almost always omitted."},
}

_nolan_names_cache = {"map": None}


def get_nolan_company_names():
    """cik -> most recent company name, taken from the SEC sub_merged data."""
    if _nolan_names_cache["map"] is None:
        mapping = {}
        try:
            df = get_teyyub_df("sub_merged")
            for cik, name in zip(df["cik"].astype(str), df["name"].astype(str)):
                cik = cik.strip()
                name = name.strip()
                if cik and name:
                    try:
                        mapping[str(int(float(cik)))] = name
                    except ValueError:
                        mapping[cik] = name
        except Exception:
            pass
        _nolan_names_cache["map"] = mapping
    return _nolan_names_cache["map"]


def assign_nolan_items(sentences):
    """Labels every sentence with the 10-K item section it belongs to.

    Two passes. First, collect candidate headings: an "Item N" token that is
    not a cross-reference by its surrounding text and whose canonical title
    appears right after the token (whitespace-compacted match, so OCR-broken
    words still count). Second, decide which candidates are real section
    starts: the cover page / table of contents is cut at the last confirmed
    "Item 1 — Business" in the first 30% of the document, and the longest
    non-decreasing chain of item orders is kept, so an isolated
    cross-reference that slipped through the guards cannot derail the real
    heading sequence.
    """
    n = len(sentences)
    labels = [""] * n
    cands = []  # (sentence index, label, numeric order)
    for i, sentence in enumerate(sentences):
        for m in NOLAN_ITEM_RE.finditer(sentence):
            before = sentence[max(0, m.start() - 32):m.start()]
            if NOLAN_XREF_BEFORE_RE.search(before):
                continue
            label = m.group(1) + (m.group(2) or "").upper()
            title_re = NOLAN_TITLE_RES.get(label)
            if title_re is None:
                continue
            window = sentence[m.end():]
            if len(window) < 90 and i + 1 < n:
                window = window + " " + sentences[i + 1]
            # The quoted-title / "of this Form…" xref check must see the
            # joined window: quoted titles often start the next sentence.
            if NOLAN_XREF_AFTER_RE.match(window):
                continue
            compact = re.sub(r"\s+", "", window[:110])
            tm = title_re.search(compact[:80])
            if not tm or tm.start() > 55:
                continue
            cands.append((i, label, _nolan_item_order(label)))
        if not cands or cands[-1][0] != i:
            # Fallback: bare-title heading with no "Item N" token.
            head = re.sub(r"\s+", "", sentence[:130])
            for label, tre in NOLAN_TITLE_ONLY_RES.items():
                if not tre.match(head):
                    continue
                # "Risk Factors" is short enough to start ordinary prose;
                # only heading-cased occurrences count.
                if label == "1A" and not re.match(
                        r"\s*(?:RISK\s+FACTORS|Risk\s+Factors)", sentence):
                    continue
                cands.append((i, label, _nolan_item_order(label)))
                break
    if not cands:
        return labels
    # Max-weight strictly-increasing subsequence of item orders (O(k^2),
    # k tiny). Weight = sentences governed until the next candidate (capped):
    # body headings govern long stretches and dominate; TOC rows and stray
    # cross-references govern almost nothing and get pruned. Strict increase
    # stops repeated cross-references of the same item from stacking weight.
    k = len(cands)
    weights = []
    for a in range(k):
        nxt = cands[a + 1][0] if a + 1 < k else n
        weights.append(max(1, min(nxt - cands[a][0], 40)))
    best = weights[:]
    prev = [-1] * k
    for a in range(k):
        for b in range(a):
            if cands[b][2] < cands[a][2] and best[b] + weights[a] > best[a]:
                best[a] = best[b] + weights[a]
                prev[a] = b
    end = max(range(k), key=lambda a: best[a])
    chain = []
    while end != -1:
        chain.append(cands[end])
        end = prev[end]
    chain.reverse()
    current = ""
    ptr = 0
    for i in range(n):
        first = None
        while ptr < len(chain) and chain[ptr][0] == i:
            if first is None:
                first = chain[ptr][1]
            current = chain[ptr][1]
            ptr += 1
        # When several headings share one glued sentence, the sentence
        # belongs to the first of them; the rest continues with the last.
        labels[i] = first if first is not None else current
    return labels


def raghavendra_signature():
    """Size+mtime of every source CSV, used to detect a stale SQLite build."""
    parts = []
    for key in sorted(RAGHAVENDRA_FILES):
        stat = os.stat(os.path.join(RAGHAVENDRA_DIR, RAGHAVENDRA_FILES[key]))
        parts.append(f"{key}:{stat.st_size}:{int(stat.st_mtime)}")
    return "|".join(parts)


def _open_raghavendra_db(read_only: bool = True) -> sqlite3.Connection:
    if read_only:
        uri = f"file:{RAGHAVENDRA_DB}?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=30)
    return sqlite3.connect(RAGHAVENDRA_DB, timeout=60)


def _import_raghavendra_csv(conn: sqlite3.Connection, key: str):
    path = os.path.join(RAGHAVENDRA_DIR, RAGHAVENDRA_FILES[key])
    conn.execute(f'DROP TABLE IF EXISTS "{key}"')
    conn.execute(
        f'CREATE TABLE "{key}" ('
        "cik INTEGER, adsh TEXT, filing_date INTEGER, "
        "section TEXT, sentence TEXT, word_count INTEGER, layer0_prediction TEXT)"
    )
    insert = f'INSERT INTO "{key}" VALUES (?, ?, ?, ?, ?, ?, ?)'

    def to_int(value):
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    rows = 0
    with open(path, "r", encoding="utf-8", newline="", errors="replace") as fh:
        reader = csv.DictReader(fh)
        batch = []
        for record in reader:
            batch.append((
                to_int(record.get("cik")),
                (record.get("adsh") or "").strip(),
                to_int(record.get("filing_date")),
                (record.get("section") or "").strip(),
                record.get("sentence") or "",
                to_int(record.get("word_count")),
                (record.get("layer0_prediction") or "").strip(),
            ))
            if len(batch) >= RAGHAVENDRA_BATCH:
                conn.executemany(insert, batch)
                rows += len(batch)
                batch = []
        if batch:
            conn.executemany(insert, batch)
            rows += len(batch)
    conn.commit()

    for col in ("cik", "adsh", "filing_date", "word_count", "layer0_prediction"):
        conn.execute(f'CREATE INDEX "idx_{key}_{col}" ON "{key}" ("{col}")')
    conn.commit()
    return rows


def build_raghavendra_db():
    """One-off import of all item CSVs into SQLite (takes several minutes)."""
    csv.field_size_limit(sys.maxsize)
    tmp_path = RAGHAVENDRA_DB + ".building"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    conn = sqlite3.connect(tmp_path, timeout=60)
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA cache_size=-200000")
        conn.execute("CREATE TABLE meta (name TEXT PRIMARY KEY, value TEXT)")
        for key in RAGHAVENDRA_FILES:
            ts = time.time()
            print(f"[raghavendra] importing {key}…", flush=True)
            rows = _import_raghavendra_csv(conn, key)
            print(
                f"[raghavendra]   {key}: {rows:,} rows in {time.time() - ts:.1f}s",
                flush=True,
            )
        conn.execute(
            "INSERT INTO meta VALUES ('signature', ?)", (raghavendra_signature(),)
        )
        conn.commit()
    finally:
        conn.close()
    os.replace(tmp_path, RAGHAVENDRA_DB)


def ensure_raghavendra_db():
    """Builds the SQLite database when it is missing or out of date."""
    if _raghavendra_cache["ready"]:
        return
    signature = raghavendra_signature()
    current = None
    if os.path.exists(RAGHAVENDRA_DB):
        try:
            conn = _open_raghavendra_db()
            try:
                row = conn.execute(
                    "SELECT value FROM meta WHERE name = 'signature'"
                ).fetchone()
                current = row[0] if row else None
            finally:
                conn.close()
        except sqlite3.Error:
            current = None
    if current != signature:
        build_raghavendra_db()
    _raghavendra_cache["ready"] = True


def _like_pattern(value: str) -> str:
    escaped = str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def raghavendra_where(filters):
    """Builds a parameterised WHERE clause; fields are whitelisted columns."""
    clauses, params = [], []
    for fl in filters:
        field = fl["field"]
        if field not in RAGHAVENDRA_COLUMNS:
            continue
        value = fl["value"]
        if value is None or value == "":
            continue
        ftype = fl["type"]
        if field in RAGHAVENDRA_NUMERIC and ftype in (
            "=", "==", "equals", "!=", ">", "<", ">=", "<="
        ):
            try:
                number = float(value)
            except (TypeError, ValueError):
                clauses.append("0")
                continue
            op = {"==": "=", "equals": "="}.get(ftype, ftype)
            clauses.append(f'"{field}" {op} ?')
            params.append(number)
        elif ftype in ("=", "==", "equals"):
            clauses.append(f'"{field}" = ? COLLATE NOCASE')
            params.append(str(value))
        elif ftype == "!=":
            clauses.append(f'"{field}" <> ? COLLATE NOCASE')
            params.append(str(value))
        else:
            clauses.append(f'CAST("{field}" AS TEXT) LIKE ? ESCAPE \'\\\'')
            params.append(_like_pattern(value))
    if not clauses:
        return "", []
    return " WHERE " + " AND ".join(clauses), params


def raghavendra_order_by(args):
    parts = []
    i = 0
    while True:
        field = args.get(f"sorters[{i}][field]")
        if field is None:
            break
        if field in RAGHAVENDRA_COLUMNS:
            direction = "DESC" if args.get(f"sorters[{i}][dir]") == "desc" else "ASC"
            parts.append(f'"{field}" {direction}')
        i += 1
    return " ORDER BY " + ", ".join(parts) if parts else ""


def get_raghavendra_total(key: str) -> int:
    if key not in _raghavendra_cache["totals"]:
        conn = _open_raghavendra_db()
        try:
            total = conn.execute(f'SELECT COUNT(*) FROM "{key}"').fetchone()[0]
        finally:
            conn.close()
        _raghavendra_cache["totals"][key] = int(total)
    return _raghavendra_cache["totals"][key]


def get_raghavendra_categories(key: str):
    cache = _raghavendra_cache.setdefault("categories", {})
    if key not in cache:
        conn = _open_raghavendra_db()
        try:
            cache[key] = {
                col: [
                    r[0] for r in conn.execute(
                        f'SELECT DISTINCT "{col}" FROM "{key}" ORDER BY 1'
                    ).fetchall()
                ]
                for col in sorted(RAGHAVENDRA_CATEGORICAL)
            }
        finally:
            conn.close()
    return cache[key]


def compute_raghavendra_stats(key: str, where: str, params):
    conn = _open_raghavendra_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT cik), COUNT(DISTINCT adsh), "
            "COUNT(DISTINCT filing_date / 10000), SUM(word_count), "
            "AVG(word_count), MIN(word_count), MAX(word_count) "
            f'FROM "{key}"{where}',
            params,
        ).fetchone()
        predictions = conn.execute(
            f'SELECT layer0_prediction, COUNT(*) FROM "{key}"{where} '
            "GROUP BY layer0_prediction ORDER BY COUNT(*) DESC",
            params,
        ).fetchall()
    finally:
        conn.close()

    total = int(row[0] or 0)
    prediction_cards = [
        {
            "label": label or "(no prediction)",
            "value": int(count),
            "pct": round(100 * count / total, 1) if total else 0,
        }
        for label, count in predictions
    ]
    return prediction_cards + [
        {"label": "Sentences", "value": total},
        {"label": "Unique Companies (CIK)", "value": int(row[1] or 0)},
        {"label": "Filings (adsh)", "value": int(row[2] or 0)},
        {"label": "Years Covered", "value": int(row[3] or 0)},
        {"label": "Total Words", "value": int(row[4] or 0)},
        {"label": "Avg Words / Sentence", "value": round(row[5] or 0, 1)},
        {"label": "Min Words", "value": int(row[6] or 0)},
        {"label": "Max Words", "value": int(row[7] or 0)},
    ]


def get_raghavendra_stats(key: str, filters):
    where, params = raghavendra_where(filters)
    if not where:
        if key not in _raghavendra_cache["stats"]:
            _raghavendra_cache["stats"][key] = compute_raghavendra_stats(key, "", [])
        return _raghavendra_cache["stats"][key]
    return compute_raghavendra_stats(key, where, params)


def fetch_zotero_page(path: str, start: int):
    """Returns (items, total_results) for one page of the group library."""
    params = urlencode({
        "format": "json",
        "include": "data",
        "limit": ZOTERO_PAGE_SIZE,
        "start": start,
    })
    with urlopen(f"{ZOTERO_BASE_URL}{path}?{params}", timeout=30) as response:
        total = int(response.headers.get("Total-Results", 0))
        items = json.loads(response.read().decode("utf-8"))
    return items, total


def fetch_zotero_items(path: str):
    items, total = fetch_zotero_page(path, 0)
    start = ZOTERO_PAGE_SIZE
    while start < total:
        page, _ = fetch_zotero_page(path, start)
        items.extend(page)
        start += ZOTERO_PAGE_SIZE
    return items


def format_creator(creator) -> str:
    if creator.get("name"):
        return creator["name"]
    first = creator.get("firstName", "")
    last = creator.get("lastName", "")
    return f"{first} {last}".strip()


def format_authors(creators) -> str:
    names = []
    for creator in creators:
        if creator.get("creatorType") == "author":
            names.append(format_creator(creator))
    if not names:
        for creator in creators:
            names.append(format_creator(creator))
    return "; ".join(names)


VENUE_FIELDS = [
    "publicationTitle",
    "proceedingsTitle",
    "bookTitle",
    "repository",
    "institution",
    "publisher",
    "websiteTitle",
]


def get_venue(data) -> str:
    for field in VENUE_FIELDS:
        value = data.get(field)
        if value:
            return value
    return ""


def get_year(date: str) -> str:
    match = re.search(r"[12]\d{3}", date or "")
    if match:
        return match.group(0)
    return ""


def format_item_type(item_type: str) -> str:
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", item_type)
    return spaced.capitalize()


def fetch_zotero_group_url() -> str:
    """Web library URL of the group, e.g. .../groups/6550970/my_group_slug."""
    with urlopen(ZOTERO_BASE_URL, timeout=30) as response:
        group = json.loads(response.read().decode("utf-8"))
    slug = group["links"]["alternate"]["href"].rstrip("/").split("/")[-1]
    return f"https://www.zotero.org/groups/{ZOTERO_GROUP_ID}/{slug}"


def index_children(all_items):
    """Maps a parent item key to the list of its child item data dicts."""
    children = {}
    for item in all_items:
        data = item["data"]
        parent = data.get("parentItem")
        if parent:
            children.setdefault(parent, []).append(data)
    return children


def summarize_children(children, item_key):
    """Counts notes/annotations and finds the first stored PDF attachment."""
    pdf_key = ""
    note_count = 0
    annotation_count = 0
    for child in children.get(item_key, []):
        if child["itemType"] == "note":
            note_count += 1
        elif child["itemType"] == "attachment":
            attachment_key = child["key"]
            if not pdf_key and child.get("contentType") == "application/pdf":
                pdf_key = attachment_key
            for grandchild in children.get(attachment_key, []):
                if grandchild["itemType"] == "annotation":
                    annotation_count += 1
    return pdf_key, note_count, annotation_count


def format_badges(has_pdf: bool, note_count: int, annotation_count: int) -> str:
    parts = []
    if has_pdf:
        parts.append("PDF")
    if note_count == 1:
        parts.append("1 note")
    elif note_count > 1:
        parts.append(f"{note_count} notes")
    if annotation_count == 1:
        parts.append("1 highlight")
    elif annotation_count > 1:
        parts.append(f"{annotation_count} highlights")
    return " · ".join(parts)


def build_literature_record(item, children, group_url):
    data = item["data"]
    key = data.get("key", "")
    date = data.get("date", "")
    item_type = data.get("itemType", "")
    pdf_key, note_count, annotation_count = summarize_children(children, key)

    if pdf_key:
        reader_url = f"{group_url}/items/{key}/attachment/{pdf_key}/reader"
    else:
        reader_url = f"{group_url}/items/{key}/item-details"

    return {
        "key": key,
        "item_type": item_type,
        "item_type_label": format_item_type(item_type),
        "title": data.get("title", ""),
        "authors": format_authors(data.get("creators", [])),
        "year": get_year(date),
        "date": date,
        "venue": get_venue(data),
        "doi": data.get("DOI", ""),
        "url": data.get("url", ""),
        "tags": [t.get("tag", "") for t in data.get("tags", [])],
        "abstract": data.get("abstractNote", ""),
        "date_added": (data.get("dateAdded") or "")[:10],
        "has_pdf": bool(pdf_key),
        "note_count": note_count,
        "annotation_count": annotation_count,
        "badges": format_badges(bool(pdf_key), note_count, annotation_count),
        "zotero_url": reader_url,
    }


def sort_key_recent_first(record):
    return (record["year"], record["date_added"])


def get_literature_records(force_refresh: bool = False):
    now = time.time()
    is_stale = now - _zotero_cache["time"] > ZOTERO_CACHE_TTL
    if force_refresh or _zotero_cache["records"] is None or is_stale:
        group_url = fetch_zotero_group_url()
        top_items = fetch_zotero_items("/items/top")
        children = index_children(fetch_zotero_items("/items"))
        _zotero_cache["group_url"] = group_url
        _zotero_cache["records"] = [
            build_literature_record(item, children, group_url) for item in top_items
        ]
        _zotero_cache["time"] = now
    return _zotero_cache["records"]


def count_values(records, key):
    """Counts how many records carry each value of a single-valued field."""
    counts = {}
    for record in records:
        value = record[key]
        if value:
            counts[value] = counts.get(value, 0) + 1
    return counts


def count_tags(records):
    counts = {}
    for record in records:
        for tag in record["tags"]:
            counts[tag] = counts.get(tag, 0) + 1
    return counts


def parse_filters(args):
    """Reads Tabulator remote filter params from the query string."""
    filters = []
    i = 0
    while True:
        f = args.get(f"filters[{i}][field]")
        if f is None:
            break
        filters.append({
            "field": f,
            "type": args.get(f"filters[{i}][type]", "like"),
            "value": args.get(f"filters[{i}][value]", ""),
        })
        i += 1
    return filters


def apply_filters(df: pd.DataFrame, filters) -> pd.DataFrame:
    if not filters:
        return df
    mask = pd.Series(True, index=df.index)
    for fl in filters:
        field = fl["field"]
        if field not in df.columns:
            continue
        val = fl["value"]
        if val is None or val == "":
            continue
        ftype = fl["type"]
        col_str = df[field].astype(str)
        sval = str(val)
        if ftype in ("=", "==", "equals"):
            m = col_str.str.lower() == sval.lower()
        elif ftype == "!=":
            m = col_str.str.lower() != sval.lower()
        elif ftype in (">", "<", ">=", "<="):
            try:
                num_col = pd.to_numeric(df[field], errors="coerce")
                num_val = float(sval)
                if ftype == ">":
                    m = num_col > num_val
                elif ftype == "<":
                    m = num_col < num_val
                elif ftype == ">=":
                    m = num_col >= num_val
                else:
                    m = num_col <= num_val
                m = m.fillna(False)
            except Exception:
                m = pd.Series(False, index=df.index)
        else:
            m = col_str.str.contains(sval, case=False, na=False, regex=False)
        mask &= m
    return df[mask]


def parse_sorters(args, df: pd.DataFrame):
    fields, dirs = [], []
    i = 0
    while True:
        sf = args.get(f"sorters[{i}][field]")
        if sf is None:
            break
        if sf in df.columns:
            fields.append(sf)
            dirs.append(args.get(f"sorters[{i}][dir]", "asc") == "asc")
        i += 1
    return fields, dirs


def warmup_caches():
    """Pre-load all CSVs into memory so the first request is fast."""
    t0 = time.time()
    print("[warmup] loading CRSP…", flush=True)
    get_crsp_records()
    for key in TEYYUB_FILES:
        ts = time.time()
        print(f"[warmup] loading {key}…", flush=True)
        get_teyyub_df(key)
        get_teyyub_stats(key)
        print(f"[warmup]   {key} loaded in {time.time() - ts:.1f}s", flush=True)
    ts = time.time()
    print("[warmup] loading ragu predictions…", flush=True)
    get_ragu_df()
    print(f"[warmup]   ragu loaded in {time.time() - ts:.1f}s", flush=True)
    ts = time.time()
    print("[warmup] loading maryam matching…", flush=True)
    get_maryam_df()
    get_maryam6_df()
    print(f"[warmup]   maryam loaded in {time.time() - ts:.1f}s", flush=True)
    ts = time.time()
    print("[warmup] indexing nolan 10-K sentences…", flush=True)
    try:
        get_nolan_df()
        print(f"[warmup]   nolan indexed in {time.time() - ts:.1f}s", flush=True)
    except Exception as e:
        print(f"[warmup]   nolan index failed: {e}", flush=True)
    ts = time.time()
    print("[warmup] preparing raghavendra item sentences…", flush=True)
    try:
        ensure_raghavendra_db()
        for key in RAGHAVENDRA_FILES:
            get_raghavendra_total(key)
            get_raghavendra_stats(key, [])
        print(f"[warmup]   raghavendra ready in {time.time() - ts:.1f}s", flush=True)
    except Exception as e:
        print(f"[warmup]   raghavendra failed: {e}", flush=True)
    print(f"[warmup] all datasets ready in {time.time() - t0:.1f}s", flush=True)


@app.route("/")
def index():
    return render_template("home.html")


@app.route("/companies")
def companies():
    return render_template("companies.html")


@app.route("/ragu")
def ragu():
    return render_template("ragu.html", dataset_label=RAGU_LABEL)


@app.route("/maryam")
def maryam():
    return render_template("maryam.html", dataset_label=MARYAM_LABEL)


@app.route("/maryam6")
def maryam6():
    return render_template("maryam6.html", dataset_label=MARYAM6_LABEL)


@app.route("/nolan")
def nolan():
    return render_template("nolan.html", dataset_label=NOLAN_LABEL)


@app.route("/nolan/reader/<int:row>")
def nolan_reader(row):
    df = get_nolan_df()
    if df[df["row"] == row].empty:
        return "Unknown filing", 404
    return render_template("nolan_reader.html", row=row)


@app.route("/raghavendra/<key>")
def raghavendra(key):
    if key not in RAGHAVENDRA_FILES:
        return "Unknown dataset", 404
    return render_template(
        "raghavendra.html",
        dataset_key=key,
        dataset_label=RAGHAVENDRA_LABELS[key],
        source_file=RAGHAVENDRA_FILES[key],
    )


@app.route("/literature")
def literature():
    force_refresh = request.args.get("refresh") == "1"
    records = sorted(
        get_literature_records(force_refresh),
        key=sort_key_recent_first,
        reverse=True,
    )

    year_counts = count_values(records, "year")
    type_counts = count_values(records, "item_type_label")
    tag_counts = count_tags(records)

    filters = [
        {
            "key": "type",
            "label": "All types",
            "options": [{"value": v, "count": type_counts[v]} for v in sorted(type_counts)],
        },
        {
            "key": "year",
            "label": "All years",
            "options": [
                {"value": v, "count": year_counts[v]}
                for v in sorted(year_counts, reverse=True)
            ],
        },
        {
            "key": "tag",
            "label": "All topics",
            "options": [{"value": v, "count": tag_counts[v]} for v in sorted(tag_counts)],
        },
    ]

    return render_template(
        "literature.html",
        records=records,
        filters=filters,
        group_url=_zotero_cache["group_url"] + "/library",
    )


@app.route("/api/literature")
def literature_api():
    force_refresh = request.args.get("refresh") == "1"
    return jsonify(get_literature_records(force_refresh))


@app.route("/imgs/<path:filename>")
def imgs(filename):
    return send_from_directory(os.path.join(BASE_DIR, "imgs"), filename)


@app.route("/api/companies")
def companies_api():
    try:
        return jsonify(get_crsp_records())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/teyyub/<key>")
def teyyub(key):
    if key not in TEYYUB_FILES:
        return "Unknown dataset", 404
    return render_template(
        "teyyub.html",
        dataset_key=key,
        dataset_label=TEYYUB_LABELS[key],
        datasets=[{"key": k, "label": TEYYUB_LABELS[k]} for k in TEYYUB_FILES],
    )


@app.context_processor
def inject_teyyub_nav():
    return {
        "teyyub_datasets": [
            {"key": k, "label": TEYYUB_LABELS[k]} for k in TEYYUB_FILES
        ],
        "raghavendra_datasets": [
            {"key": k, "label": RAGHAVENDRA_LABELS[k]} for k in RAGHAVENDRA_FILES
        ],
    }


@app.route("/api/teyyub/<key>/columns")
def teyyub_columns(key):
    try:
        df = get_teyyub_df(key)
        total = int(len(df))
        return jsonify({
            "columns": list(df.columns),
            "total": total,
            "is_large": total > LARGE_THRESHOLD,
            "preview_limit": PREVIEW_LIMIT,
        })
    except KeyError:
        return jsonify({"error": "unknown dataset"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/teyyub/<key>/stats")
def teyyub_stats_api(key):
    try:
        return jsonify({"stats": get_teyyub_stats(key)})
    except KeyError:
        return jsonify({"error": "unknown dataset"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/teyyub/<key>")
def teyyub_data(key):
    """Returns data. Supports Tabulator remote pagination via query params:
    page, size, sorters[i][field], sorters[i][dir],
    filters[i][field|type|value]. If `paginate=false` returns full dataset
    (intended for small files / CSV export).
    """
    try:
        df = get_teyyub_df(key)
    except KeyError:
        return jsonify({"error": "unknown dataset"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    paginate = request.args.get("paginate", "true").lower() != "false"

    filters = []
    i = 0
    while True:
        f = request.args.get(f"filters[{i}][field]")
        if f is None:
            break
        filters.append({
            "field": f,
            "type": request.args.get(f"filters[{i}][type]", "like"),
            "value": request.args.get(f"filters[{i}][value]", ""),
        })
        i += 1

    work = df
    if filters:
        mask = pd.Series(True, index=work.index)
        for fl in filters:
            field = fl["field"]
            if field not in work.columns:
                continue
            val = fl["value"]
            if val is None or val == "":
                continue
            ftype = fl["type"]
            col_str = work[field].astype(str)
            sval = str(val)
            if ftype in ("=", "==", "equals"):
                m = col_str.str.lower() == sval.lower()
            elif ftype == "!=":
                m = col_str.str.lower() != sval.lower()
            elif ftype in ("like", "ilike"):
                m = col_str.str.contains(sval, case=False, na=False, regex=False)
            elif ftype in (">", "<", ">=", "<="):
                try:
                    num_col = pd.to_numeric(work[field], errors="coerce")
                    num_val = float(sval)
                    if ftype == ">":
                        m = num_col > num_val
                    elif ftype == "<":
                        m = num_col < num_val
                    elif ftype == ">=":
                        m = num_col >= num_val
                    else:
                        m = num_col <= num_val
                    m = m.fillna(False)
                except Exception:
                    m = pd.Series(False, index=work.index)
            else:
                m = col_str.str.contains(sval, case=False, na=False, regex=False)
            mask &= m
        work = work[mask]

    sort_fields, sort_dirs = [], []
    i = 0
    while True:
        sf = request.args.get(f"sorters[{i}][field]")
        if sf is None:
            break
        if sf in work.columns:
            sort_fields.append(sf)
            sort_dirs.append(request.args.get(f"sorters[{i}][dir]", "asc") == "asc")
        i += 1
    if sort_fields:
        try:
            work = work.sort_values(
                by=sort_fields, ascending=sort_dirs,
                kind="mergesort", na_position="last",
            )
        except Exception:
            pass

    total = int(len(work))
    is_large = int(len(df)) > LARGE_THRESHOLD
    has_filters = bool(filters)
    has_sorters = bool(sort_fields)
    # For large datasets: when no filters/sorters, restrict to the first
    # PREVIEW_LIMIT rows so the UI stays responsive. Any filter/sort uses the
    # full dataset.
    preview_mode = is_large and not has_filters and not has_sorters
    if preview_mode:
        work = work.iloc[:PREVIEW_LIMIT]
        total = int(len(work))

    if not paginate:
        return jsonify(work.to_dict(orient="records"))

    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    try:
        size = max(1, min(1000, int(request.args.get("size", 50))))
    except ValueError:
        size = 50

    last_page = max(1, math.ceil(total / size))
    start = (page - 1) * size
    end = start + size
    chunk = work.iloc[start:end]

    return jsonify({
        "last_page": last_page,
        "last_row": total,
        "data": chunk.to_dict(orient="records"),
        "preview": preview_mode,
        "full_total": int(len(df)),
    })


@app.route("/api/ragu/columns")
def ragu_columns():
    try:
        df = get_ragu_df()
        total = int(len(df))
        return jsonify({
            "columns": list(df.columns),
            "total": total,
            "preview_limit": PREVIEW_LIMIT,
            "is_large": total > LARGE_THRESHOLD,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ragu/stats")
def ragu_stats():
    """Stat cards recomputed against the currently applied filters."""
    try:
        df = get_ragu_df()
        work = apply_filters(df, parse_filters(request.args))

        total = int(len(work))
        pred = work["layer1_prediction"].astype(str) if total else pd.Series(dtype=str)
        cyber = int((pred == "Cyber Security Related").sum()) if total else 0
        not_cyber = total - cyber
        pct = round(100.0 * cyber / total, 2) if total else 0.0

        cat = work["Category"].astype(str) if total else pd.Series(dtype=str)
        cat_counts = {}
        if total:
            cat_counts = cat[cat != ""].value_counts().to_dict()

        stats = [
            {"label": "Sentences", "value": total},
            {"label": "Cyber Security Related", "value": cyber},
            {"label": "Not Cyber Related", "value": not_cyber},
            {"label": "Cyber Share", "value": pct, "suffix": "%"},
            {"label": "Unique Companies (CIK)", "value": _nunique_nonblank(work["CIK"]) if total else 0},
            {"label": "Years Covered", "value": _nunique_nonblank(work["Year"]) if total else 0},
            {"label": "Labelled Sentences", "value": int(sum(cat_counts.values()))},
        ]
        for label in ("PCR", "RM", "CD", "IR", "REMOVE"):
            stats.append({"label": "Category " + label, "value": int(cat_counts.get(label, 0))})

        return jsonify({"stats": stats})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ragu")
def ragu_data():
    """Tabulator remote pagination endpoint for the NLP predictions dataset."""
    try:
        df = get_ragu_df()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    filters = parse_filters(request.args)
    work = apply_filters(df, filters)

    sort_fields, sort_dirs = parse_sorters(request.args, work)
    if sort_fields:
        try:
            work = work.sort_values(
                by=sort_fields, ascending=sort_dirs,
                kind="mergesort", na_position="last",
            )
        except Exception:
            pass

    total = int(len(work))
    preview_mode = (
        int(len(df)) > LARGE_THRESHOLD and not filters and not sort_fields
    )
    if preview_mode:
        work = work.iloc[:PREVIEW_LIMIT]
        total = int(len(work))

    if request.args.get("paginate", "true").lower() == "false":
        return jsonify(work.to_dict(orient="records"))

    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    try:
        size = max(1, min(1000, int(request.args.get("size", 50))))
    except ValueError:
        size = 50

    last_page = max(1, math.ceil(total / size))
    start = (page - 1) * size
    chunk = work.iloc[start:start + size]

    return jsonify({
        "last_page": last_page,
        "last_row": total,
        "data": chunk.to_dict(orient="records"),
        "preview": preview_mode,
        "full_total": int(len(df)),
    })


@app.route("/api/maryam/columns")
def maryam_columns():
    try:
        df = get_maryam_df()
        total = int(len(df))
        return jsonify({
            "columns": list(df.columns),
            "total": total,
            "preview_limit": PREVIEW_LIMIT,
            "is_large": total > LARGE_THRESHOLD,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/maryam/stats")
def maryam_stats():
    """Stat cards recomputed against the currently applied filters."""
    try:
        df = get_maryam_df()
        work = apply_filters(df, parse_filters(request.args))
        total = int(len(work))

        if total:
            per_account = work.groupby("account_id")["cik"].nunique()
            per_cik = work.groupby("cik")["account_id"].nunique()
            ambiguous_accounts = int((per_account > 1).sum())
            ambiguous_ciks = int((per_cik > 1).sum())
            confirmed = int((work["Result"].astype(str) == "MATCH").sum())
        else:
            ambiguous_accounts = 0
            ambiguous_ciks = 0
            confirmed = 0

        stats = [
            {"label": "Matched Pairs", "value": total},
            {"label": "Unique Companies (CIK)", "value": _nunique_nonblank(work["cik"]) if total else 0},
            {"label": "Unique 10-K Names", "value": _nunique_nonblank(work["name"]) if total else 0},
            {"label": "Spiceworks Accounts", "value": _nunique_nonblank(work["account_id"]) if total else 0},
            {"label": "Spiceworks Account Names", "value": _nunique_nonblank(work["account_name"]) if total else 0},
            {"label": "Accounts With 2+ CIKs", "value": ambiguous_accounts},
            {"label": "CIKs With 2+ Accounts", "value": ambiguous_ciks},
            {"label": "Result = MATCH", "value": confirmed},
        ]
        return jsonify({"stats": stats})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/maryam")
def maryam_data():
    """Tabulator remote pagination endpoint for the matching dataset."""
    try:
        df = get_maryam_df()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    filters = parse_filters(request.args)
    work = apply_filters(df, filters)

    sort_fields, sort_dirs = parse_sorters(request.args, work)
    if sort_fields:
        try:
            work = work.sort_values(
                by=sort_fields, ascending=sort_dirs,
                kind="mergesort", na_position="last",
            )
        except Exception:
            pass

    total = int(len(work))
    preview_mode = (
        int(len(df)) > LARGE_THRESHOLD and not filters and not sort_fields
    )
    if preview_mode:
        work = work.iloc[:PREVIEW_LIMIT]
        total = int(len(work))

    if request.args.get("paginate", "true").lower() == "false":
        return jsonify(work.to_dict(orient="records"))

    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    try:
        size = max(1, min(1000, int(request.args.get("size", 50))))
    except ValueError:
        size = 50

    last_page = max(1, math.ceil(total / size))
    start = (page - 1) * size
    chunk = work.iloc[start:start + size]

    return jsonify({
        "last_page": last_page,
        "last_row": total,
        "data": chunk.to_dict(orient="records"),
        "preview": preview_mode,
        "full_total": int(len(df)),
    })


def _maryam6_filtered():
    """Applies the incident toggle + Tabulator header filters."""
    df = get_maryam6_df()
    work = df
    if request.args.get("incidents_only") == "1":
        work = work[work["incidents"] >= 1]
    return df, apply_filters(work, parse_filters(request.args))


@app.route("/api/maryam6/columns")
def maryam6_columns():
    try:
        df = get_maryam6_df()
        return jsonify({
            "columns": list(df.columns),
            "db_cols": MARYAM6_DB_COLS,
            "db_labels": MARYAM6_DB_LABELS,
            "total": int(len(df)),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/maryam6/stats")
def maryam6_stats():
    """Stat cards recomputed against the currently applied filters."""
    try:
        _, work = _maryam6_filtered()
        total = int(len(work))
        stats = [
            {"label": "Matched Companies (10-K \u2194 SWDB)", "value": total},
            {"label": "With \u22651 Incident (any DB)", "value": int((work["incidents"] >= 1).sum()) if total else 0},
        ]
        for col in MARYAM6_DB_COLS:
            stats.append({
                "label": MARYAM6_DB_LABELS[col],
                "value": int((work[col].astype(str) != "").sum()) if total else 0,
            })
        return jsonify({"stats": stats})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/maryam6")
def maryam6_data():
    """Tabulator remote pagination endpoint for the 6-DB merge."""
    try:
        _, work = _maryam6_filtered()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    sort_fields, sort_dirs = parse_sorters(request.args, work)
    if sort_fields:
        try:
            work = work.sort_values(
                by=sort_fields, ascending=sort_dirs,
                kind="mergesort", na_position="last",
            )
        except Exception:
            pass

    total = int(len(work))

    if request.args.get("paginate", "true").lower() == "false":
        return jsonify(work.to_dict(orient="records"))

    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    try:
        size = max(1, min(1000, int(request.args.get("size", 50))))
    except ValueError:
        size = 50

    last_page = max(1, math.ceil(total / size))
    start = (page - 1) * size
    chunk = work.iloc[start:start + size]

    return jsonify({
        "last_page": last_page,
        "last_row": total,
        "data": chunk.to_dict(orient="records"),
    })


@app.route("/api/nolan/columns")
def nolan_columns():
    try:
        df = get_nolan_df()
        return jsonify({
            "columns": NOLAN_COLUMNS,
            "total": int(len(df)),
            "preview_limit": PREVIEW_LIMIT,
            "is_large": int(len(df)) > LARGE_THRESHOLD,
            "sentence_page": NOLAN_SENTENCE_PAGE,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/nolan/stats")
def nolan_stats():
    """Stat cards recomputed against the currently applied filters."""
    try:
        df = get_nolan_df()
        work = apply_filters(df, parse_filters(request.args))
        return jsonify({"stats": get_nolan_stats(work)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/nolan")
def nolan_data():
    """Tabulator remote pagination endpoint for the filing-level index."""
    try:
        df = get_nolan_df()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    filters = parse_filters(request.args)
    work = apply_filters(df, filters)

    sort_fields, sort_dirs = parse_sorters(request.args, work)
    if sort_fields:
        try:
            work = work.sort_values(
                by=sort_fields, ascending=sort_dirs,
                kind="mergesort", na_position="last",
            )
        except Exception:
            pass

    work = work[NOLAN_COLUMNS]
    total = int(len(work))

    if request.args.get("paginate", "true").lower() == "false":
        return jsonify(work.to_dict(orient="records"))

    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    try:
        size = max(1, min(1000, int(request.args.get("size", 50))))
    except ValueError:
        size = 50

    last_page = max(1, math.ceil(total / size))
    start = (page - 1) * size
    chunk = work.iloc[start:start + size]

    return jsonify({
        "last_page": last_page,
        "last_row": total,
        "data": chunk.to_dict(orient="records"),
        "preview": False,
        "full_total": int(len(df)),
    })


@app.route("/api/nolan/filing/<int:row>")
def nolan_filing(row):
    """Paginated sentences of a single filing, with optional text search
    and item-section filter (e.g. items=1A,7,8,9A)."""
    try:
        sentences, item_labels = read_nolan_filing(row)
    except KeyError:
        return jsonify({"error": "unknown filing"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    wanted = {
        part.strip().upper()
        for part in (request.args.get("items") or "").split(",")
        if part.strip()
    }

    query = (request.args.get("q") or "").strip()
    needle = query.lower()
    triples = [
        (i, s, item_labels[i])
        for i, s in enumerate(sentences)
        if (not wanted or item_labels[i] in wanted)
        and (not needle or needle in s.lower())
    ]

    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    try:
        size = max(1, min(1000, int(request.args.get("size", NOLAN_SENTENCE_PAGE))))
    except ValueError:
        size = NOLAN_SENTENCE_PAGE

    total = len(triples)
    last_page = max(1, math.ceil(total / size))
    start = (page - 1) * size
    chunk = triples[start:start + size]

    meta = get_nolan_df()
    entry = meta[meta["row"] == row].iloc[0]

    return jsonify({
        "last_page": last_page,
        "last_row": total,
        "matched": total,
        "total_sentences": len(sentences),
        "filing": {
            "row": int(entry["row"]),
            "cik": str(entry["cik"]),
            "adsh": str(entry["adsh"]),
            "filing_date": str(entry["filing_date"]),
        },
        "data": [
            {"n": i + 1, "sentence": s, "item": it} for i, s, it in chunk
        ],
    })


@app.route("/api/nolan/reader/<int:row>")
def nolan_reader_data(row):
    """Whole filing grouped into item sections for the friendly reader view."""
    try:
        sentences, item_labels = read_nolan_filing(row)
    except KeyError:
        return jsonify({"error": "unknown filing"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    sections = []
    for i, (sent, label) in enumerate(zip(sentences, item_labels)):
        if not sections or sections[-1]["item"] != label:
            info = NOLAN_ITEM_META.get(label, {})
            sections.append({
                "item": label,
                "title": info.get("title", "Item " + label),
                "plain": info.get("plain", ""),
                "cyber": bool(info.get("cyber")),
                "start": i + 1,
                "sentences": [],
            })
        sections[-1]["sentences"].append(sent)

    meta = get_nolan_df()
    entry = meta[meta["row"] == row].iloc[0]
    cik = str(entry["cik"])
    adsh = str(entry["adsh"])
    try:
        cik_num = str(int(float(cik)))
    except ValueError:
        cik_num = cik
    company = get_nolan_company_names().get(cik_num, "")
    edgar_url = (
        "https://www.sec.gov/Archives/edgar/data/"
        + cik_num + "/" + adsh.replace("-", "") + "/" + adsh + "-index.htm"
    )

    return jsonify({
        "filing": {
            "row": int(entry["row"]),
            "cik": cik,
            "adsh": adsh,
            "filing_date": str(entry["filing_date"]),
            "company": company,
            "edgar_url": edgar_url,
        },
        "total_sentences": len(sentences),
        "sections": sections,
    })


@app.route("/api/raghavendra/<key>/columns")
def raghavendra_columns(key):
    if key not in RAGHAVENDRA_FILES:
        return jsonify({"error": "unknown dataset"}), 404
    try:
        ensure_raghavendra_db()
        return jsonify({
            "columns": RAGHAVENDRA_COLUMNS,
            "numeric": sorted(RAGHAVENDRA_NUMERIC),
            "categorical": get_raghavendra_categories(key),
            "total": get_raghavendra_total(key),
            "max_export": RAGHAVENDRA_MAX_EXPORT,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/raghavendra/<key>/stats")
def raghavendra_stats(key):
    """Stat cards recomputed against the currently applied filters."""
    if key not in RAGHAVENDRA_FILES:
        return jsonify({"error": "unknown dataset"}), 404
    try:
        ensure_raghavendra_db()
        return jsonify({"stats": get_raghavendra_stats(key, parse_filters(request.args))})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/raghavendra/<key>")
def raghavendra_data(key):
    """Tabulator remote pagination endpoint backed by SQLite."""
    if key not in RAGHAVENDRA_FILES:
        return jsonify({"error": "unknown dataset"}), 404
    try:
        ensure_raghavendra_db()
        filters = parse_filters(request.args)
        where, params = raghavendra_where(filters)
        order_by = raghavendra_order_by(request.args)

        conn = _open_raghavendra_db()
        try:
            if where:
                total = int(conn.execute(
                    f'SELECT COUNT(*) FROM "{key}"{where}', params
                ).fetchone()[0])
            else:
                total = get_raghavendra_total(key)

            select = f'SELECT {", ".join(RAGHAVENDRA_COLUMNS)} FROM "{key}"'

            if request.args.get("paginate", "true").lower() == "false":
                rows = conn.execute(
                    f"{select}{where}{order_by} LIMIT ?",
                    list(params) + [RAGHAVENDRA_MAX_EXPORT],
                ).fetchall()
                return jsonify([dict(zip(RAGHAVENDRA_COLUMNS, r)) for r in rows])

            try:
                page = max(1, int(request.args.get("page", 1)))
            except ValueError:
                page = 1
            try:
                size = max(1, min(1000, int(request.args.get("size", 50))))
            except ValueError:
                size = 50

            rows = conn.execute(
                f"{select}{where}{order_by} LIMIT ? OFFSET ?",
                list(params) + [size, (page - 1) * size],
            ).fetchall()
        finally:
            conn.close()

        return jsonify({
            "last_page": max(1, math.ceil(total / size)),
            "last_row": total,
            "data": [dict(zip(RAGHAVENDRA_COLUMNS, r)) for r in rows],
            "full_total": get_raghavendra_total(key),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    # Pre-warm caches once (avoid the debug reloader doing it twice).
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
        warmup_caches()
    else:
        warmup_caches()
    # debug=False keeps the in-memory cache alive across edits.
    app.run(debug=False, port=9898, threaded=True)
