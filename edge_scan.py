#!/usr/bin/env python3
"""
edge_scan.py · v1.1 · Pivot: edge por microestructura de mercado.
El modelo actúa como FILTRO, no como fuente. Dos señales:
  A) VAPOR: cuota acortada >= 4% desde el primer snapshot y el modelo confirma.
  B) CONSENSO: mejor precio >= 3.5% sobre la mediana de casas y el modelo
     da EV >= +2% a ese mejor precio.
Registra en 'picks' con fuente 'Vapor (steam)' / 'Consenso (mejor precio)'.
Persiste odds_snapshots para medir drift entre ejecuciones.
v1.1: contadores de diagnóstico como variables locales (fix UnboundLocalError).
"""
import sys
import logging
import statistics
import pandas as pd
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from stats_tracker import StatsTracker
from odds_client import odds_get, get_keys
from auto_scan import (load_models, load_team_database, get_team_stats,
                       compute_recalib, SEED_CALIB_PP, load_auto_tune_config,
                       format_value_bet_alert, send_telegram_message,
                       resolve_team)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

STEAM_MIN_PCT = 4.0       # acortamiento mínimo desde el primer snapshot
CONSENSUS_MIN_PCT = 3.5   # mejor precio mínimo sobre la mediana
MIN_EV_CONSENSO = 2.0     # EV del modelo al mejor precio para señal B
MIN_BOOKS_STEAM = 4
MIN_BOOKS_CONSENSUS = 6
ODD_MIN, ODD_MAX = 1.4, 2.4
SNAP_TABLE = 'odds_snapshots'


