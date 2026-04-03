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
from dataclasses import dataclass, field
from pathlib import Path


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

def search_duckduckgo_html(query: str, timeout: int = 15) -> list[dict]:
    """
    Cerca su DuckDuckGo HTML version (lite) e restituisce i risultati trovati.
    Non richiede API key.
    """
    url = "https://html.duckduckgo.com/html/"
    data = urllib.parse.urlencode({"q": query}).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; PhoneSearchTool/1.0)",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return [{"error": str(e)}]

    # Parsing basilare dei risultati HTML di DuckDuckGo
    results = []
    # I risultati sono in <a class="result__a" href="...">titolo</a>
    # e snippet in <a class="result__snippet">...</a>
    link_pattern = re.compile(
        r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
        re.DOTALL,
    )
    snippet_pattern = re.compile(
        r'class="result__snippet"[^>]*>(.*?)</(?:a|td)',
        re.DOTALL,
    )

    links = link_pattern.findall(html)
    snippets = snippet_pattern.findall(html)

    for i, (href, title) in enumerate(links):
        title_clean = re.sub(r"<[^>]+>", "", title).strip()
        snippet = ""
        if i < len(snippets):
            snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip()

        # Decodifica URL redirect di DuckDuckGo
        if "uddg=" in href:
            match = re.search(r"uddg=([^&]+)", href)
            if match:
                href = urllib.parse.unquote(match.group(1))

        results.append({
            "title": title_clean,
            "url": href,
            "snippet": snippet,
        })

    return results


def search_phone_number(phone: str, delay: float = 2.0) -> list[dict]:
    """Cerca un numero di telefono online con diverse query."""
    all_results = []

    # Query 1: numero esatto tra virgolette
    query = f'"{phone}"'
    results = search_duckduckgo_html(query)
    for r in results:
        r["query"] = query
    all_results.extend(results)

    time.sleep(delay)

    # Query 2: numero con spazi (formato comune italiano)
    if len(phone) >= 10:
        spaced = phone
        if phone.startswith("+"):
            # es. +39 333 1234567 -> cerca anche con spazi
            spaced = phone[:3] + " " + phone[3:6] + " " + phone[6:]
        query2 = f'"{spaced}"'
        results2 = search_duckduckgo_html(query2)
        for r in results2:
            r["query"] = query2
        all_results.extend(results2)

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

    # Ricerca
    search_results: list[SearchResult] = []
    total = len(contacts)

    for i, contact in enumerate(contacts, 1):
        print(f"\n[{i}/{total}] Ricerca: {contact.name} ({contact.phone})...", end="", flush=True)

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
            search_results.append(sr)

            if valid:
                print(f" {len(valid)} risultati trovati")
            else:
                print(" nessun risultato")

        except Exception as e:
            sr = SearchResult(
                contact=contact,
                query=f'"{contact.phone}"',
                error=str(e),
            )
            search_results.append(sr)
            print(f" ERRORE: {e}")

        # Pausa tra un contatto e l'altro
        if i < total:
            time.sleep(args.delay)

    # Report
    print_report(search_results)

    if args.output:
        save_json_report(search_results, args.output)


if __name__ == "__main__":
    main()
