#!/usr/bin/env python3
"""
Phone Number Search Tool v2.0
Analizza una rubrica telefonica (CSV o vCard/.vcf) e cerca ogni numero online
su piu motori di ricerca (Google, Bing, Yandex, Tellows, Chi-mi-chiama, Pagine Bianche).

Funzionalita:
  - Multi-engine: Google, Bing, Yandex + siti specifici italiani
  - Resume automatico: riprende da dove si era fermato
  - Rotazione User-Agent e supporto proxy
  - Categorizzazione risultati (spam, social, annunci, directory, data breach)
  - Report HTML navigabile con colori
  - Cache SQLite per non ripetere ricerche recenti
  - Rate limiting intelligente (adattivo)
  - Notifiche email/Telegram a fine scansione
  - Modalita monitoraggio (confronto con scansioni precedenti)
"""

import argparse
import csv
import email.mime.text
import hashlib
import json
import os
import random
import re
import smtplib
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

# ---------------------------------------------------------------------------
# User-Agent rotation
# ---------------------------------------------------------------------------

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
]


def get_random_ua() -> str:
    return random.choice(USER_AGENTS)


# ---------------------------------------------------------------------------
# Proxy support
# ---------------------------------------------------------------------------

class ProxyManager:
    def __init__(self, proxy_file: str = ""):
        self.proxies: list[str] = []
        self.index = 0
        self.lock = Lock()
        if proxy_file and os.path.isfile(proxy_file):
            with open(proxy_file) as f:
                self.proxies = [line.strip() for line in f if line.strip()]
            print(f"  Caricati {len(self.proxies)} proxy da {proxy_file}")

    def get_proxy(self) -> str | None:
        if not self.proxies:
            return None
        with self.lock:
            proxy = self.proxies[self.index % len(self.proxies)]
            self.index += 1
            return proxy

    def build_opener(self) -> urllib.request.OpenerDirector:
        proxy = self.get_proxy()
        if proxy:
            proxy_handler = urllib.request.ProxyHandler({
                "http": proxy,
                "https": proxy,
            })
            return urllib.request.build_opener(proxy_handler)
        return urllib.request.build_opener()


proxy_manager = ProxyManager()


# ---------------------------------------------------------------------------
# Rate limiting intelligente
# ---------------------------------------------------------------------------

class AdaptiveRateLimiter:
    def __init__(self, base_delay: float = 3.0):
        self.base_delay = base_delay
        self.current_delay = base_delay
        self.min_delay = 1.5
        self.max_delay = 30.0
        self.lock = Lock()
        self.consecutive_ok = 0
        self.consecutive_fail = 0

    def success(self):
        with self.lock:
            self.consecutive_ok += 1
            self.consecutive_fail = 0
            if self.consecutive_ok >= 3:
                self.current_delay = max(self.min_delay, self.current_delay * 0.8)
                self.consecutive_ok = 0

    def failure(self):
        with self.lock:
            self.consecutive_fail += 1
            self.consecutive_ok = 0
            self.current_delay = min(self.max_delay, self.current_delay * 2)

    def wait(self):
        delay = self.current_delay + random.uniform(0, 1)
        time.sleep(delay)

    def get_delay(self) -> float:
        return self.current_delay


rate_limiter = AdaptiveRateLimiter()


# ---------------------------------------------------------------------------
# Categorizzazione risultati
# ---------------------------------------------------------------------------

CATEGORIES = {
    "spam": {
        "keywords": ["spam", "truffa", "scam", "telemarketing", "call center",
                      "segnalazione", "blocca", "indesiderat", "molest", "phishing"],
        "domains": ["tellows", "chi-mi-chiama", "unknownphone", "dovechiamare",
                     "numericentrali", "chicercachiama"],
        "color": "#e74c3c",
        "label": "SPAM/Truffa",
    },
    "social": {
        "keywords": ["facebook", "instagram", "twitter", "linkedin", "tiktok",
                      "whatsapp", "telegram", "social", "profilo"],
        "domains": ["facebook.com", "instagram.com", "twitter.com", "linkedin.com",
                     "tiktok.com"],
        "color": "#3498db",
        "label": "Social Media",
    },
    "annunci": {
        "keywords": ["annuncio", "vendita", "compra", "subito", "bakeca",
                      "kijiji", "ebay", "marketplace", "usato"],
        "domains": ["subito.it", "bakeca.it", "kijiji.it", "ebay.it",
                     "marketplace", "autoscout"],
        "color": "#f39c12",
        "label": "Annunci",
    },
    "directory": {
        "keywords": ["pagine bianche", "pagine gialle", "elenco", "directory",
                      "azienda", "impresa", "partita iva"],
        "domains": ["paginebianche.it", "paginegialle.it", "tuttocitta.it",
                     "infobel.com", "europages"],
        "color": "#2ecc71",
        "label": "Directory/Aziende",
    },
    "data_breach": {
        "keywords": ["breach", "leak", "dump", "compromesso", "esposto",
                      "data breach", "database", "hack"],
        "domains": ["haveibeenpwned", "dehashed", "leakcheck", "pastebin"],
        "color": "#8e44ad",
        "label": "Data Breach",
    },
}


def categorize_result(result: dict) -> str:
    url = result.get("url", "").lower()
    title = result.get("title", "").lower()
    snippet = result.get("snippet", "").lower()
    text = f"{url} {title} {snippet}"

    for cat_name, cat_info in CATEGORIES.items():
        for domain in cat_info["domains"]:
            if domain in url:
                return cat_name
        for keyword in cat_info["keywords"]:
            if keyword in text:
                return cat_name

    return "altro"


