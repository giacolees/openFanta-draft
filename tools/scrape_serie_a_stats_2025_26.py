#!/usr/bin/env python3.10
"""
Scraping statistiche ufficiali Serie A — Stagione 2025/2026.

Interroga l'API pubblica usata dal sito ufficiale Lega Serie A
(https://en.legaseriea.it/serie-a/statistiche/giocatori -> api-sdp.legaseriea.it).
Nessuna autenticazione richiesta; robots.txt consente la navigazione (/).
Per rispetto del servizio: poche richieste (~20 totali), delay tra le chiamate,
retry con backoff, e cache locale dei JSON grezzi (--force per riscaricare).

Categorie statistiche scaricate (per giocatore):
- General    (come la tabella "Generale" del sito)
- Attacking  (produzione offensiva)
- Defending  (lavoro difensivo)
- Passing    (costruzione del gioco)

Output:
- data/serie_a_stats_2025_26/raw_<category>.json      -> risposte grezze dell'API (tutte le metriche)
- data/serie_a_stats_2025_26/giocatori_serie_a_2025_26.csv -> tabella unica, una riga per giocatore,
  colonne italiane per le metriche principali, statsId originale per il resto.

Uso:  python3 scripts/scrape_serie_a_stats_2025_26.py [--force] [--out-dir DIR]
"""

import csv
import importlib
import json
import os
import random
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_BASE = "https://api-sdp.legaseriea.it/v1/serie-a/football"
COMPETITION_ID = "serie-a::Football_Competition::ec93b94f74294dc98ab5bcfd67fc0d88"

# Scelti da CLI con --season (default 2025/2026)
TARGET_SEASON_NAME = "2025/2026"
OUT_DIR = os.path.join(BASE_DIR, "data", "serie_a_stats_2025_26")
OUT_CSV_NAME = "giocatori_serie_a_2025_26.csv"

CATEGORIES = ["General", "Attacking", "Defending", "Passing"]
PAGE_SIZE = 25  # valore usato dal sito; page > 4 con pageNumElement=100 da' errore 503
REQUEST_DELAY_SEC = 0.3  # politica di cortesia
MAX_ATTEMPTS = 5
TIMEOUT_SEC = 30

# Nome colonna italiano per le metriche principali (le altre mantengono lo statsId originale)
STAT_RENAME = {
    "games-played": "Pres",
    "minutes-played": "Min",
    "goals": "Gol",
    "assists": "Assist",
    "total-scoring-attempts": "Tiri",
    "on-target-scoring-attempts": "TiriPorta",
    "total-attacking-assist": "PassChiave",
    "tackles-won": "TackleVinti",
    "fouls-committed": "FalliFatti",
    "fouls-suffered": "FalliSubiti",
    "total-offside": "Fuorigioco",
    "yellow-cards": "Ammonizioni",
    "red-cards": "Espulsioni",
    "accurate-pass-percentage": "PassPrec",
    "duels-won-perc": "DuelliVintiPerc",
    "aerial-duels-won-perc": "AereiVintiPerc",
    "crosses-complete-percentage": "CrossPrec",
    "goals-conceded": "GolSubiti",
    "goals-against-average": "MediaGolSubiti",
    "shotsSaved": "Parate",
    "shotsOnGoalConceded": "TiriPortaSubiti",
    "Xg": "xG",
    "expectedGoals": "xG_Alternativo",
    "XGEfficiency": "xGEfficienza",
    "penalties-successful": "RigoriSegnati",
    "penalty-attempts": "RigoriTentati",
    "hit-woodwork": "Legni",
}

HEADERS = {
    "User-Agent": "openFanta-draft-stats-scraper/1.0 (progetto fantacalcio personale)",
    "Accept": "text/plain; x-api-version=1.0",
    "Referer": "https://en.legaseriea.it/",
}


def _init_tls_contexts():
    """Contesti TLS ordinati per affidabilita': default -> certifi -> senza verifica."""
    contexts = [ssl.create_default_context()]
    try:
        certifi = importlib.import_module("certifi")
        contexts.append(ssl.create_default_context(cafile=certifi.where()))
    except (ImportError, OSError, ssl.SSLError):
        pass
    fallback = ssl.create_default_context()
    fallback.check_hostname = False
    fallback.verify_mode = ssl.CERT_NONE
    contexts.append(fallback)
    return contexts


