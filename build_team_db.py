#!/usr/bin/env python3
"""
Fase 1 + Fase 2 · Construcción/ampliación de team_stats_db.csv
- football-data.co.uk (Europa)
- ESPN (Sudamérica, MLS, Liga MX)

Calcula las 5 métricas que consume el modelo:
    Team, Last_Form_Pts, Last_Goals_Scored_Avg, Last_Goals_Conceded_Avg,
    Last_Over25_Rate, Last_BTTS_Rate

Fusiona con team_stats_db.csv existente (actualiza + añade + conserva).
"""

import io
import os
import re
import time
import unicodedata
import logging
from datetime import datetime, timedelta

import requests
import pandas as pd


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("build_team_db")


OUTPUT_FILE = "team_stats_db.csv"
WINDOW = 5

# Ligas football-data.co.uk
LEAGUE_CODES = {
    "E0": "England Premier League",
    "E1": "England Championship",
    "E2": "England League One",
    "E3": "England League Two",
    "SP1": "Spain La Liga",
    "SP2": "Spain Segunda",
    "I1": "Italy Serie A",
    "I2": "Italy Serie B",
    "D1": "Germany Bundesliga",
    "D2": "Germany Bundesliga 2",
    "F1": "France Ligue 1",
    "F2": "France Ligue 2",
    "N1": "Netherlands Eredivisie",
    "P1": "Portugal Primeira Liga",
    "B1": "Belgium First Division A",
    "T1": "Turkey Super Lig",
    "G1": "Greece Super League",
    "SC0": "Scotland Premiership",
    "SC1": "Scotland Championship",
}

# Ligas ESPN
ESPN_LEAGUES = {
    "bra.1": "Brazil Serie A",
    "bra.2": "Brazil Serie B",
    "arg.1": "Argentina Liga Profesional",
    "chi.1": "Chile Primera Division",
    "col.1": "Colombia Primera A",
    "ecu.1": "Ecuador Serie A",
    "uru.1": "Uruguay Primera Division",
    "mex.1": "Mexico Liga MX",
    "usa.1": "USA MLS",
}

REQUIRED_COLS = [
    "Team",
    "Last_Form_Pts",
    "Last_Goals_Scored_Avg",
    "Last_Goals_Conceded_Avg",
    "Last_Over25_Rate",
    "Last_BTTS_Rate",
]


def current_and_previous_seasons():
    now = datetime.utcnow()
    year = now.year
    month = now.month

    if month >= 7:
        start = year % 100
        end = (year + 1) % 100
    else:
        start = (year - 1) % 100
        end = year % 100

    current = f"{start:02d}{end:02d}"
    previous = f"{(start - 1) % 100:02d}{start:02d}"

    return [current, previous]


def norm_team(name):
    if not name:
        return ""

    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().strip()

    s = re.sub(r"[^a-z0-9 ]+", " ", s)

    stop = {
        "fc", "cf", "afc", "sc", "cd", "sd", "ud", "ac", "as",
        "calcio", "club", "football", "futbol", "de", "the",
        "atletico", "atletico", "deportivo", "real", "santos"
    }
    parts = [p for p in s.split() if p not in stop]
    return " ".join(parts).strip()


def download_league_csv(season, code):
    url = f"https://www.football-data.co.uk/mmz4281/{season}/{code}.csv"
    try:
        r = requests.get(url, timeout=20)
        if r.status_code != 200 or not r.text.strip():
            logger.warning(f"⚠️ No disponible: {season}/{code} HTTP {r.status_code}")
            return None

        df = pd.read_csv(io.StringIO(r.text))
        if df.empty:
            logger.warning(f"⚠️ CSV vacío: {season}/{code}")
            return None

        required = {"Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"}
        if not required.issubset(df.columns):
            logger.warning(f"⚠️ Columnas incompletas en {season}/{code}")
            return None

        df = df[["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"]].copy()
        df["Season"] = season
        df["LeagueCode"] = code
        df["LeagueName"] = LEAGUE_CODES.get(code, code)

        return df

    except Exception as e:
        logger.warning(f"⚠️ Error descargando {season}/{code}: {e}")
        return None


def parse_dates(df):
    df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce")
    df = df.dropna(subset=["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"])
    df["FTHG"] = pd.to_numeric(df["FTHG"], errors="coerce")
    df["FTAG"] = pd.to_numeric(df["FTAG"], errors="coerce")
    df = df.dropna(subset=["FTHG", "FTAG"])
    df["FTHG"] = df["FTHG"].astype(int)
    df["FTAG"] = df["FTAG"].astype(int)
    return df