def scan_edge():
    logger.info("🚀 Edge scan (vapor/consenso)...")
    if not get_keys():
        logger.error("❌ No hay claves de The Odds API")
        return 1
    tracker = StatsTracker()
    cfg = load_auto_tune_config(tracker)
    safety_mode = cfg['gap'] is not None and cfg['gap'] > 10
    models = load_models()
    if not models:
        return 1
    team_db = load_team_database()
    team_db_lower = {str(t).lower() for t in team_db}
    recalib = compute_recalib(tracker)
    if recalib:
        logger.info(f"🧮 Capa B activa: p_corr = {recalib['alpha']:.2f} + {recalib['beta']:.2f}·p")
    else:
        logger.info("🧮 Capa B inactiva; semilla activa")

    resp = odds_get("sports/soccer/odds", {
        "regions": "eu,us", "markets": "h2h,totals", "oddsFormat": "decimal"},
        tracker=tracker)
    if resp is None or resp.status_code != 200:
        logger.error("❌ Error The Odds API")
        return 1
    fixtures = resp.json()
    logger.info(f"📡 {len(fixtures)} partidos")

    now = datetime.now(timezone.utc)
    now_es = now.astimezone(ZoneInfo("Europe/Madrid"))
    picks = []
    snaps = 0
    skipped_no_db = 0
    skipped_no_prices = 0
    skipped_started = 0
    evaluados = 0
    muestra = []

    for ev in fixtures:
        league = ev.get("sport_title", "Unknown")
        home, away = ev["home_team"], ev["away_team"]
        ct = ev["commence_time"]
        mt = datetime.fromisoformat(ct.replace('Z', '+00:00')) if ct.endswith('Z') \
            else datetime.fromisoformat(ct)
        if mt.astimezone(ZoneInfo("Europe/Madrid")) <= now_es:
            skipped_started += 1
            continue
        if len(muestra) < 10:
            en_db = (home.lower() in team_db_lower and away.lower() in team_db_lower)
            muestra.append(f"{home} vs {away} @ {ct} | enDB={en_db}")
        home_key = resolve_team(home, team_db_lower)
        away_key = resolve_team(away, team_db_lower)
        if home_key is None or away_key is None:
            skipped_no_db += 1
            continue
        evaluados += 1

        # ---- agregar precios por resultado ----
        prices = {}
        for bk in ev.get("bookmakers", []):
            for mk in bk.get("markets", []):
                for oc in mk.get("outcomes", []):
                    odd, name, point = oc["price"], oc["name"], oc.get("point", 2.5)
                    if not (ODD_MIN <= odd <= ODD_MAX):
                        continue
                    key = None
                    if mk["key"] == "h2h":
                        if name == home:
                            key = ("1X2_home", 0)
                        elif name == "Draw":
                            key = ("1X2_draw", 0)
                        elif name == away:
                            key = ("1X2_away", 0)
                    elif mk["key"] == "totals" and name == "Over" and point in (1.5, 2.5, 3.5):
                        key = (f"Over_{point}", point)
                    if key:
                        prices.setdefault(key, []).append(odd)
        if not prices:
            skipped_no_prices += 1
            continue

        # ---- features y probabilidades del modelo ----
        hs = get_team_stats(home_key, team_db)
        as_ = get_team_stats(away_key, team_db)
        features = pd.DataFrame([{
            'Home_Form_Pts': hs['Last_Form_Pts'], 'Away_Form_Pts': as_['Last_Form_Pts'],
            'Form_Diff': hs['Last_Form_Pts'] - as_['Last_Form_Pts'],
            'Home_Goals_Scored': hs['Last_Goals_Scored_Avg'],
            'Away_Goals_Conceded': as_['Last_Goals_Conceded_Avg'],
            'Goal_Threat_Diff': hs['Last_Goals_Scored_Avg'] - as_['Last_Goals_Conceded_Avg'],
            'Combined_Over25_Rate': (hs['Last_Over25_Rate'] + as_['Last_Over25_Rate']) / 2,
            'Combined_BTTS_Rate': (hs['Last_BTTS_Rate'] + as_['Last_BTTS_Rate']) / 2,
        }])
        p1x2 = models['1x2'].predict_proba(features)[0]
        p_ov = {1.5: models['over15'].predict_proba(features)[0][1],
                2.5: models['over25'].predict_proba(features)[0][1],
                3.5: models['over35'].predict_proba(features)[0][1]}

        for (mkey, point), odds_list in prices.items():
            n = len(odds_list)
            best = max(odds_list)
            median = statistics.median(odds_list)

            # ---- snapshot upsert ----
            q = (tracker.client.table(SNAP_TABLE).select("first_best")
                 .eq("event_id", ev["id"]).eq("market_key", mkey).eq("point", point)
                 .execute())
            row = {"event_id": ev["id"], "market_key": mkey, "point": point,
                   "last_ts": now.isoformat(), "last_best": best,
                   "last_median": median, "n_books": n}
            if q.data:
                first_best = float(q.data[0]["first_best"])
                (tracker.client.table(SNAP_TABLE).update(row)
                 .eq("event_id", ev["id"]).eq("market_key", mkey).eq("point", point)
                 .execute())
            else:
                row.update({"first_ts": now.isoformat(),
                            "first_best": best, "first_median": median})
                tracker.client.table(SNAP_TABLE).insert(row).execute()
                first_best = best
            snaps += 1

            drift = (first_best / best - 1) * 100   # >0 = la cuota se acorta
            cons = (best / median - 1) * 100        # >0 = mejor precio sobre consenso

            if mkey == "1X2_home":
                p_raw, mname = p1x2[0], f"1X2 - {home}"
            elif mkey == "1X2_draw":
                p_raw, mname = p1x2[1], "1X2 - Empate"
            elif mkey == "1X2_away":
                p_raw, mname = p1x2[2], f"1X2 - {away}"
            else:
                p_raw, mname = p_ov[point], f"Over {point} Goles"

            if recalib:
                p = max(0.03, min(0.97, recalib['alpha'] + recalib['beta'] * p_raw))
            else:
                p = max(0.03, min(0.97, p_raw + SEED_CALIB_PP.get(mname, 0.0) / 100))

            ev_best = (p * best - 1) * 100
            signal = None
            if drift >= STEAM_MIN_PCT and n >= MIN_BOOKS_STEAM and p * best >= 1.0:
                signal = "Vapor (steam)"
            elif cons >= CONSENSUS_MIN_PCT and n >= MIN_BOOKS_CONSENSUS \
                    and ev_best >= MIN_EV_CONSENSO:
                signal = "Consenso (mejor precio)"
            if not signal:
                continue

            picks.append({
                "Liga": league, "Partido": f"{home} vs {away}",
                "Hora": mt.astimezone(ZoneInfo("Europe/Madrid")).strftime('%d/%m %H:%M'),
                "Mercado": mname, "Cuota": best, "Prob. IA": p,
                "Prob. Casa": 1 / best, "EV (%)": ev_best, "Fuente": signal,
                "Features": {**features.iloc[0].to_dict(),
                             "median_odd": median, "n_books": n,
                             "drift_pct": round(drift, 2),
                             "consensus_edge_pct": round(cons, 2)},
            })

    logger.info(f"⏭️ {skipped_started} partidos ya comenzados (descartados)")
    for m in muestra:
        logger.info(f"   🔎 {m}")
    logger.info(f"⏭️ {skipped_no_db} partidos omitidos (fuera de team_stats_db)")
    logger.info(f"⏭️ {skipped_no_prices} partidos sin cuotas en ventana {ODD_MIN}-{ODD_MAX}")
    logger.info(f"✅ {evaluados} partidos evaluables procesados")
    logger.info(f"📸 {snaps} snapshots escritos/actualizados")
    logger.info(f"🎯 {len(picks)} picks de edge detectados")
    if picks:
        if safety_mode:
            send_telegram_message(
                "🚫 <b>MODO SEGURIDAD ACTIVO</b>\nPicks de edge solo para "
                "registro y validación: NO apostar dinero real todavía.")
        for vb in sorted(picks, key=lambda x: -x['EV (%)'])[:10]:
            send_telegram_message(format_value_bet_alert(vb, cfg['kelly']))
        reg = sum(1 for vb in picks if tracker.register_pick(vb))
        logger.info(f"💾 {reg} picks de edge registrados")
    else:
        send_telegram_message("💤 <b>Edge scan</b>\nSin señales de vapor/consenso en esta pasada.")
    return 0


if __name__ == "__main__":
    sys.exit(scan_edge())
