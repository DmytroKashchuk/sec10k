"""
FASE 1 — Costruisci l'indice di tutti i 10-K filing dal 2024 in poi.

L'obbligo Item 1C cybersecurity scatta per fiscal year che terminano
dopo il 15 dicembre 2023, quindi i primi 10-K con Item 1C arrivano
da inizio 2024 in poi.

Scarica i quarterly index file da EDGAR (gratis, no API key)
e produce un CSV con: CIK, company_name, form_type, date_filed, filename.

Output: 10k_index.csv
"""

import csv
import time
from datetime import datetime

import requests

HEADERS = {
    "User-Agent": "Dmytro Kashchuk dima@utulsa.edu",
    "Accept-Encoding": "gzip, deflate",
    "Host": "www.sec.gov",
}

START_YEAR = 2024
END_YEAR = datetime.now().year
OUTPUT_FILE = "10k_index.csv"


def download_quarter_index(year, quarter):
    """Scarica il form.idx di un trimestre da EDGAR."""
    url = f"https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{quarter}/form.idx"
    print(f"  Scarico {year} Q{quarter}...")

    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()

    return response.text


def parse_form_idx(text):
    """
    Estrae le righe relative ai 10-K dal file form.idx.

    Il formato form.idx e' a colonne fisse:
    Form Type        Company Name        CIK        Date Filed     Filename
    """
    rows = []
    lines = text.split("\n")
    data_started = False

    for line in lines:
        if line.startswith("---"):
            data_started = True
            continue

        if not data_started or len(line) < 98:
            continue

        form_type = line[0:12].strip()
        company_name = line[12:74].strip()
        cik = line[74:86].strip()
        date_filed = line[86:98].strip()
        filename = line[98:].strip()

        # Solo 10-K standard (no amendments)
        if form_type == "10-K":
            rows.append({
                "form_type": form_type,
                "company_name": company_name,
                "cik": cik,
                "date_filed": date_filed,
                "filename": filename,
            })

    return rows


def main():
    all_filings = []

    for year in range(START_YEAR, END_YEAR + 1):
        print(f"\nAnno {year}:")
        for quarter in range(1, 5):
            try:
                text = download_quarter_index(year, quarter)
                rows = parse_form_idx(text)
                all_filings.extend(rows)
                print(f"    Trovati {len(rows)} 10-K")
            except Exception as e:
                print(f"    Errore: {e}")

            time.sleep(0.5)

    print(f"\nTotale 10-K trovati: {len(all_filings)}")

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["form_type", "company_name", "cik", "date_filed", "filename"]
        )
        writer.writeheader()
        writer.writerows(all_filings)

    print(f"Salvato in {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
