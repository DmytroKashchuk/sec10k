import math
import os
import time
from flask import Flask, jsonify, render_template, request

import pandas as pd

app = Flask(__name__)

BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, "data")
TEYYUB_DIR = os.path.join(DATA_DIR, "teyyub_data")

DATA_PATH = os.path.join(DATA_DIR, "crsp_public_companies.csv")

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

# Cache loaded DataFrames to avoid re-reading CSVs on every request.
_df_cache = {}
_stats_cache = {}
_crsp_cache = {"df": None, "records": None}


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
    print(f"[warmup] all datasets ready in {time.time() - t0:.1f}s", flush=True)


@app.route("/")
def index():
    return render_template("index.html")


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
        ]
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


if __name__ == "__main__":
    # Pre-warm caches once (avoid the debug reloader doing it twice).
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
        warmup_caches()
    else:
        warmup_caches()
    # debug=False keeps the in-memory cache alive across edits.
    app.run(debug=False, port=5000, threaded=True)
