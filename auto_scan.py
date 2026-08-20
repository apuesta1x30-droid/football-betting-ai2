#!/usr/bin/env python3
"""
v0.5-B · Escaneo automático de value bets.
- Modelo XGBoost para probabilidades.
- The Odds API con rotación de 3 claves y contador mensual.
- Lista negra empírica de ligas (n≥8, PnL≤-5 u).
- Modo seguridad: si gap > +10, sin alertas de apuesta (solo registro).
- Liquidación manual vía reacciones 👌/👎 en Telegram.
"""
import os
import sys
import json
import time
import logging
import pickle
import hashlib
import pandas as pd
import numpy as np
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from xgboost import XGBClassifier

from stats_tracker import StatsTracker
from odds_client import odds_get, get_keys
from features import TEAM_DB, build_features_for_match
from reports import compute_for

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

MARKETS = [
    "Match Winner",
    "Over 0.5", "Over 1.5", "Over 2.5", "Over 3.5",
    "Under 0.5", "Under 1.5", "Under 2.5", "Under 3.5",
    "Over 0.5 1st Half", "Over 1.5 1st Half",
    "Under 0.5 1st Half", "Under 1.5 1st Half",
    "Both Teams to Score",
    "Double Chance",
]

MODEL_FILE = "models/xgb_model.pkl"
FEATURE_COLS = [
    "implied_prob", "team_strength_home", "team_strength_away",
    "is_home", "market_type", "point_value",
]


def load_models():
    """Carga el modelo XGBoost entrenado."""
    if not os.path.exists(MODEL_FILE):
        logger.error(f"❌ Modelo no encontrado: {MODEL_FILE}")
        return None
    with open(MODEL_FILE, "rb") as f:
        return pickle.load(f)


def predict_proba(model, X):
    """Devuelve probabilidades del modelo XGBoost."""
    if model is None:
        return np.zeros(len(X))
    return model.predict_proba(X)[:, 1]


def load_auto_tune_config(tracker):
    """Lee la configuración dinámica (gap, n, ev_notify, kelly) desde Supabase."""
    cfg = {"ev_notify": 10.0, "kelly": 4, "gap": None, "n": 0}
    try:
        resp = tracker.client.table('meta').select('value').eq('key', 'auto_tune').execute()
        if resp.data:
            data = json.loads(resp.data[0]['value'])
            inner = data.get('cfg', {})
            cfg['ev_notify'] = float(inner.get('ev_notify', 10.0))
            cfg['kelly'] = int(inner.get('kelly', 4))
            cfg['gap'] = data.get('gap')
            cfg['n'] = data.get('n', 0)
    except Exception as e:
        logger.debug(f"No hay config auto_tune: {e}")
    return cfg


def league_blacklist(tracker, min_n=8, min_pnl=-5.0):
    """Ligas con evidencia negativa suficiente (n≥min_n y PnL≤min_pnl)."""
    if not tracker or not tracker.enabled:
        return set()
    agg = {}
    for p in tracker.get_all_picks():
        if p.get('status') not in ('won', 'lost'):
            continue
        lg = p.get('liga') or '?'
        a = agg.setdefault(lg, {'n': 0, 'pnl': 0.0})
        a['n'] += 1
        if p['status'] == 'won':
            a['pnl'] += (p.get('cuota') or 2.0) - 1.0
        else:
            a['pnl'] -= 1.0
    return {lg for lg, a in agg.items() if a['n'] >= min_n and a['pnl'] <= min_pnl}


