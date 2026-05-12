"""
FASE 3 — Estrae Item 1C (Cybersecurity) da ogni 10-K scaricato.

Strategia:
1. Pulisce l'HTML in testo leggibile con BeautifulSoup
2. Trova "Item 1C" con regex flessibile (gestisce varianti: ITEM 1C,
   Item 1C., Item 1C:, ecc.)
3. Estrae il testo fino al successivo "Item 2"
4. Salva in CSV: CIK, company, date_filed, filing_path, item_1c_text, word_count

Output: item_1c_extracted.csv

Setup: pip install beautifulsoup4 lxml
"""

import csv
import re
from pathlib import Path

from bs4 import BeautifulSoup

FILINGS_DIR = Path("10k_filings")
INDEX_FILE = "10k_index.csv"
OUTPUT_FILE = "item_1c_extracted.csv"


def html_to_text(html):
    """Converte HTML in testo pulito."""
    soup = BeautifulSoup(html, "lxml")

    # Rimuove script e style
    for tag in soup(["script", "style"]):
        tag.decompose()

    text = soup.get_text(separator="\n")

    # Normalizza whitespace: piu' newline o spazi -> uno solo
    text = re.sub(r"\n\s*\n", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)

    return text


def find_item_1c(text):
    """
    Trova Item 1C nel testo e ritorna il contenuto fino al successivo Item.

    Gestisce varianti reali viste nei filing:
      - Item 1C. Cybersecurity
      - ITEM 1C: CYBERSECURITY
      - Item 1C Cybersecurity
      - Item 1C.Cybersecurity (a volte senza spazi)
    """
    # Pattern per inizio di Item 1C
    # Cerca "Item 1C" seguito opzionalmente da ., :, spazi, e poi "Cybersecurity"
    start_pattern = re.compile(
        r"item\s*1c[\.\:\s]*\s*cybersecurity",
        re.IGNORECASE,
    )

    # Pattern per fine: Item 2, Item 3, ecc. (il successivo)
    # Item 1D non esiste, quindi il successivo e' Item 2 (Properties)
    end_pattern = re.compile(
        r"item\s*2[\.\:\s]",
        re.IGNORECASE,
    )

    # Trova tutti i match di Item 1C (di solito ce ne sono 2: TOC + sezione vera)
    start_matches = list(start_pattern.finditer(text))

    if not start_matches:
        return None

    # L'ultimo match e' di solito la sezione vera (non il TOC)
    # Il TOC sta in cima al documento, la sezione vera dopo Item 1B
    start_pos = start_matches[-1].start()

    # Cerca il prossimo Item 2 dopo start_pos
    end_match = end_pattern.search(text, pos=start_pos + 50)

    if not end_match:
        # Se non trova Item 2, prende fino a 50.000 caratteri (fallback)
        end_pos = min(start_pos + 50000, len(text))
    else:
        end_pos = end_match.start()

    section = text[start_pos:end_pos].strip()

    return section


def load_index():
    """Carica l'indice della Fase 1 per mappare CIK->company."""
    index = {}
    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            index[row["cik"]] = row
    return index


def main():
    index = load_index()
    print(f"Indice caricato: {len(index)} filing")

    results = []
    processed = 0
    not_found = 0
    errors = 0

    # Itera su tutti i file scaricati
    filing_files = list(FILINGS_DIR.glob("*/*.htm"))
    print(f"File da processare: {len(filing_files)}")

    for filepath in filing_files:
        # Estrae CIK dal nome file (formato: CIK_accession.htm)
        filename_parts = filepath.stem.split("_")
        cik = filename_parts[0]

        # Recupera metadati dall'indice
        meta = index.get(cik, {})

        try:
            html = filepath.read_text(encoding="utf-8")
            text = html_to_text(html)
            item_1c = find_item_1c(text)

            if item_1c:
                results.append({
                    "cik": cik,
                    "company_name": meta.get("company_name", ""),
                    "date_filed": meta.get("date_filed", ""),
                    "filing_path": str(filepath),
                    "item_1c_text": item_1c,
                    "word_count": len(item_1c.split()),
                })
                processed += 1
            else:
                not_found += 1

        except Exception as e:
            print(f"  Errore su {filepath.name}: {e}")
            errors += 1

        if (processed + not_found + errors) % 100 == 0:
            print(
                f"  Processati: {processed}, "
                f"Item 1C non trovato: {not_found}, "
                f"Errori: {errors}"
            )

    # Salva risultati
    print(f"\nSalvo {len(results)} estrazioni in {OUTPUT_FILE}")
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "cik", "company_name", "date_filed", "filing_path",
                "item_1c_text", "word_count"
            ]
        )
        writer.writeheader()
        writer.writerows(results)

    print(f"\nRiepilogo finale:")
    print(f"  Item 1C estratti: {processed}")
    print(f"  Filing senza Item 1C: {not_found}")
    print(f"  Errori: {errors}")


if __name__ == "__main__":
    main()