# ---------------------------------------------------------------------------
# SQLite Cache
# ---------------------------------------------------------------------------

class SearchCache:
    def __init__(self, db_path: str = "phone_search_cache.db", max_age_hours: int = 168):
        self.db_path = db_path
        self.max_age = max_age_hours * 3600
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.lock = Lock()
        self._init_db()

    def _init_db(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                phone TEXT PRIMARY KEY,
                results TEXT,
                timestamp REAL
            )
        """)
        self.conn.commit()

    def get(self, phone: str) -> list[dict] | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT results, timestamp FROM cache WHERE phone = ?", (phone,)
            ).fetchone()
            if row:
                age = time.time() - row[1]
                if age < self.max_age:
                    return json.loads(row[0])
                self.conn.execute("DELETE FROM cache WHERE phone = ?", (phone,))
                self.conn.commit()
            return None

    def set(self, phone: str, results: list[dict]):
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO cache (phone, results, timestamp) VALUES (?, ?, ?)",
                (phone, json.dumps(results, ensure_ascii=False), time.time()),
            )
            self.conn.commit()

    def close(self):
        self.conn.close()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

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
    categories: dict = field(default_factory=dict)
    from_cache: bool = False


# ---------------------------------------------------------------------------
# Parsing rubrica
# ---------------------------------------------------------------------------

def normalize_phone(raw: str) -> str:
    cleaned = re.sub(r"[^\d+]", "", raw)
    return cleaned


def parse_csv(filepath: str) -> list[Contact]:
    contacts = []
    with open(filepath, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return contacts
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
                    if len(phone) >= 6:
                        contacts.append(Contact(name=name, phone=phone))
    return contacts


def parse_vcf(filepath: str) -> list[Contact]:
    contacts = []
    current_name = ""
    with open(filepath, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("FN:") or line.startswith("FN;"):
                current_name = line.split(":", 1)[1].strip()
            elif line.upper().startswith("TEL"):
                raw_phone = line.split(":", 1)[1].strip() if ":" in line else ""
                phone = normalize_phone(raw_phone)
                if len(phone) >= 6:
                    contacts.append(Contact(name=current_name or "Sconosciuto", phone=phone))
            elif line == "END:VCARD":
                current_name = ""
    return contacts


def load_contacts(filepath: str) -> list[Contact]:
    ext = Path(filepath).suffix.lower()
    if ext == ".vcf":
        return parse_vcf(filepath)
    elif ext in (".csv", ".tsv", ".txt"):
        return parse_csv(filepath)
    else:
        print(f"Formato non riconosciuto: {ext}. Provo come CSV...")
        return parse_csv(filepath)


def strip_prefix(phone: str) -> str:
    if phone.startswith("+39"):
        return phone[3:]
    if phone.startswith("0039"):
        return phone[4:]
    return phone


# ---------------------------------------------------------------------------
# Motori di ricerca
# ---------------------------------------------------------------------------

def _fetch_html(url: str, method: str = "GET", data: bytes = None, timeout: int = 8) -> str:
    headers = {
        "User-Agent": get_random_ua(),
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if data:
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = proxy_manager.build_opener()
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        raise


def search_google(query: str) -> list[dict]:
    """Cerca su Google."""
    encoded = urllib.parse.quote_plus(query)
    url = f"https://www.google.com/search?q={encoded}&num=10&hl=it"
    try:
        html = _fetch_html(url)
        rate_limiter.success()
    except Exception as e:
        rate_limiter.failure()
        return [{"error": f"Google: {e}"}]

    results = []
    seen_urls = set()

    for pattern in [
        re.compile(r'<a[^>]+href="/url\?q=([^"&]+)[^"]*"[^>]*>(.*?)</a>', re.DOTALL),
        re.compile(r'<a[^>]+href="(https?://(?!google\.com|accounts\.google)[^"]+)"[^>]*>(.*?)</a>', re.DOTALL),
    ]:
        for href, title in pattern.findall(html):
            href = urllib.parse.unquote(href)
            if any(x in href for x in ["google.com", "accounts.google", "support.google",
                                        "maps.google", "policies.google", "webcache"]):
                continue
            if href in seen_urls:
                continue
            seen_urls.add(href)
            title_clean = re.sub(r"<[^>]+>", "", title).strip()
            if title_clean and len(title_clean) >= 3:
                results.append({"title": title_clean, "url": href, "snippet": "", "engine": "Google"})

    return results


def search_bing(query: str) -> list[dict]:
    """Cerca su Bing."""
    encoded = urllib.parse.quote_plus(query)
    url = f"https://www.bing.com/search?q={encoded}&count=10"
    try:
        html = _fetch_html(url)
        rate_limiter.success()
    except Exception as e:
        rate_limiter.failure()
        return [{"error": f"Bing: {e}"}]

    results = []
    link_pattern = re.compile(r'<a[^>]+href="(https?://(?!bing\.com|microsoft\.com|msn\.com)[^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
    seen = set()
    for href, title in link_pattern.findall(html):
        if href in seen:
            continue
        seen.add(href)
        title_clean = re.sub(r"<[^>]+>", "", title).strip()
        if title_clean and len(title_clean) >= 3 and "bing.com" not in href:
            results.append({"title": title_clean, "url": href, "snippet": "", "engine": "Bing"})
    return results


def search_yandex(query: str) -> list[dict]:
    """Cerca su Yandex."""
    encoded = urllib.parse.quote_plus(query)
    url = f"https://yandex.com/search/?text={encoded}&lr=10511"
    try:
        html = _fetch_html(url)
        rate_limiter.success()
    except Exception as e:
        rate_limiter.failure()
        return [{"error": f"Yandex: {e}"}]

    results = []
    link_pattern = re.compile(r'<a[^>]+href="(https?://(?!yandex\.|ya\.)[^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
    seen = set()
    for href, title in link_pattern.findall(html):
        if href in seen:
            continue
        seen.add(href)
        title_clean = re.sub(r"<[^>]+>", "", title).strip()
        if title_clean and len(title_clean) >= 5:
            results.append({"title": title_clean, "url": href, "snippet": "", "engine": "Yandex"})
    return results


def search_duckduckgo(query: str) -> list[dict]:
    """Cerca su DuckDuckGo (fallback)."""
    url = "https://html.duckduckgo.com/html/"
    data = urllib.parse.urlencode({"q": query}).encode("utf-8")
    try:
        html = _fetch_html(url, method="POST", data=data)
        rate_limiter.success()
    except Exception as e:
        rate_limiter.failure()
        return [{"error": f"DuckDuckGo: {e}"}]

    results = []
    link_pattern = re.compile(r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', re.DOTALL)
    snippet_pattern = re.compile(r'class="result__snippet"[^>]*>(.*?)</(?:a|td)', re.DOTALL)
    links = link_pattern.findall(html)
    snippets = snippet_pattern.findall(html)

    for i, (href, title) in enumerate(links):
        title_clean = re.sub(r"<[^>]+>", "", title).strip()
        snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip() if i < len(snippets) else ""
        if "uddg=" in href:
            m = re.search(r"uddg=([^&]+)", href)
            if m:
                href = urllib.parse.unquote(m.group(1))
        results.append({"title": title_clean, "url": href, "snippet": snippet, "engine": "DuckDuckGo"})
    return results


def search_tellows(phone_no_prefix: str) -> list[dict]:
    """Cerca su Tellows.it."""
    url = f"https://www.tellows.it/num/{phone_no_prefix}"
    try:
        html = _fetch_html(url)
        if "Nessuna valutazione" not in html and "nessun commento" not in html.lower():
            score_match = re.search(r'score["\s:]+(\d)', html)
            comments = len(re.findall(r'class="comment-body"', html))
            if comments > 0 or score_match:
                score = score_match.group(1) if score_match else "?"
                return [{
                    "title": f"Tellows: {phone_no_prefix} - Score {score}/9, {comments} commenti",
                    "url": url,
                    "snippet": f"Numero segnalato su Tellows con score {score}/9 e {comments} commenti utenti",
                    "engine": "Tellows",
                }]
    except Exception:
        pass
    return []


def search_chimicchiama(phone_no_prefix: str) -> list[dict]:
    """Cerca su Chi-mi-chiama.it."""
    url = f"https://www.chi-mi-chiama.it/numero/{phone_no_prefix}"
    try:
        html = _fetch_html(url)
        if "non trovato" not in html.lower() and len(html) > 5000:
            comments = len(re.findall(r'class="[^"]*comment[^"]*"', html))
            if comments > 0:
                return [{
                    "title": f"Chi-mi-chiama: {phone_no_prefix} - {comments} segnalazioni",
                    "url": url,
                    "snippet": f"Numero trovato su chi-mi-chiama.it con {comments} segnalazioni utenti",
                    "engine": "Chi-mi-chiama",
                }]
    except Exception:
        pass
    return []


def search_paginebianche(phone_no_prefix: str) -> list[dict]:
    """Cerca su PagineBianche.it."""
    url = f"https://www.paginebianche.it/ricerca?qs={phone_no_prefix}&type=numero"
    try:
        html = _fetch_html(url)
        name_match = re.search(r'class="[^"]*listing-name[^"]*"[^>]*>(.*?)</[^>]+>', html, re.DOTALL)
        if name_match:
            name = re.sub(r"<[^>]+>", "", name_match.group(1)).strip()
            return [{
                "title": f"PagineBianche: {name}",
                "url": url,
                "snippet": f"Numero registrato su PagineBianche a nome: {name}",
                "engine": "PagineBianche",
            }]
    except Exception:
        pass
    return []


SEARCH_ENGINES = {
    "google": search_google,
    "bing": search_bing,
    "yandex": search_yandex,
    "duckduckgo": search_duckduckgo,
}

SPECIFIC_SITES = {
    "tellows": search_tellows,
    "chimicchiama": search_chimicchiama,
    "paginebianche": search_paginebianche,
}


# ---------------------------------------------------------------------------
# Ricerca principale multi-engine
# ---------------------------------------------------------------------------

def search_phone_number(phone: str, cache: SearchCache = None, engines: list[str] = None) -> list[dict]:
    """Cerca un numero su tutti i motori di ricerca."""
    # Check cache
    if cache:
        cached = cache.get(phone)
        if cached is not None:
            return cached

    all_results = []
    phone_no_prefix = strip_prefix(phone)
    active_engines = engines or list(SEARCH_ENGINES.keys())

    # Strategia: distribuisci le query tra i motori per ridurre le richieste
    # Ogni motore cerca una variante diversa del numero
    queries = [f'"{phone}"']
    if phone_no_prefix != phone:
        queries.append(f'"{phone_no_prefix}"')
    if len(phone_no_prefix) >= 9:
        spaced = phone_no_prefix[:3] + " " + phone_no_prefix[3:6] + " " + phone_no_prefix[6:]
        queries.append(f'"{spaced}"')

    # Assegna una query diversa a ciascun motore (round-robin)
    for i, eng_name in enumerate(active_engines):
        if eng_name in SEARCH_ENGINES:
            query = queries[i % len(queries)]
            print(f"        [{phone_no_prefix}] -> {eng_name}: {query}", flush=True)
            results = SEARCH_ENGINES[eng_name](query)
            valid_count = len([r for r in results if "error" not in r])
            err_count = len([r for r in results if "error" in r])
            print(f"        [{phone_no_prefix}] <- {eng_name}: {valid_count} risultati, {err_count} errori", flush=True)
            for r in results:
                r["query"] = query
            all_results.extend(results)
            rate_limiter.wait()

    # Cerca sui siti specifici (veloci, una richiesta ciascuno)
    for site_name, site_func in SPECIFIC_SITES.items():
        print(f"        [{phone_no_prefix}] -> {site_name}", flush=True)
        results = site_func(phone_no_prefix)
        for r in results:
            r["query"] = phone_no_prefix
        all_results.extend(results)

    # Deduplica per URL
    seen = set()
    unique = []
    for r in all_results:
        if "error" in r:
            unique.append(r)
            continue
        url = r.get("url", "")
        if url not in seen:
            seen.add(url)
            r["category"] = categorize_result(r)
            unique.append(r)

    # Salva in cache solo se ci sono risultati validi (non cacheficare errori)
    valid_results = [r for r in unique if "error" not in r]
    if cache and valid_results:
        cache.set(phone, unique)

    return unique


# ---------------------------------------------------------------------------
# Resume (ripresa automatica)
# ---------------------------------------------------------------------------

PROGRESS_FILE = "phone_search_progress.json"


def save_progress(progress_file: str, completed_phones: dict, input_file: str):
    data = {
        "input_file": input_file,
        "timestamp": time.time(),
        "completed": completed_phones,
    }
    with open(progress_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def load_progress(progress_file: str, input_file: str) -> dict:
    if not os.path.isfile(progress_file):
        return {}
    try:
        with open(progress_file, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("input_file") != input_file:
            return {}
        return data.get("completed", {})
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Report TXT
# ---------------------------------------------------------------------------

def print_report(search_results: list[SearchResult]):
    found_count = 0
    not_found_count = 0
    error_count = 0
    cache_count = 0

    print("\n" + "=" * 70)
    print("  REPORT RICERCA NUMERI TELEFONICI ONLINE")
    print("=" * 70)

    for sr in search_results:
        valid_results = [r for r in sr.results if "error" not in r]
        has_results = len(valid_results) > 0

        if sr.from_cache:
            cache_count += 1

        if sr.error:
            status = "ERRORE"
            error_count += 1
        elif has_results:
            cats = {}
            for r in valid_results:
                cat = r.get("category", "altro")
                cats[cat] = cats.get(cat, 0) + 1
            cat_str = ", ".join(f"{CATEGORIES.get(c, {}).get('label', c)}:{n}" for c, n in cats.items())
            status = f"TROVATO ({len(valid_results)} risultati) [{cat_str}]"
            found_count += 1
        else:
            status = "NON TROVATO"
            not_found_count += 1

        cached_tag = " [CACHE]" if sr.from_cache else ""
        print(f"\n{'-' * 70}")
        print(f"  {sr.contact.name} | {sr.contact.phone}{cached_tag}")
        print(f"  Stato: {status}")

        if sr.error:
            print(f"  Errore: {sr.error}")

        if has_results:
            for i, r in enumerate(valid_results[:5], 1):
                cat_label = CATEGORIES.get(r.get("category", ""), {}).get("label", "Altro")
                engine = r.get("engine", "?")
                print(f"\n    [{i}] [{engine}] [{cat_label}] {r.get('title', 'N/A')}")
                print(f"        URL: {r.get('url', 'N/A')}")
                if r.get("snippet"):
                    print(f"        {r['snippet'][:150]}")

    print(f"\n{'=' * 70}")
    print(f"  RIEPILOGO")
    print(f"  Totale numeri analizzati: {len(search_results)}")
    print(f"  Trovati online:          {found_count}")
    print(f"  Non trovati:             {not_found_count}")
    print(f"  Errori:                  {error_count}")
    print(f"  Da cache:                {cache_count}")
    print(f"{'=' * 70}\n")


def save_json_report(search_results: list[SearchResult], output_path: str):
    data = []
    for sr in search_results:
        data.append({
            "name": sr.contact.name, "phone": sr.contact.phone,
            "query": sr.query, "error": sr.error, "from_cache": sr.from_cache,
            "categories": sr.categories,
            "results_count": len([r for r in sr.results if "error" not in r]),
            "results": sr.results,
        })
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"\nReport JSON salvato in: {output_path}")


def save_found_numbers(search_results: list[SearchResult], output_path: str):
    found = [sr for sr in search_results if any("error" not in r for r in sr.results)]
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("NUMERI TROVATI ONLINE\n")
        f.write(f"Data: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Totale: {len(found)} numeri con risultati positivi\n")
        f.write("=" * 60 + "\n\n")
        for sr in found:
            valid_results = [r for r in sr.results if "error" not in r]
            cats = {}
            for r in valid_results:
                cat = r.get("category", "altro")
                cats[cat] = cats.get(cat, 0) + 1
            cat_str = ", ".join(f"{CATEGORIES.get(c, {}).get('label', c)}:{n}" for c, n in cats.items())
            f.write(f"Nome: {sr.contact.name}\n")
            f.write(f"Numero: {sr.contact.phone}\n")
            f.write(f"Risultati: {len(valid_results)} | Categorie: {cat_str}\n\n")
            for i, r in enumerate(valid_results[:10], 1):
                cat_label = CATEGORIES.get(r.get("category", ""), {}).get("label", "Altro")
                engine = r.get("engine", "?")
                f.write(f"  [{i}] [{engine}] [{cat_label}] {r.get('title', 'N/A')}\n")
                f.write(f"      URL: {r.get('url', 'N/A')}\n")
                if r.get("snippet"):
                    f.write(f"      Dettagli: {r['snippet'][:200]}\n")
                f.write("\n")
            f.write("-" * 60 + "\n\n")
    print(f"File numeri trovati salvato in: {output_path}")


# ---------------------------------------------------------------------------
# Report HTML
# ---------------------------------------------------------------------------

def save_html_report(search_results: list[SearchResult], output_path: str):
    found = [sr for sr in search_results if any("error" not in r for r in sr.results)]
    not_found = [sr for sr in search_results if not any("error" not in r for r in sr.results) and not sr.error]
    errors = [sr for sr in search_results if sr.error]

    html_parts = ["""<!DOCTYPE html>