_TLS_CONTEXTS = _init_tls_contexts()
_TLS_LEVEL = (
    0  # 0 = verifica completa, avanzamento automatico se il server rifiuta la catena
)


def _is_ssl_failure(err):
    """True se l'errore e' (o avvolge) un problema di verifica TLS."""
    if err is None:
        return False
    return isinstance(err, ssl.SSLError) or (
        isinstance(err, urllib.error.URLError)
        and isinstance(getattr(err, "reason", None), ssl.SSLError)
    )


def http_get(url, attempts=MAX_ATTEMPTS):
    """GET con retry + backoff esponenziale. Ritorna il body come testo."""
    global _TLS_LEVEL
    last_err = None
    max_level = len(_TLS_CONTEXTS) - 1
    for attempt in range(1, attempts + 1):
        level = min(_TLS_LEVEL, max_level)
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(
                req, timeout=TIMEOUT_SEC, context=_TLS_CONTEXTS[level]
            ) as resp:
                return resp.read().decode("utf-8")
        except OSError as err:
            last_err = err
        if _is_ssl_failure(last_err) and max_level > _TLS_LEVEL:
            _TLS_LEVEL += 1
            if _TLS_CONTEXTS[_TLS_LEVEL].verify_mode == ssl.CERT_NONE:
                print(
                    "AVVISO: verifica TLS disattivata per API Lega Serie A (certificati locali mancanti)."
                )
            continue  # riprova subito con il livello successivo
        if attempt < attempts:
            # backoff esponenziale + jitter: gli 503 del server sono transitori
            time.sleep(2**attempt + random.uniform(0, 1))
    raise RuntimeError(f"GET {url} fallito dopo {attempts} tentativi: {last_err}")


def fetch_json(url):
    raw = http_get(url)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as err:
        raise RuntimeError(f"Risposta non-JSON da {url}: {err}") from err


def resolve_season_id():
    """Trova lo seasonId della stagione richiesta dalla lista stagioni dell'API."""
    url = f"{API_BASE}/competitions/{urllib.parse.quote(COMPETITION_ID, safe='')}/seasons?locale=en-GB"
    data = fetch_json(url)
    seasons = data if isinstance(data, list) else data.get("seasons", [])
    for s in seasons:
        if s.get("seasonName") == TARGET_SEASON_NAME:
            print(f"  stagione {TARGET_SEASON_NAME} -> {s['seasonId']}")
            return s["seasonId"]
    raise RuntimeError(
        f"Stagione {TARGET_SEASON_NAME} non trovata nelle {len(seasons)} stagioni restituite"
    )


def download_category(season_id, category, force):
    """Scarica tutti i giocatori di una categoria (paginato). Ritorna la lista di giocatori."""
    raw_path = os.path.join(OUT_DIR, f"raw_{category.lower()}.json")
    if not force and os.path.exists(raw_path):
        try:
            with open(raw_path, encoding="utf-8") as fh:
                cached = json.load(fh)
            print(f"  [{category}] da cache: {raw_path}")
            return cached["players"]
        except (OSError, KeyError, json.JSONDecodeError) as err:
            print(f"  [{category}] cache non valida ({err}), riscarico...")

    base = f"{API_BASE}/seasons/{urllib.parse.quote(season_id, safe='')}/stats/players"
    players = []
    page = 1
    while True:
        params = {
            "category": category,
            "pageNumElement": PAGE_SIZE,
            "page": page,  # parametro di paginazione reale dell'API
            "locale": "en-GB",
        }
        url = base + "?" + urllib.parse.urlencode(params)
        data = fetch_json(url)
        batch = data.get("players", [])
        players.extend(batch)
        pagination = data.get("pagination", {})
        total_pages = pagination.get("totalPages", 1)
        print(
            f"  [{category}] pagina {page}/{total_pages}: {len(batch)} giocatori (tot {len(players)})"
        )
        if page >= total_pages or not batch:
            break
        page += 1
        time.sleep(REQUEST_DELAY_SEC)

    # Rete di sicurezza: al netto di eventuali record duplicati dal server
    seen_ids = set()
    unique = []
    for p in players:
        if p["playerId"] not in seen_ids:
            seen_ids.add(p["playerId"])
            unique.append(p)
    if len(unique) != len(players):
        print(f"  [{category}] rimossi {len(players) - len(unique)} duplicati")
    players = unique

    try:
        os.makedirs(OUT_DIR, exist_ok=True)
    except OSError as err:
        raise RuntimeError(f"Impossibile creare {OUT_DIR}: {err}") from err
    try:
        with open(raw_path, "w", encoding="utf-8") as fh:
            json.dump(
                {"seasonId": season_id, "category": category, "players": players},
                fh,
                ensure_ascii=False,
            )
    except OSError as err:
        raise RuntimeError(f"Impossibile scrivere {raw_path}: {err}") from err
    print(f"  [{category}] salvato {raw_path}")
    return players


