#!/usr/bin/env python3
"""
Fase 1 · Construcción/ampliación de team_stats_db.csv desde football-data.co.uk

- Descarga CSVs públicos de football-data.co.uk.
- Usa temporada actual + temporada anterior.
- Calcula las 5 métricas que consume el modelo actual:
    Team
    Last_Form_Pts
    Last_Goals_Scored_Avg
    Last_Goals_Conceded_Avg
    Last_Over25_Rate
    Last_BTTS_Rate

- Fusiona con team_stats_db.csv existente:
    * Si un equipo ya existe, actualiza sus métricas.
    * Si no existe, lo añade.
    * Si un equipo antiguo no aparece en football-data, lo conserva.
"""

import io
import os
import re
import unicodedata
import logging
from datetime import datetime

import requests
import pandas as pd


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("build_team_db")


OUTPUT_FILE = "team_stats_db.csv"
WINDOW = 5  # Importante: mantiene escala compatible con Last_Form_Pts actual, default 7 sobre últimos 5 partidos

# Ligas football-data.co.uk
# https://www.football-data.co.uk/data.php
LEAGUE_CODES = {
    # Inglaterra
    "E0": "England Premier League",
    "E1": "England Championship",
    "E2": "England League One",
    "E3": "England League Two",

    # España
    "SP1": "Spain La Liga",
    "SP2": "Spain Segunda",

    # Italia
    "I1": "Italy Serie A",
    "I2": "Italy Serie B",

    # Alemania
    "D1": "Germany Bundesliga",
    "D2": "Germany Bundesliga 2",

    # Francia
    "F1": "France Ligue 1",
    "F2": "France Ligue 2",

    # Países Bajos
    "N1": "Netherlands Eredivisie",

    # Portugal
    "P1": "Portugal Primeira Liga",

    # Bélgica
    "B1": "Belgium First Division A",

    # Turquía
    "T1": "Turkey Super Lig",

    # Grecia
    "G1": "Greece Super League",

    # Escocia
    "SC0": "Scotland Premiership",
    "SC1": "Scotland Championship",
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
    """
    football-data usa formato tipo:
    2526 = temporada 2025/26
    2627 = temporada 2026/27
    """
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
    """
    Normalización básica para detectar duplicados.
    No se usa directamente por auto_scan; solo para fusionar filas.
    """
    if not name:
        return ""

    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().strip()

    # Quitar puntuación común
    s = re.sub(r"[^a-z0-9 ]+", " ", s)

    # Quitar sufijos frecuentes
    stop = {
        "fc", "cf", "afc", "sc", "cd", "sd", "ud", "ac", "as",
        "calcio", "club", "football", "futbol", "de", "the"
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
            logger.warning(f"⚠️ Columnas incompletas en {season}/{code}: {list(df.columns)[:10]}")
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
    # football-data suele usar dd/mm/yy o dd/mm/yyyy
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
    """
    Fusiona por nombre normalizado.
    - Si existe en ambos: mantiene el nombre antiguo pero actualiza métricas.
    - Si solo existe en antiguo: conserva.
    - Si solo existe en nuevo: añade.
    """
    existing = existing.copy()
    new = new.copy()

    existing["_norm"] = existing["Team"].apply(norm_team)
    new["_norm"] = new["Team"].apply(norm_team)

    new_by_norm = {row["_norm"]: row for _, row in new.iterrows() if row["_norm"]}

    merged_rows = []
    seen_norms = set()

    # Actualizar existentes si hay equivalente nuevo
    for _, old in existing.iterrows():
        n = old["_norm"]
        if n in new_by_norm:
            nr = new_by_norm[n]
            merged_rows.append({
                "Team": old["Team"],  # mantener naming que ya funcionaba en tu bot
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

    # Añadir nuevos
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
    seasons = current_and_previous_seasons()
    logger.info(f"Temporadas usadas: {seasons}")

    dfs = []
    for season in seasons:
        for code, league_name in LEAGUE_CODES.items():
            df = download_league_csv(season, code)
            if df is not None:
                logger.info(f"✅ {season}/{code} {league_name}: {len(df)} partidos")
                dfs.append(df)

    if not dfs:
        raise RuntimeError("No se descargó ningún CSV de football-data.co.uk")

    results = pd.concat(dfs, ignore_index=True)
    results = parse_dates(results)

    logger.info(f"⚽ Partidos válidos descargados: {len(results)}")

    match_rows = build_match_rows(results)
    new_stats = compute_team_stats(match_rows)

    logger.info(f"🧮 Equipos calculados desde football-data: {len(new_stats)}")

    existing = load_existing_db()
    merged = merge_existing_with_new(existing, new_stats)

    merged.to_csv(OUTPUT_FILE, index=False)
    logger.info(f"✅ Guardado {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