def build_match_rows(results_df):
    rows = []

    for _, r in results_df.iterrows():
        date = r["Date"]
        home = r["HomeTeam"]
        away = r["AwayTeam"]
        hg = int(r["FTHG"])
        ag = int(r["FTAG"])

        total_goals = hg + ag
        over25 = 1 if total_goals > 2.5 else 0
        btts = 1 if hg > 0 and ag > 0 else 0

        if hg > ag:
            home_pts, away_pts = 3, 0
        elif hg < ag:
            home_pts, away_pts = 0, 3
        else:
            home_pts, away_pts = 1, 1

        rows.append({
            "Date": date,
            "Team": home,
            "GF": hg,
            "GA": ag,
            "Pts": home_pts,
            "Over25": over25,
            "BTTS": btts,
        })

        rows.append({
            "Date": date,
            "Team": away,
            "GF": ag,
            "GA": hg,
            "Pts": away_pts,
            "Over25": over25,
            "BTTS": btts,
        })

    return pd.DataFrame(rows)


def compute_team_stats(match_rows):
    output = []

    for team, g in match_rows.groupby("Team"):
        g = g.sort_values("Date").tail(WINDOW)

        if len(g) == 0:
            continue

        output.append({
            "Team": team,
            "Last_Form_Pts": float(g["Pts"].sum()),
            "Last_Goals_Scored_Avg": float(g["GF"].mean()),
            "Last_Goals_Conceded_Avg": float(g["GA"].mean()),
            "Last_Over25_Rate": float(g["Over25"].mean()),
            "Last_BTTS_Rate": float(g["BTTS"].mean()),
        })

    df = pd.DataFrame(output)
    return df[REQUIRED_COLS].sort_values("Team").reset_index(drop=True)


# ========================================
# FASE 2: ESPN
# ========================================

def download_espn_data():
    """Fase 2: resultados reales vía scoreboards públicos de ESPN
    (el mismo endpoint que usa la liquidación automática).
    Recorre los últimos 45 días por liga y recoge partidos completados."""
    ESPN_DAYS_BACK = 45
    all_rows = []
    today = datetime.utcnow().date()

    for league_code, league_name in ESPN_LEAGUES.items():
        league_count = 0
        for days_back in range(ESPN_DAYS_BACK, -1, -1):
            d = today - timedelta(days=days_back)
            url = (f"https://site.api.espn.com/apis/site/v2/sports/soccer/"
                   f"{league_code}/scoreboard?dates={d.strftime('%Y%m%d')}")
            try:
                r = requests.get(url, timeout=15)
                if r.status_code != 200:
                    continue
                data = r.json()
            except Exception:
                continue

            for event in data.get("events", []):
                status = event.get("status", {}).get("type", {})
                if not status.get("completed"):
                    continue
                comps = event.get("competitions", [])
                if not comps:
                    continue
                home = away = None
                hs = as_ = None
                for comp in comps[0].get("competitors", []):
                    team_data = comp.get("team") or {}
                    name = team_data.get("displayName") or team_data.get("name")
                    try:
                        score = int(comp.get("score"))
                    except Exception:
                        score = None
                    if comp.get("homeAway") == "home":
                        home, hs = name, score
                    else:
                        away, as_ = name, score
                if not home or not away or hs is None or as_ is None:
                    continue

                over25 = 1 if (hs + as_) >= 3 else 0
                btts = 1 if hs > 0 and as_ > 0 else 0
                for team, gf, ga in ((home, hs, as_), (away, as_, hs)):
                    all_rows.append({
                        "Date": datetime(d.year, d.month, d.day),
                        "Team": team,
                        "GF": gf,
                        "GA": ga,
                        "Pts": 3 if gf > ga else (1 if gf == ga else 0),
                        "Over25": over25,
                        "BTTS": btts,
                    })
                league_count += 1
            time.sleep(0.2)  # cortesía con ESPN

        logger.info(f"✅ ESPN {league_name}: {league_count} partidos completados")

    if not all_rows:
        logger.warning("⚠️ No se descargaron datos de ESPN")
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    logger.info(f"⚽ ESPN: {len(df)} filas equipo-partido descargadas")
    return df


def load_existing_db():
    if not os.path.exists(OUTPUT_FILE):
        logger.info("No existe team_stats_db.csv previo. Se creará uno nuevo.")
        return pd.DataFrame(columns=REQUIRED_COLS)

    df = pd.read_csv(OUTPUT_FILE)

    for col in REQUIRED_COLS:
        if col not in df.columns:
            if col == "Team":
                df[col] = ""
            else:
                df[col] = None

    df = df[REQUIRED_COLS].copy()
    df = df.dropna(subset=["Team"])
    df = df[df["Team"].astype(str).str.strip() != ""]
    logger.info(f"📁 DB existente: {len(df)} equipos")
    return df