<html lang="it"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Phone Search Report</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 20px; background: #f5f6fa; color: #2c3e50; }
  .header { background: linear-gradient(135deg, #667eea, #764ba2); color: white; padding: 30px; border-radius: 12px; margin-bottom: 20px; }
  .header h1 { margin: 0 0 10px 0; }
  .stats { display: flex; gap: 15px; flex-wrap: wrap; margin-bottom: 20px; }
  .stat { background: white; padding: 15px 25px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); text-align: center; }
  .stat .number { font-size: 2em; font-weight: bold; }
  .stat.found .number { color: #e74c3c; }
  .stat.clean .number { color: #2ecc71; }
  .stat.error .number { color: #f39c12; }
  .card { background: white; border-radius: 8px; padding: 20px; margin-bottom: 15px; box-shadow: 0 2px 10px rgba(0,0,0,0.08); border-left: 4px solid #e74c3c; }
  .card.clean { border-left-color: #2ecc71; }
  .card.error { border-left-color: #f39c12; }
  .card h3 { margin: 0 0 5px 0; }
  .card .phone { color: #7f8c8d; font-size: 0.9em; }
  .result { margin: 10px 0; padding: 10px; background: #f8f9fa; border-radius: 6px; }
  .result .engine { font-size: 0.75em; background: #3498db; color: white; padding: 2px 8px; border-radius: 10px; }
  .result .category { font-size: 0.75em; padding: 2px 8px; border-radius: 10px; color: white; }
  .result a { color: #3498db; text-decoration: none; word-break: break-all; }
  .result a:hover { text-decoration: underline; }
  .result .snippet { color: #7f8c8d; font-size: 0.85em; margin-top: 4px; }
  .filter-bar { background: white; padding: 15px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.08); }
  .filter-bar button { padding: 8px 16px; border: none; border-radius: 6px; cursor: pointer; margin: 3px; background: #ecf0f1; }
  .filter-bar button.active { background: #667eea; color: white; }
  .hidden { display: none; }
</style></head><body>
<div class="header">
  <h1>Phone Number Search Report</h1>
  <p>Generato il """ + time.strftime('%Y-%m-%d %H:%M:%S') + f""" | {len(search_results)} numeri analizzati</p>
</div>

<div class="stats">
  <div class="stat found"><div class="number">{len(found)}</div><div>Trovati Online</div></div>
  <div class="stat clean"><div class="number">{len(not_found)}</div><div>Puliti</div></div>
  <div class="stat error"><div class="number">{len(errors)}</div><div>Errori</div></div>
</div>

<div class="filter-bar">
  <b>Filtra:</b>
  <button class="active" onclick="filterCards('all')">Tutti</button>
  <button onclick="filterCards('found')">Trovati</button>
  <button onclick="filterCards('clean')">Puliti</button>
  <button onclick="filterCards('error')">Errori</button>
</div>
"""]

    for sr in search_results:
        valid = [r for r in sr.results if "error" not in r]
        if sr.error:
            css_class = "error"
            status = "ERRORE"
        elif valid:
            css_class = "found"
            status = f"{len(valid)} risultati"
        else:
            css_class = "clean"
            status = "Pulito"

        cache_tag = " [CACHE]" if sr.from_cache else ""
        html_parts.append(f'<div class="card {css_class}" data-type="{css_class}">')
        html_parts.append(f'  <h3>{sr.contact.name}{cache_tag}</h3>')
        html_parts.append(f'  <div class="phone">{sr.contact.phone} | {status}</div>')

        if sr.error:
            html_parts.append(f'  <p style="color:#e74c3c">{sr.error}</p>')

        for r in valid[:10]:
            cat = r.get("category", "altro")
            cat_info = CATEGORIES.get(cat, {"color": "#95a5a6", "label": "Altro"})
            engine = r.get("engine", "?")
            html_parts.append(f'  <div class="result">')
            html_parts.append(f'    <span class="engine">{engine}</span>')
            html_parts.append(f'    <span class="category" style="background:{cat_info["color"]}">{cat_info["label"]}</span>')
            html_parts.append(f'    <br><b>{r.get("title", "N/A")}</b>')
            html_parts.append(f'    <br><a href="{r.get("url", "#")}" target="_blank">{r.get("url", "N/A")}</a>')
            if r.get("snippet"):
                html_parts.append(f'    <div class="snippet">{r["snippet"][:200]}</div>')
            html_parts.append(f'  </div>')

        html_parts.append('</div>')

    html_parts.append("""
<script>
function filterCards(type) {
  document.querySelectorAll('.filter-bar button').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  document.querySelectorAll('.card').forEach(card => {
    if (type === 'all' || card.dataset.type === type) {
      card.classList.remove('hidden');
    } else {
      card.classList.add('hidden');
    }
  });
}
</script></body></html>""")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(html_parts))
    print(f"Report HTML salvato in: {output_path}")


# ---------------------------------------------------------------------------
# Notifiche (Email + Telegram)
# ---------------------------------------------------------------------------

def send_email_notification(smtp_server: str, smtp_port: int, sender: str,
                            password: str, recipient: str, subject: str, body: str):
    try:
        msg = email.mime.text.MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = recipient
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(sender, password)
            server.send_message(msg)
        print(f"Email inviata a {recipient}")
    except Exception as e:
        print(f"Errore invio email: {e}")


def send_telegram_notification(bot_token: str, chat_id: str, message: str):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        print(f"Notifica Telegram inviata a chat {chat_id}")
    except Exception as e:
        print(f"Errore invio Telegram: {e}")


def build_notification_summary(search_results: list[SearchResult]) -> str:
    found = sum(1 for sr in search_results if any("error" not in r for r in sr.results))
    total = len(search_results)
    lines = [
        f"[*] Phone Search completato!",
        f"Analizzati: {total} numeri",
        f"Trovati online: {found}",
        f"Puliti: {total - found}",
        "",
    ]
    for sr in search_results:
        valid = [r for r in sr.results if "error" not in r]
        if valid:
            lines.append(f"[!] {sr.contact.name} ({sr.contact.phone}): {len(valid)} risultati")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Monitoraggio (confronto con scansioni precedenti)
# ---------------------------------------------------------------------------

MONITOR_FILE = "phone_search_history.json"


def load_history(monitor_file: str) -> dict:
    if not os.path.isfile(monitor_file):
        return {}
    try:
        with open(monitor_file, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_history(monitor_file: str, search_results: list[SearchResult]):
    history = load_history(monitor_file)
    current = {}
    for sr in search_results:
        valid = [r for r in sr.results if "error" not in r]
        current[sr.contact.phone] = {
            "name": sr.contact.name,
            "results_count": len(valid),
            "urls": [r.get("url", "") for r in valid],
            "timestamp": time.time(),
        }
    history[time.strftime("%Y-%m-%d_%H:%M:%S")] = current
    with open(monitor_file, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


def compare_with_history(monitor_file: str, search_results: list[SearchResult]) -> list[str]:
    history = load_history(monitor_file)
    if not history:
        return ["Prima scansione - nessun confronto disponibile."]

    last_scan_key = sorted(history.keys())[-1]
    last_scan = history[last_scan_key]
    changes = []

    for sr in search_results:
        valid = [r for r in sr.results if "error" not in r]
        current_urls = set(r.get("url", "") for r in valid)
        prev = last_scan.get(sr.contact.phone, {})
        prev_urls = set(prev.get("urls", []))

        new_urls = current_urls - prev_urls
        if new_urls:
            changes.append(f"[NEW] {sr.contact.name} ({sr.contact.phone}): {len(new_urls)} NUOVI risultati")
            for u in list(new_urls)[:3]:
                changes.append(f"    -> {u}")

        if not prev and valid:
            changes.append(f"[!] {sr.contact.name} ({sr.contact.phone}): appare online per la PRIMA VOLTA ({len(valid)} risultati)")

    if not changes:
        changes.append("[OK] Nessun cambiamento rispetto alla scansione precedente.")

    return changes


# ---------------------------------------------------------------------------
# Lookup singolo numero
# ---------------------------------------------------------------------------

def lookup_single_number(phone_input: str, contacts: list[Contact], cache: SearchCache = None,
                         engines: list[str] = None):
    """Cerca un singolo numero nella rubrica e online."""
    phone = normalize_phone(phone_input)
    phone_no_prefix = strip_prefix(phone)

    print(f"\n{'=' * 70}")
    print(f"  RICERCA NUMERO: {phone}")
    print(f"{'=' * 70}")

    # 1. Confronto con rubrica
    print(f"\n[>] Confronto con rubrica ({len(contacts)} contatti)...")
    found_in_contacts = []
    for c in contacts:
        c_no_prefix = strip_prefix(c.phone)
        if (c.phone == phone or c_no_prefix == phone_no_prefix
                or c.phone == phone_no_prefix or c_no_prefix == phone):
            found_in_contacts.append(c)

    if found_in_contacts:
        print(f"\n  [OK] TROVATO IN RUBRICA ({len(found_in_contacts)} corrispondenze):")
        for c in found_in_contacts:
            print(f"     -> {c.name} ({c.phone})")
    else:
        print(f"\n  [X] NON presente in rubrica")

    # 2. Ricerca online
    print(f"\n[?] Ricerca online in corso...")
    results = search_phone_number(phone, cache=cache, engines=engines)
    valid = [r for r in results if "error" not in r]
    errors = [r for r in results if "error" in r]

    if valid:
        cats = {}
        for r in valid:
            cat = r.get("category", "altro")
            cats[cat] = cats.get(cat, 0) + 1
        cat_str = ", ".join(f"{CATEGORIES.get(c, {}).get('label', c)}:{n}" for c, n in cats.items())

        print(f"\n  [!]  TROVATO ONLINE: {len(valid)} risultati [{cat_str}]")
        for i, r in enumerate(valid[:10], 1):
            cat_label = CATEGORIES.get(r.get("category", ""), {}).get("label", "Altro")
            engine = r.get("engine", "?")
            print(f"\n    [{i}] [{engine}] [{cat_label}] {r.get('title', 'N/A')}")
            print(f"        URL: {r.get('url', 'N/A')}")
            if r.get("snippet"):
                print(f"        {r['snippet'][:150]}")
    else:
        print(f"\n  [OK] NON trovato online")

    if errors:
        print(f"\n  [!]  {len(errors)} motori hanno restituito errori")

    # Riepilogo
    print(f"\n{'=' * 70}")
    print(f"  RIEPILOGO per {phone}:")
    print(f"    In rubrica:  {'SI' if found_in_contacts else 'NO'}", end="")
    if found_in_contacts:
        print(f" ({', '.join(c.name for c in found_in_contacts)})", end="")
    print()
    print(f"    Online:      {'SI' if valid else 'NO'} ({len(valid)} risultati)")
    print(f"{'=' * 70}\n")

    return found_in_contacts, valid


def interactive_lookup(contacts: list[Contact], cache: SearchCache = None,
                       engines: list[str] = None):
    """Modalita interattiva: inserisci numeri uno alla volta."""
    print("\n[#] MODALITA RICERCA SINGOLO NUMERO")
    print("   Inserisci un numero di telefono per cercarlo nella rubrica e online.")
    print("   Digita 'q' o 'esci' per uscire.\n")

    while True:
        try:
            phone_input = input("  Numero da cercare: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n")
            break

        if not phone_input or phone_input.lower() in ("q", "esci", "exit", "quit"):
            break

        lookup_single_number(phone_input, contacts, cache=cache, engines=engines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Phone Number Search Tool v2.0 - Cerca numeri telefonici online",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Formati supportati:
  CSV, VCF (vCard)

Motori di ricerca:
  Google, Bing, Yandex, DuckDuckGo + Tellows, Chi-mi-chiama, PagineBianche

Esempi:
  python phone_search.py rubrica.csv
  python phone_search.py rubrica.vcf --workers 8 --html report.html
  python phone_search.py rubrica.csv --engines google,bing --delay 3
  python phone_search.py rubrica.csv --resume
  python phone_search.py rubrica.csv --monitor
  python phone_search.py rubrica.csv --proxy-file proxies.txt
  python phone_search.py rubrica.csv --telegram-token BOT:TOKEN --telegram-chat 12345
  python phone_search.py rubrica.csv --lookup +393331234567
  python phone_search.py rubrica.csv --interactive
        """,
    )
    parser.add_argument("file", help="File rubrica (CSV o VCF)")
    parser.add_argument("--output", "-o", help="Salva risultati in JSON")
    parser.add_argument("--html", help="Genera report HTML")
    parser.add_argument("--found", "-f", default="numeri_trovati.txt", help="File TXT numeri trovati (default: numeri_trovati.txt)")
    parser.add_argument("--delay", "-d", type=float, default=2.0, help="Delay base tra ricerche in secondi (default: 2)")
    parser.add_argument("--limit", "-l", type=int, default=0, help="Max contatti da cercare (0=tutti)")
    parser.add_argument("--workers", "-w", type=int, default=3, help="Thread paralleli (default: 3)")
    parser.add_argument("--engines", default="google,bing,duckduckgo", help="Motori da usare separati da virgola (default: google,bing,duckduckgo)")
    parser.add_argument("--proxy-file", default="", help="File con lista proxy (uno per riga, formato http://ip:port)")
    parser.add_argument("--resume", action="store_true", help="Riprendi scansione interrotta")
    parser.add_argument("--no-cache", action="store_true", help="Ignora la cache")
    parser.add_argument("--clear-cache", action="store_true", help="Cancella la cache e ricomincia da zero")
    parser.add_argument("--cache-hours", type=int, default=168, help="Ore validita cache (default: 168 = 7 giorni)")
    parser.add_argument("--monitor", action="store_true", help="Confronta con scansione precedente")
    parser.add_argument("--telegram-token", default="", help="Bot token Telegram per notifiche")
    parser.add_argument("--telegram-chat", default="", help="Chat ID Telegram")
    parser.add_argument("--email-smtp", default="", help="Server SMTP (es. smtp.gmail.com)")
    parser.add_argument("--email-port", type=int, default=587, help="Porta SMTP (default: 587)")
    parser.add_argument("--email-from", default="", help="Email mittente")
    parser.add_argument("--email-pass", default="", help="Password email")
    parser.add_argument("--email-to", default="", help="Email destinatario")
    parser.add_argument("--lookup", help="Cerca un singolo numero nella rubrica e online")
    parser.add_argument("--interactive", action="store_true", help="Modalita interattiva: inserisci numeri manualmente")

    args = parser.parse_args()

    if not os.path.isfile(args.file):
        print(f"Errore: file non trovato: {args.file}", file=sys.stderr)
        sys.exit(1)

    # Setup globale
    global proxy_manager, rate_limiter
    proxy_manager = ProxyManager(args.proxy_file)
    rate_limiter = AdaptiveRateLimiter(args.delay)

    # Forza output non bufferizzato (importante per GUI)
    sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, 'reconfigure') else None

    # Cache
    if args.clear_cache and os.path.isfile("phone_search_cache.db"):
        os.remove("phone_search_cache.db")
        print("[OK] Cache cancellata.", flush=True)
    cache = None if args.no_cache else SearchCache(max_age_hours=args.cache_hours)

    # Carica contatti
    print(f"[>] Caricamento rubrica da: {args.file}", flush=True)
    contacts = load_contacts(args.file)
    if not contacts:
        print("Nessun contatto trovato nel file.", file=sys.stderr)
        sys.exit(1)

    # Deduplica
    seen = set()
    unique = []
    for c in contacts:
        if c.phone not in seen:
            seen.add(c.phone)
            unique.append(c)
    contacts = unique
    print(f"[#] Trovati {len(contacts)} numeri unici.", flush=True)

    # Engines
    active_engines = [e.strip() for e in args.engines.split(",")]

    # Modalita lookup singolo numero
    if args.lookup:
        lookup_single_number(args.lookup, contacts, cache=cache, engines=active_engines)
        if cache:
            cache.close()
        return

    # Modalita interattiva
    if args.interactive:
        interactive_lookup(contacts, cache=cache, engines=active_engines)
        if cache:
            cache.close()
        return

    if args.limit > 0:
        contacts = contacts[:args.limit]
        print(f"[i] Limitato a {len(contacts)} contatti.")

    # Resume
    progress_file = PROGRESS_FILE
    completed_progress = {}
    if args.resume:
        completed_progress = load_progress(progress_file, args.file)
        if completed_progress:
            print(f"[~] Ripresa: trovati {len(completed_progress)} numeri gia analizzati.")

    # Engines
    active_engines = [e.strip() for e in args.engines.split(",")]
    print(f"[?] Motori attivi: {', '.join(active_engines)} + Tellows, Chi-mi-chiama, PagineBianche", flush=True)

    # Ricerca in parallelo
    search_results: list[SearchResult] = [None] * len(contacts)
    total = len(contacts)
    print_lock = Lock()
    completed_count = [0]

    def search_contact(index: int, contact: Contact) -> None:
        # Stagger: ritarda l'avvio di ogni thread per non bombardare i motori
        time.sleep(index * 1.5)

        # Skip se gia fatto (resume)
        if contact.phone in completed_progress:
            prev = completed_progress[contact.phone]
            sr = SearchResult(
                contact=contact, query=prev.get("query", ""),
                results=prev.get("results", []),
                error=prev.get("error", ""),
                from_cache=True,
            )
            search_results[index] = sr
            with print_lock:
                completed_count[0] += 1
                print(f"  [{completed_count[0]}/{total}] {contact.name} ({contact.phone}): RIPRESO da progresso", flush=True)
            return

        try:
            with print_lock:
                print(f"  ... analizzando {contact.name} ({contact.phone})", flush=True)
            results = search_phone_number(contact.phone, cache=cache, engines=active_engines)
            valid = [r for r in results if "error" not in r]
            errors = [r for r in results if "error" in r]

            # Categorizza
            cats = {}
            for r in valid:
                cat = r.get("category", "altro")
                cats[cat] = cats.get(cat, 0) + 1

            sr = SearchResult(
                contact=contact, query=f'"{contact.phone}"',
                results=results,
                error=errors[0]["error"] if errors and not valid else "",
                categories=cats,
                from_cache=bool(cache and cache.get(contact.phone) is not None),
            )
            search_results[index] = sr

            # Salva progresso
            completed_progress[contact.phone] = {
                "query": sr.query, "results": results, "error": sr.error,
            }
            with print_lock:
                save_progress(progress_file, completed_progress, args.file)
                completed_count[0] += 1
                if valid:
                    print(f"\n  [{completed_count[0]}/{total}] {contact.name} ({contact.phone}): {len(valid)} risultati", flush=True)
                    for j, r in enumerate(valid[:3], 1):
                        cat_label = CATEGORIES.get(r.get("category", ""), {}).get("label", "Altro")
                        print(f"      [{j}] [{r.get('engine','?')}] [{cat_label}] {r.get('url', 'N/A')}", flush=True)
                    if len(valid) > 3:
                        print(f"      ... e altri {len(valid) - 3} risultati", flush=True)
                else:
                    print(f"\n  [{completed_count[0]}/{total}] {contact.name} ({contact.phone}): nessun risultato", flush=True)

        except Exception as e:
            sr = SearchResult(contact=contact, query=f'"{contact.phone}"', error=str(e))
            search_results[index] = sr
            with print_lock:
                completed_count[0] += 1
                print(f"\n  [{completed_count[0]}/{total}] {contact.name} ({contact.phone}): ERRORE - {e}", flush=True)

    workers = min(args.workers, total)
    print(f"\n[>>] Ricerca in corso con {workers} thread paralleli...\n", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(search_contact, i, c): c for i, c in enumerate(contacts)}
        for future in as_completed(futures):
            future.result()

    # Report
    print_report(search_results)
    save_found_numbers(search_results, args.found)

    if args.html:
        save_html_report(search_results, args.html)

    if args.output:
        save_json_report(search_results, args.output)

    # Monitoraggio
    if args.monitor:
        changes = compare_with_history(MONITOR_FILE, search_results)
        print("\n[i] MONITORAGGIO - Confronto con scansione precedente:")
        for change in changes:
            print(f"  {change}")
        save_history(MONITOR_FILE, search_results)

    # Notifiche
    summary = build_notification_summary(search_results)

    if args.telegram_token and args.telegram_chat:
        send_telegram_notification(args.telegram_token, args.telegram_chat, summary)

    if args.email_smtp and args.email_from and args.email_to:
        send_email_notification(
            args.email_smtp, args.email_port, args.email_from,
            args.email_pass, args.email_to,
            "Phone Search - Report", summary,
        )

    # Pulizia progresso se completato
    if os.path.isfile(progress_file):
        os.remove(progress_file)

    if cache:
        cache.close()

    print("\n[OK] Scansione completata!")


if __name__ == "__main__":
    main()