def format_summary_message(stats, value_bets, cfg, n_blacklist=0):
    """Formatea el resumen del escaneo con la configuración activa."""
    config_line = f"🤖 Config activa: EV≥{cfg['ev_notify']:.0f}% · Kelly 1/{cfg['kelly']}"
    if cfg['gap'] is not None:
        config_line += f" (gap {cfg['gap']:+.1f} pp)"
    if n_blacklist:
        config_line += f" · 🚫 {n_blacklist} ligas excluidas"

    return (
        f"📊 <b>RESUMEN DEL ESCANEO</b>\n\n"
        f"🔎 Partidos analizados: <b>{stats['total']}</b>\n"
        f"✅ Value Bets detectadas: <b>{len(value_bets)}</b>\n\n"
        f"{config_line}\n\n"
        f"📡 Datos de The Odds API\n"
        f"🤖 Probabilidades: Modelo XGBoost"
    )


def format_value_bet_alert(vb, kelly_fraction):
    """Formatea una alerta de value bet individual."""
    ev = vb['EV (%)']
    prob_ia = vb['Prob. IA']
    cuota = vb['Cuota']
    
    # Kelly simple
    kelly_pct = max(0, (prob_ia * cuota - 1) / (cuota - 1)) * 100
    stake = kelly_pct / kelly_fraction
    
    return (
        f"🎯 <b>VALUE BET DETECTADA</b>\n\n"
        f"🏆 <b>{vb['Liga']}</b>\n"
        f"⚽ {vb['Partido']}\n"
        f"🕐 {vb['Hora']}\n\n"
        f"📊 <b>{vb['Mercado']}</b>\n"
        f"💰 Cuota: <b>{cuota:.2f}</b>\n"
        f"🤖 Prob. IA: <b>{prob_ia:.1%}</b>\n"
        f"🏠 Prob. Casa: <b>{vb['Prob. Casa']:.1%}</b>\n\n"
        f"📈 EV: <b>{ev:+.1f}%</b>\n"
        f"💵 Stake sugerido: <b>{stake:.1f}%</b> (Kelly 1/{kelly_fraction})\n\n"
        f"🤙 <b>Liquidar</b>: reacciona 👌 (acertada) o 👎 (fallada)"
    )


def send_telegram_message(message):
    """Envía mensaje a Telegram y devuelve el message_id o None."""
    bot_token = os.getenv('TELEGRAM_BOT_TOKEN', '')
    chat_id = os.getenv('TELEGRAM_CHAT_ID', '')
    if not bot_token or not chat_id:
        logger.error("❌ TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID no configurados")
        return None
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            logger.info("✅ Notificación enviada a Telegram")
            return data.get('result', {}).get('message_id')
        else:
            logger.error(f"❌ Error Telegram: HTTP {r.status_code} · {r.text[:200]}")
            return None
    except Exception as e:
        logger.error(f"❌ Error enviando a Telegram: {e}")
        return None