def merge_existing_with_new(existing, new):
    existing = existing.copy()
    new = new.copy()

    existing["_norm"] = existing["Team"].apply(norm_team)
    new["_norm"] = new["Team"].apply(norm_team)

    new_by_norm = {row["_norm"]: row for _, row in new.iterrows() if row["_norm"]}

    merged_rows = []
    seen_norms = set()

    for _, old in existing.iterrows():
        n = old["_norm"]
        if n in new_by_norm:
            nr = new_by_norm[n]
            merged_rows.append({
                "Team": old["Team"],
                "Last_Form_Pts": nr["Last_Form_Pts"],
                "Last_Goals_Scored_Avg": nr["Last_Goals_Scored_Avg"],
                "Last_Goals_Conceded_Avg": nr["Last_Goals_Conceded_Avg"],
                "Last_Over25_Rate": nr["Last_Over25_Rate"],
                "Last_BTTS_Rate": nr["Last_BTTS_Rate"],
            })
            seen_norms.add(n)
        else:
            merged_rows.append({
                "Team": old["Team"],
                "Last_Form_Pts": old["Last_Form_Pts"],
                "Last_Goals_Scored_Avg": old["Last_Goals_Scored_Avg"],
                "Last_Goals_Conceded_Avg": old["Last_Goals_Conceded_Avg"],
                "Last_Over25_Rate": old["Last_Over25_Rate"],
                "Last_BTTS_Rate": old["Last_BTTS_Rate"],
            })
            seen_norms.add(n)

    added = 0
    for _, nr in new.iterrows():
        n = nr["_norm"]
        if not n or n in seen_norms:
            continue

        merged_rows.append({
            "Team": nr["Team"],
            "Last_Form_Pts": nr["Last_Form_Pts"],
            "Last_Goals_Scored_Avg": nr["Last_Goals_Scored_Avg"],
            "Last_Goals_Conceded_Avg": nr["Last_Goals_Conceded_Avg"],
            "Last_Over25_Rate": nr["Last_Over25_Rate"],
            "Last_BTTS_Rate": nr["Last_BTTS_Rate"],
        })
        seen_norms.add(n)
        added += 1

    merged = pd.DataFrame(merged_rows)
    merged = merged[REQUIRED_COLS].sort_values("Team").reset_index(drop=True)

    logger.info(f"➕ Equipos nuevos añadidos: {added}")
    logger.info(f"📊 Total final team_stats_db.csv: {len(merged)} equipos")

    return merged


def main():
    # FASE 1: football-data
    seasons = current_and_previous_seasons()
    logger.info(f"🌍 FASE 1: football-data.co.uk (temporadas {seasons})")

    dfs = []
    for season in seasons:
        for code, league_name in LEAGUE_CODES.items():
            df = download_league_csv(season, code)
            if df is not None:
                logger.info(f"✅ {season}/{code} {league_name}: {len(df)} partidos")
                dfs.append(df)

    if dfs:
        results = pd.concat(dfs, ignore_index=True)
        results = parse_dates(results)
        logger.info(f"⚽ football-data: {len(results)} partidos válidos")
        fd_match_rows = build_match_rows(results)
        fd_stats = compute_team_stats(fd_match_rows)
        logger.info(f"🧮 football-data: {len(fd_stats)} equipos calculados")
    else:
        logger.warning("⚠️ No se descargó ningún CSV de football-data")
        fd_stats = pd.DataFrame(columns=REQUIRED_COLS)

    # FASE 2: ESPN
    logger.info(f"🌎 FASE 2: ESPN (Sudamérica + MLS + Liga MX)")
    espn_match_rows = download_espn_data()
    
    if not espn_match_rows.empty:
        espn_stats = compute_team_stats(espn_match_rows)
        logger.info(f"🧮 ESPN: {len(espn_stats)} equipos calculados")
        
        # Combinar stats de ambas fuentes
        combined_stats = pd.concat([fd_stats, espn_stats], ignore_index=True)
        combined_stats = combined_stats.drop_duplicates(subset=["Team"], keep="last")
        logger.info(f"🧮 Combinado: {len(combined_stats)} equipos únicos")
    else:
        combined_stats = fd_stats

    # Fusionar con DB existente
    existing = load_existing_db()
    merged = merge_existing_with_new(existing, combined_stats)

    merged.to_csv(OUTPUT_FILE, index=False)
    logger.info(f"✅ Guardado {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
