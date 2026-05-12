"""
FASE 2 — Scarica solo il documento principale di ogni 10-K (HTML).

Per ogni filing nell'indice:
1. Risolve l'URL del documento principale tramite l'index.json del filing
2. Scarica solo l'HTML del 10-K (no exhibits)

Questo riduce la dimensione totale del download ~10x rispetto a
scaricare l'intero submission .txt.

Output: directory 10k_filings/YYYY/CIK_accession.htm
"""

import csv
import json
import time
from pathlib import Path

import requests

HEADERS = {
    "User-Agent": "Dmytro Kashchuk dima@utulsa.edu",
    "Accept-Encoding": "gzip, deflate",
}

INDEX_FILE = "10k_index.csv"
OUTPUT_DIR = Path("10k_filings")


def load_index():
    """Carica l'indice prodotto dalla Fase 1."""
    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def get_accession_no_dashes(filing):
    """
    Estrae l'accession number senza trattini dal filename.
    Es: edgar/data/1234/0001193125-24-118890-index.htm -> 000119312524118890
    """
    # filename e' tipo: edgar/data/CIK/ACCESSION-index.htm
    stem = Path(filing["filename"]).stem  # rimuove .htm
    # Rimuove "-index" se presente
    accession_with_dashes = stem.replace("-index", "")
    # Rimuove i trattini
    return accession_with_dashes.replace("-", "")


def get_main_document_url(cik, accession_no_dashes):
    """
    Recupera l'URL del documento principale 10-K usando l'index.json
    del filing su EDGAR.
    """
    cik_int = int(cik)  # rimuove zeri iniziali
    index_url = (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{cik_int}/{accession_no_dashes}/index.json"
    )

    # Per data.sec.gov serve Host diverso, qui usiamo www.sec.gov
    headers = {**HEADERS, "Host": "www.sec.gov"}
    response = requests.get(index_url, headers=headers)
    response.raise_for_status()

    data = response.json()
    items = data.get("directory", {}).get("item", [])

    # Cerca il documento principale 10-K
    # Strategia: cerca file .htm che NON contenga "ex" (exhibit) e
    # che abbia type "10-K"
    for item in items:
        name = item.get("name", "").lower()
        # Salta exhibits, immagini, allegati
        if name.endswith(".htm") and "ex" not in name and "/" not in name:
            # Costruisce URL completo del documento
            doc_url = (
                f"https://www.sec.gov/Archives/edgar/data/"
                f"{cik_int}/{accession_no_dashes}/{item['name']}"
            )
            return doc_url

    return None


def download_document(url):
    """Scarica un documento da SEC."""
    headers = {**HEADERS, "Host": "www.sec.gov"}
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.text


def build_output_path(filing, accession_no_dashes):
    """Crea il percorso di output: 10k_filings/YYYY/CIK_accession.htm"""
    year = filing["date_filed"][:4]
    cik = filing["cik"]

    out_dir = OUTPUT_DIR / year
    out_dir.mkdir(parents=True, exist_ok=True)

    return out_dir / f"{cik}_{accession_no_dashes}.htm"


def main():
    filings = load_index()
    print(f"Trovati {len(filings)} filing nell'indice")

    OUTPUT_DIR.mkdir(exist_ok=True)

    downloaded = 0
    skipped = 0
    errors = 0

    for i, filing in enumerate(filings, 1):
        try:
            accession_no_dashes = get_accession_no_dashes(filing)
        except Exception as e:
            print(f"  Errore parsing accession per CIK {filing['cik']}: {e}")
            errors += 1
            continue

        out_path = build_output_path(filing, accession_no_dashes)

        # Skip se gia' presente (resume)
        if out_path.exists():
            skipped += 1
            continue

        try:
            # Step 1: trova URL del documento principale
            doc_url = get_main_document_url(filing["cik"], accession_no_dashes)

            if not doc_url:
                print(f"  Nessun documento principale trovato per CIK {filing['cik']}")
                errors += 1
                time.sleep(0.15)
                continue

            time.sleep(0.15)  # rate limit tra le due chiamate

            # Step 2: scarica il documento
            content = download_document(doc_url)
            out_path.write_text(content, encoding="utf-8")
            downloaded += 1

            if i % 50 == 0:
                print(
                    f"  [{i}/{len(filings)}] "
                    f"scaricati: {downloaded}, saltati: {skipped}, errori: {errors}"
                )

        except Exception as e:
            errors += 1
            print(f"  Errore CIK {filing['cik']} ({filing['date_filed']}): {e}")

        # SEC: max 10 req/s. Ogni iterazione fa 2 req, quindi 0.25s = ~4 req/s
        time.sleep(0.25)

    print(f"\nFatto!")
    print(f"  Scaricati: {downloaded}")
    print(f"  Saltati (gia' presenti): {skipped}")
    print(f"  Errori: {errors}")


if __name__ == "__main__":
    main()