def scan_value_bets():
    """Función principal: escanea fixtures, predice, filtra y notifica."""
    tracker = StatsTracker()
    cfg = load_auto_tune_config(tracker)
    ev_notify = cfg['ev_notify']
    kelly_fraction = cfg['kelly']
    safety_mode = cfg['gap'] is not None and cfg['gap'] > 10
    blacklist = league_blacklist(tracker)
    logger.info(f"🤖 Auto-ajuste: EV≥{ev_notify:.0f}% · Kelly 1/{kelly_fraction} "
                f"(gap={cfg['gap']}, n={cfg['n']})")
    logger.info(f"🚫 Ligas en lista negra ({len(blacklist)}): {sorted(blacklist)}")
    if safety_mode:
        logger.info(f"🚫 MODO SEGURIDAD: gap {cfg['gap']:+.1f} pp > +10 → sin alertas de apuesta")

    if not get_keys():
        logger.error("❌ No hay claves de The Odds API configuradas")
        return 1

    # Cargar modelo
    model = load_models()
    if model is None:
        return 1
    logger.info("✅ Modelos cargados")

    # Cargar base de equipos
    if not os.path.exists(TEAM_DB):
        logger.error(f"❌ Base de equipos no encontrada: {TEAM_DB}")
        return 1
    teams_df = pd.read_csv(TEAM_DB)
    logger.info(f"📁 {len(teams_df)} equipos en la base de datos")

    # Obtener fixtures de The Odds API (con rotación de claves y contador)
    response = odds_get("sports/soccer/odds", {
        "regions": "eu,us",
        "markets": "h2h,totals",
        "oddsFormat": "decimal",
    }, tracker=tracker)
    if response is None or response.status_code != 200:
        logger.error("❌ Error The Odds API (todas las claves agotadas o tope mensual)")
        return 1
    fixtures_data = response.json()
    logger.info(f"📡 {len(fixtures_data)} partidos obtenidos")

    # Filtrar ligas en lista negra
    fixtures_data = [f for f in fixtures_data if f.get("sport_title") not in blacklist]

    # Construir features y predecir
    all_predictions = []
    for event in fixtures_data:
        league = event.get("sport_title", "Unknown")
        home_team = event["home_team"]
        away_team = event["away_team"]
        commence_time = event.get("commence_time", "")

        # Parsear hora
        try:
            dt = datetime.fromisoformat(commence_time.replace('Z', '+00:00'))
            dt_madrid = dt.astimezone(ZoneInfo("Europe/Madrid"))
            hora = dt_madrid.strftime("%d/%m %H:%M")
        except Exception:
            hora = "??"

        # Iterar mercados
        for bookmaker in event.get("bookmakers", [])[:1]:  # Solo primera casa
            for market in bookmaker.get("markets", []):
                market_name = market.get("key", "")
                if market_name not in ["h2h", "totals"]:
                    continue

                for outcome in market.get("outcomes", []):
                    outcome_name = outcome.get("name", "")
                    odds = outcome.get("price", 0)
                    point = outcome.get("point")

                    # Mapear a formato legible
                    if market_name == "h2h":
                        if outcome_name == home_team:
                            mercado = f"1X2 - {home_team}"
                        elif outcome_name == away_team:
                            mercado = f"1X2 - {away_team}"
                        else:
                            mercado = "1X2 - Empate"
                    elif market_name == "totals":
                        if outcome_name == "Over":
                            mercado = f"Over {point} Goles"
                        else:
                            mercado = f"Under {point} Goles"
                    else:
                        continue

                    # Construir features
                    X = build_features_for_match(teams_df, home_team, away_team, odds, mercado, point)
                    if X is None:
                        continue

                    # Predecir probabilidad
                    prob_ia = predict_proba(model, X)[0]
                    prob_casa = 1 / odds if odds > 0 else 0
                    ev = (prob_ia * odds - 1) * 100 if odds > 0 else 0

                    all_predictions.append({
                        "Liga": league,
                        "Partido": f"{home_team} vs {away_team}",
                        "Hora": hora,
                        "Mercado": mercado,
                        "Cuota": odds,
                        "Prob. IA": prob_ia,
                        "Prob. Casa": prob_casa,
                        "EV (%)": ev,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })

    # Estadísticas
    stats = {"total": len(all_predictions)}

    # Filtrar por EV mínimo
    value_bets = [p for p in all_predictions if p['EV (%)'] >= ev_notify]
    logger.info(f"📊 {len(value_bets)} value bets con EV≥{ev_notify:.0f}%")

    if value_bets:
        send_telegram_message(format_summary_message(stats, value_bets, cfg, len(blacklist)))

        # Tomar top 10 por EV
        top10 = sorted(value_bets, key=lambda x: x['EV (%)'], reverse=True)[:10]

        if safety_mode:
            send_telegram_message(
                f"🚫 <b>MODO SEGURIDAD ACTIVO</b>\n\n"
                f"⚖️ Gap {cfg['gap']:+.1f} pp: el modelo está sobreestimando.\n"
                f"Los picks se registran pero NO se recomienda apostar.\n"
                f"Puedes liquidar manualmente con 👌/👎 si lo deseas.\n"
                f"🚫 Ligas excluidas por historial: {len(blacklist)}\n\n"
                f"ℹ️ Más info: /glosario")

            # Picks informativos: se envían solo si aún no tienen mensaje asociado
            todos = tracker.get_all_picks()
            con_msg = {p.get('raw_hash') for p in todos if p.get('telegram_message_id')}
            pend_sin_msg = {p.get('raw_hash'): p['id'] for p in todos
                            if p.get('status') == 'pending' and not p.get('telegram_message_id')}
            for vb in top10:
                h = tracker.hash_pick(vb)
                if h in con_msg:
                    logger.info(f"⏭️ Ya comunicado: {vb['Partido']} | {vb['Mercado']}")
                    continue
                msg = (
                    f"📝 <b>SOLO REGISTRO (MODO SEGURIDAD)</b>\n\n"
                    f"🏆 <b>{vb['Liga']}</b>\n"
                    f"⚽ {vb['Partido']}\n"
                    f"🕐 {vb['Hora']}\n\n"
                    f"📊 <b>{vb['Mercado']}</b>\n"
                    f"💰 Cuota: <b>{vb['Cuota']:.2f}</b>\n"
                    f"🤖 Prob. IA: <b>{vb['Prob. IA']:.1%}</b>\n"
                    f"🏠 Prob. Casa: <b>{vb['Prob. Casa']:.1%}</b>\n\n"
                    f"📈 EV: {vb['EV (%)']:+.1f}%\n\n"
                    f"🚫 <b>NO APOSTAR</b> — modelo descalibrado\n"
                    f"💡 Liquidable manualmente con 👌/👎"
                )
                msg_id = send_telegram_message(msg)
                if msg_id:
                    vb['Telegram Msg ID'] = msg_id
                    if h in pend_sin_msg:
                        tracker.client.table(tracker.table).update(
                            {'telegram_message_id': msg_id}).eq('id', pend_sin_msg[h]).execute()
                time.sleep(1.0)

        elif top10:
            ya_registrados = tracker.get_registered_hashes()
            for vb in top10:
                if tracker.hash_pick(vb) in ya_registrados:
                    logger.info(f"⏭️ Ya alertado en un escaneo previo: {vb['Partido']} | {vb['Mercado']}")
                    continue
                msg_id = send_telegram_message(format_value_bet_alert(vb, kelly_fraction))
                if msg_id:
                    vb['Telegram Msg ID'] = msg_id
                time.sleep(1.0)
        else:
            send_telegram_message(
                f"⚠️ <b>Sin apuestas de valor alto</b>\n\n"
                f"📊 Hay <b>{len(value_bets)}</b> value bets registradas (EV 2-{ev_notify:.0f}%), "
                f"pero ninguna supera el umbral de notificación (EV ≥ {ev_notify:.0f}%).\n\n"
                f"🤖 Config activa: Kelly 1/{kelly_fraction}\n"
                f"💡 Mercado eficiente en las próximas horas."
            )

        # Registrar TODAS las value bets en BD (con o sin alerta)
        for vb in value_bets:
            pick = {
                "liga": vb["Liga"],
                "partido": vb["Partido"],
                "hora": vb["Hora"],
                "mercado": vb["Mercado"],
                "cuota": vb["Cuota"],
                "prob_ia": vb["Prob. IA"],
                "prob_casa": vb["Prob. Casa"],
                "ev_percentage": vb["EV (%)"],
                "timestamp": vb["timestamp"],
                "status": "pending",
                "telegram_message_id": vb.get("Telegram Msg ID"),
            }
            tracker.register_pick(pick)
        logger.info(f"💾 {len(value_bets)} picks registrados en base de datos")
    else:
        send_telegram_message(
            f"💤 <b>Sin value bets</b>\n\n"
            f"📊 {stats['total']} partidos analizados, ninguno con EV ≥ {ev_notify:.0f}%.\n"
            f"🤖 Config activa: Kelly 1/{kelly_fraction}\n"
            f"📡 Datos de The Odds API"
        )

    return 0


if __name__ == "__main__":
    sys.exit(scan_value_bets())