def flatten_player(category_players):
    """
    Unisce i record delle 4 categorie per playerId.
    Ritorna lista di dict: identita' + {statsId: value} (primo valore non-null vince).
    """
    merged = {}
    for category in CATEGORIES:
        for p in category_players[category]:
            pid = p["playerId"]
            row = merged.setdefault(pid, {"_rank": {}})
            if "identity" not in row:
                row["identity"] = p
            row["_rank"][category] = p.get("rankLabel")
            stats = row.setdefault("stats", {})
            for s in p.get("stats", []):
                sid = s.get("statsId")
                if sid and stats.get(sid) is None:
                    stats[sid] = s.get("statsValue")

    out = []
    for pid, row in merged.items():
        p = row["identity"]
        flat = {
            "Id": pid,
            "ProviderId": p.get("providerId"),
            "Nome": p.get("displayName"),
            "NomeCompleto": f"{p.get('mediaFirstName', '')} {p.get('mediaLastName', '')}".strip(),
            "NomeMaglia": p.get("shirtName"),
            "NumMaglia": p.get("bibNumber"),
            "Ruolo": p.get("roleLabel"),
            "Nazionalita": p.get("nationality"),
            "Squadra": (p.get("team") or {}).get("mediaName"),
            "SquadraId": (p.get("team") or {}).get("teamId"),
        }
        for cat in CATEGORIES:
            rk = row["_rank"].get(cat)
            if rk is not None:
                flat[f"Pos_{cat}"] = rk
        for sid, val in row["stats"].items():
            col = STAT_RENAME.get(sid, sid) if sid is not None else None
            if col is not None:
                flat[col] = val
        out.append(flat)
    return out


def write_csv(players):
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
    except OSError as err:
        raise RuntimeError(f"Impossibile creare {OUT_DIR}: {err}") from err
    # Unione delle chiavi di TUTTI i giocatori: giocatori diversi hanno metriche diverse
    columns = list(dict.fromkeys(key for p in players for key in p))
    path = os.path.join(OUT_DIR, OUT_CSV_NAME)
    try:
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns)
            writer.writeheader()
            writer.writerows(players)
    except OSError as err:
        raise RuntimeError(f"Impossibile scrivere {path}: {err}") from err
    print(f"\nCSV: {path}  ({len(players)} giocatori, {len(columns)} colonne)")


def main():
    global OUT_DIR, TARGET_SEASON_NAME, OUT_CSV_NAME
    force = "--force" in sys.argv
    season_name = "2025/2026"
    if "--season" in sys.argv:
        season_name = sys.argv[sys.argv.index("--season") + 1]
    TARGET_SEASON_NAME = season_name
    y1, y2 = season_name.split("/")
    OUT_CSV_NAME = f"giocatori_serie_a_{y1}_{y2[-2:]}.csv"
    if "--out-dir" in sys.argv:
        OUT_DIR = sys.argv[sys.argv.index("--out-dir") + 1]
    else:
        OUT_DIR = os.path.join(BASE_DIR, "data", f"serie_a_stats_{y1}_{y2[-2:]}")

    try:
        os.makedirs(OUT_DIR, exist_ok=True)
    except OSError as err:
        raise RuntimeError(f"Impossibile creare {OUT_DIR}: {err}") from err
    print(f"Scraping statistiche Serie A {TARGET_SEASON_NAME} -> {OUT_DIR}")

    season_id = resolve_season_id()

    category_players = {}
    for category in CATEGORIES:
        category_players[category] = download_category(season_id, category, force)
        time.sleep(REQUEST_DELAY_SEC)

    players = flatten_player(category_players)
    players.sort(
        key=lambda r: (r.get("Pos_General") is None, r.get("Pos_General") or 0)
    )
    write_csv(players)

    n_sources = {p.get("Squadra") for p in players if p.get("Squadra")}
    print(f"Done: {len(players)} giocatori, {len(n_sources)} squadre.")


if __name__ == "__main__":
    main()
