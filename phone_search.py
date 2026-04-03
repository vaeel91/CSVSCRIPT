#!/usr/bin/env python3
"""
Phone Number Search Tool
Analizza una rubrica telefonica (CSV o vCard/.vcf) e cerca ogni numero online
per verificare se appare su siti web, directory o database pubblici.

Uso:
    python phone_search.py rubrica.csv
    python phone_search.py contatti.vcf
    python phone_search.py rubrica.csv --output risultati.json
    python phone_search.py rubrica.csv --delay 3
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock


@dataclass
class Contact:
    name: str
    phone: str


@dataclass
class SearchResult:
    contact: Contact
    query: str
    results: list = field(default_factory=list)
    error: str = ""


# ---------------------------------------------------------------------------
# Parsing rubrica
# ---------------------------------------------------------------------------

def normalize_phone(raw: str) -> str:
    """Rimuove spazi, trattini e caratteri non numerici (mantiene il +)."""
    cleaned = re.sub(r"[^\d+]", "", raw)
    return cleaned


def parse_csv(filepath: str) -> list[Contact]:
    """
    Legge un CSV con header. Cerca colonne che contengono 'phone', 'telefono',
    'numero', 'cell', 'mobile', 'tel' nel nome. Il nome viene preso dalla
    prima colonna che contiene 'name', 'nome', 'contact', 'contatto'.
    """
    contacts = []
    with open(filepath, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return contacts

        # Trova colonne rilevanti
        name_col = None
        phone_cols = []
        phone_keywords = {"phone", "telefono", "numero", "cell", "mobile", "tel"}
        name_keywords = {"name", "nome", "contact", "contatto"}

        for col in reader.fieldnames:
            col_lower = col.lower().strip()
            if any(k in col_lower for k in name_keywords) and name_col is None:
                name_col = col
            if any(k in col_lower for k in phone_keywords):
                phone_cols.append(col)

        # Fallback: se non trova colonne specifiche, usa prima e seconda colonna
        if not name_col and reader.fieldnames:
            name_col = reader.fieldnames[0]
        if not phone_cols and len(reader.fieldnames) >= 2:
            phone_cols = [reader.fieldnames[1]]

        for row in reader:
            name = row.get(name_col, "").strip() if name_col else "Sconosciuto"
            for pc in phone_cols:
                raw_phone = row.get(pc, "").strip()
                if raw_phone:
                    phone = normalize_phone(raw_phone)
                    if len(phone) >= 6:  # numero minimo ragionevole
                        contacts.append(Contact(name=name, phone=phone))

    return contacts


def parse_vcf(filepath: str) -> list[Contact]:
    """Legge un file vCard (.vcf) ed estrae nome e numeri di telefono."""
    contacts = []
    current_name = ""
    with open(filepath, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("FN:") or line.startswith("FN;"):
                current_name = line.split(":", 1)[1].strip()
            elif line.upper().startswith("TEL"):
                # TEL;TYPE=CELL:+391234567890 oppure TEL:+391234567890
                raw_phone = line.split(":", 1)[1].strip() if ":" in line else ""
                phone = normalize_phone(raw_phone)
                if len(phone) >= 6:
                    contacts.append(Contact(
                        name=current_name or "Sconosciuto",
                        phone=phone,
                    ))
            elif line == "END:VCARD":
                current_name = ""

    return contacts


def load_contacts(filepath: str) -> list[Contact]:
    """Carica i contatti dal file specificato (CSV o VCF)."""
    ext = Path(filepath).suffix.lower()
    if ext == ".vcf":
        return parse_vcf(filepath)
    elif ext in (".csv", ".tsv", ".txt"):
        return parse_csv(filepath)
    else:
        print(f"Formato non riconosciuto: {ext}. Provo come CSV...")
        return parse_csv(filepath)


# ---------------------------------------------------------------------------
# Ricerca online
# ---------------------------------------------------------------------------

def search_google_html(query: str, timeout: int = 15) -> list[dict]:
    """
    Cerca su Google e restituisce i risultati trovati.
    Non richiede API key.
    """
    encoded_query = urllib.parse.quote_plus(query)
    url = f"https://www.google.com/search?q={encoded_query}&num=10&hl=it"

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return [{"error": str(e)}]

    results = []

    # Pattern per i link dei risultati Google
    # Google usa <a href="/url?q=URL_REALE&...">
    link_pattern = re.compile(
        r'<a[^>]+href="/url\?q=([^"&]+)[^"]*"[^>]*>(.*?)</a>',
        re.DOTALL,
    )
    # Pattern alternativo per risultati diretti
    link_pattern2 = re.compile(
        r'<a[^>]+href="(https?://(?!google\.com|accounts\.google)[^"]+)"[^>]*>(.*?)</a>',
        re.DOTALL,
    )

    # Cerca snippet nei tag <span> vicini ai risultati
    snippet_pattern = re.compile(
        r'<span[^>]*class="[^"]*"[^>]*>((?:(?!<span).){50,300})</span>',
        re.DOTALL,
    )

    seen_urls = set()

    for pattern in [link_pattern, link_pattern2]:
        for href, title in pattern.findall(html):
            href = urllib.parse.unquote(href)
            # Filtra link interni di Google
            if any(x in href for x in [
                "google.com", "accounts.google", "support.google",
                "maps.google", "policies.google", "webcache",
            ]):
                continue
            if href in seen_urls:
                continue
            seen_urls.add(href)

            title_clean = re.sub(r"<[^>]+>", "", title).strip()
            if not title_clean or len(title_clean) < 3:
                continue

            results.append({
                "title": title_clean,
                "url": href,
                "snippet": "",
            })

    # Aggiungi snippet ai risultati
    snippets = snippet_pattern.findall(html)
    snippet_texts = [
        re.sub(r"<[^>]+>", "", s).strip()
        for s in snippets
        if len(re.sub(r"<[^>]+>", "", s).strip()) > 40
    ]
    for i, r in enumerate(results):
        if i < len(snippet_texts):
            r["snippet"] = snippet_texts[i]

    return results


def strip_prefix(phone: str) -> str:
    """Rimuove il prefisso internazionale (+39) dal numero."""
    if phone.startswith("+39"):
        return phone[3:]
    if phone.startswith("0039"):
        return phone[4:]
    return phone


def search_phone_number(phone: str, delay: float = 2.0) -> list[dict]:
    """Cerca un numero di telefono su Google: con prefisso e senza."""
    all_results = []

    # Query 1: numero completo con prefisso
    query1 = f'"{phone}"'
    print(f"\n      Ricerca Google: {query1}", end="", flush=True)
    results1 = search_google_html(query1)
    for r in results1:
        r["query"] = query1
    all_results.extend(results1)

    time.sleep(delay)

    # Query 2: numero senza prefisso internazionale
    phone_no_prefix = strip_prefix(phone)
    if phone_no_prefix != phone:
        query2 = f'"{phone_no_prefix}"'
        print(f"\n      Ricerca Google: {query2}", end="", flush=True)
        results2 = search_google_html(query2)
        for r in results2:
            r["query"] = query2
        all_results.extend(results2)

        time.sleep(delay)

    # Query 3: numero con spazi (formato comune)
    if len(phone_no_prefix) >= 9:
        spaced = phone_no_prefix[:3] + " " + phone_no_prefix[3:6] + " " + phone_no_prefix[6:]
        query3 = f'"{spaced}"'
        print(f"\n      Ricerca Google: {query3}", end="", flush=True)
        results3 = search_google_html(query3)
        for r in results3:
            r["query"] = query3
        all_results.extend(results3)

    return all_results


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_report(search_results: list[SearchResult]):
    """Stampa un report leggibile a terminale."""
    found_count = 0
    not_found_count = 0
    error_count = 0

    print("\n" + "=" * 70)
    print("  REPORT RICERCA NUMERI TELEFONICI ONLINE")
    print("=" * 70)

    for sr in search_results:
        valid_results = [r for r in sr.results if "error" not in r]
        has_results = len(valid_results) > 0

        if sr.error:
            status = "ERRORE"
            error_count += 1
        elif has_results:
            status = f"TROVATO ({len(valid_results)} risultati)"
            found_count += 1
        else:
            status = "NON TROVATO"
            not_found_count += 1

        print(f"\n{'─' * 70}")
        print(f"  {sr.contact.name} | {sr.contact.phone}")
        print(f"  Stato: {status}")

        if sr.error:
            print(f"  Errore: {sr.error}")

        if has_results:
            for i, r in enumerate(valid_results[:5], 1):  # max 5 risultati
                print(f"\n    [{i}] {r.get('title', 'N/A')}")
                print(f"        URL: {r.get('url', 'N/A')}")
                if r.get("snippet"):
                    snippet = r["snippet"][:150]
                    print(f"        {snippet}")

    print(f"\n{'=' * 70}")
    print(f"  RIEPILOGO")
    print(f"  Totale numeri analizzati: {len(search_results)}")
    print(f"  Trovati online:          {found_count}")
    print(f"  Non trovati:             {not_found_count}")
    print(f"  Errori:                  {error_count}")
    print(f"{'=' * 70}\n")


def save_json_report(search_results: list[SearchResult], output_path: str):
    """Salva il report in formato JSON."""
    data = []
    for sr in search_results:
        data.append({
            "name": sr.contact.name,
            "phone": sr.contact.phone,
            "query": sr.query,
            "error": sr.error,
            "results_count": len([r for r in sr.results if "error" not in r]),
            "results": sr.results,
        })

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"\nReport JSON salvato in: {output_path}")


def save_found_numbers(search_results: list[SearchResult], output_path: str):
    """Salva un file TXT con i numeri che hanno avuto risultati positivi."""
    found = []
    for sr in search_results:
        valid_results = [r for r in sr.results if "error" not in r]
        if valid_results:
            found.append(sr)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("NUMERI TROVATI ONLINE\n")
        f.write(f"Data: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Totale: {len(found)} numeri con risultati positivi\n")
        f.write("=" * 60 + "\n\n")

        for sr in found:
            valid_results = [r for r in sr.results if "error" not in r]
            f.write(f"Nome: {sr.contact.name}\n")
            f.write(f"Numero: {sr.contact.phone}\n")
            f.write(f"Risultati: {len(valid_results)}\n\n")
            for i, r in enumerate(valid_results[:10], 1):
                f.write(f"  [{i}] {r.get('title', 'N/A')}\n")
                f.write(f"      URL: {r.get('url', 'N/A')}\n")
                if r.get("snippet"):
                    f.write(f"      Dettagli: {r['snippet'][:200]}\n")
                if r.get("query"):
                    f.write(f"      Query: {r['query']}\n")
                f.write("\n")
            f.write("-" * 60 + "\n\n")

    print(f"File numeri trovati salvato in: {output_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Cerca numeri telefonici da una rubrica online",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Formati supportati:
  - CSV: con colonne nome/telefono (rileva automaticamente le colonne)
  - VCF: file vCard standard (esportabile da quasi tutti i telefoni)

Esempi:
  python phone_search.py contatti.csv
  python phone_search.py rubrica.vcf --output report.json
  python phone_search.py numeri.csv --delay 5
        """,
    )
    parser.add_argument("file", help="File rubrica (CSV o VCF)")
    parser.add_argument(
        "--output", "-o",
        help="Salva risultati in un file JSON",
    )
    parser.add_argument(
        "--delay", "-d",
        type=float,
        default=2.0,
        help="Secondi di attesa tra le ricerche (default: 2)",
    )
    parser.add_argument(
        "--limit", "-l",
        type=int,
        default=0,
        help="Limita il numero di contatti da cercare (0 = tutti)",
    )
    parser.add_argument(
        "--found", "-f",
        default="numeri_trovati.txt",
        help="File TXT con i numeri trovati online (default: numeri_trovati.txt)",
    )
    parser.add_argument(
        "--workers", "-w",
        type=int,
        default=5,
        help="Numero di ricerche in parallelo (default: 5)",
    )

    args = parser.parse_args()

    if not os.path.isfile(args.file):
        print(f"Errore: file non trovato: {args.file}", file=sys.stderr)
        sys.exit(1)

    # Carica contatti
    print(f"Caricamento rubrica da: {args.file}")
    contacts = load_contacts(args.file)

    if not contacts:
        print("Nessun contatto trovato nel file.", file=sys.stderr)
        sys.exit(1)

    # Deduplica per numero
    seen = set()
    unique_contacts = []
    for c in contacts:
        if c.phone not in seen:
            seen.add(c.phone)
            unique_contacts.append(c)
    contacts = unique_contacts

    print(f"Trovati {len(contacts)} numeri unici.")

    if args.limit > 0:
        contacts = contacts[: args.limit]
        print(f"Limitato a {len(contacts)} contatti.")

    # Ricerca in parallelo
    search_results: list[SearchResult] = [None] * len(contacts)  # type: ignore
    total = len(contacts)
    print_lock = Lock()
    completed = [0]  # contatore condiviso

    def search_contact(index: int, contact: Contact) -> None:
        try:
            results = search_phone_number(contact.phone, delay=args.delay)
            valid = [r for r in results if "error" not in r]
            errors = [r for r in results if "error" in r]

            sr = SearchResult(
                contact=contact,
                query=f'"{contact.phone}"',
                results=results,
                error=errors[0]["error"] if errors and not valid else "",
            )
            search_results[index] = sr

            with print_lock:
                completed[0] += 1
                if valid:
                    print(f"\n  [{completed[0]}/{total}] {contact.name} ({contact.phone}): {len(valid)} risultati trovati", flush=True)
                    for j, r in enumerate(valid[:3], 1):
                        print(f"      [{j}] {r.get('url', 'N/A')}", flush=True)
                    if len(valid) > 3:
                        print(f"      ... e altri {len(valid) - 3} risultati", flush=True)
                else:
                    print(f"\n  [{completed[0]}/{total}] {contact.name} ({contact.phone}): nessun risultato", flush=True)

        except Exception as e:
            sr = SearchResult(
                contact=contact,
                query=f'"{contact.phone}"',
                error=str(e),
            )
            search_results[index] = sr
            with print_lock:
                completed[0] += 1
                print(f"\n  [{completed[0]}/{total}] {contact.name} ({contact.phone}): ERRORE - {e}", flush=True)

    workers = min(args.workers, total)
    print(f"\nRicerca in corso con {workers} thread paralleli...")

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(search_contact, i, contact): contact
            for i, contact in enumerate(contacts)
        }
        for future in as_completed(futures):
            future.result()  # propaga eventuali eccezioni non gestite

    # Report
    print_report(search_results)

    # Salva file TXT con numeri trovati
    save_found_numbers(search_results, args.found)

    if args.output:
        save_json_report(search_results, args.output)


if __name__ == "__main__":
    main()
