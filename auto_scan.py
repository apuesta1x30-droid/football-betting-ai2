#!/usr/bin/env python3
"""
v0.5-A · Escaneo automático de value bets.
- Envía alertas a Telegram (máx 10 por escaneo, las de mayor EV)
- Registra picks en Supabase con features del modelo y message_id de Telegram
- Filtra por hora actual (solo partidos futuros)
- Deduplica picks ya registrados (no re-alerta en scans posteriores)
- Mensaje cuando no hay apuestas de valor alto
"""
import os
import sys
import time
import logging
import requests
import joblib
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from scipy.stats import poisson

from stats_tracker import StatsTracker

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

THE_ODDS_API_KEY = os.getenv('THE_ODDS_API_KEY', '')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

EV_THRESHOLD_MIN = 2.0  # Umbral mínimo para registrar en BD
EV_THRESHOLD_NOTIFY = 10.0  # Umbral mínimo para notificar por Telegram
MIN_EV_TO_BET = 5.0  # EV mínimo recomendado para apostar


def send_telegram_message(message, parse_mode="HTML"):
    """Envía mensaje a Telegram y devuelve el message_id (o None si falla)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram no configurado")
        return None
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True
    }
    
    try:
        response = requests.post(url, json=data, timeout=10)
        if response.status_code == 200:
            logger.info("✅ Notificación enviada a Telegram")
            return response.json().get('result', {}).get('message_id')
        else:
            logger.error(f"❌ Error Telegram: {response.text}")
            return None
    except Exception as e:
        logger.error(f"❌ Error enviando Telegram: {e}")
        return None


def format_value_bet_alert(vb):
    """Formatea una value bet como mensaje de alerta para Telegram."""
    stake = calculate_kelly_stake(vb['Prob. IA'], vb['Cuota'])
    return (
        f"🎯 <b>VALUE BET DETECTADA</b>\n\n"
        f"🏆 <b>{vb['Liga']}</b>\n"
        f"⚽ {vb['Partido']}\n"
        f"🕐 {vb['Hora']}\n\n"
        f"📊 <b>{vb['Mercado']}</b>\n"
        f"💰 Cuota: <b>{vb['Cuota']:.2f}</b>\n"
        f"🤖 Prob. IA: <b>{vb['Prob. IA']:.1%}</b>\n"
        f"🏠 Prob. Casa: <b>{vb['Prob. Casa']:.1%}</b>\n\n"
        f"📈 <b>EV: {vb['EV (%)']:+.1f}%</b>\n"
        f"💵 Stake sugerido: <b>{stake:.1%}</b> de banca (Kelly 1/4)\n\n"
        f"🔖 Fuente: {vb['Fuente']}"
    )


def format_summary_message(stats, value_bets):
    """Formatea el resumen del escaneo."""
    return (
        f"📊 <b>RESUMEN DEL ESCANEO</b>\n\n"
        f"🔎 Partidos analizados: <b>{stats['total']}</b>\n"
        f"✅ Value Bets detectadas: <b>{len(value_bets)}</b>\n\n"
        f"📡 Datos de The Odds API\n"
        f"🤖 Probabilidades: Modelo XGBoost"
    )


def calculate_kelly_stake(prob, odd, fraction=4):
    """Calcula stake usando Kelly fraccionado (1/fraction)."""
    if prob <= 0 or odd <= 1:
        return 0.0
    kelly = (prob * odd - 1) / (odd - 1)
    return max(0.0, kelly / fraction)


def load_models():
    """Carga los modelos XGBoost entrenados."""
    models = {}
    try:
        models['over15'] = joblib.load('model_over15.pkl')
        models['over25'] = joblib.load('model_over25.pkl')
        models['over35'] = joblib.load('model_over35.pkl')
        models['btts'] = joblib.load('model_btts.pkl')
        models['1x2'] = joblib.load('model_1x2.pkl')
        logger.info("✅ Modelos cargados")
        return models
    except Exception as e:
        logger.error(f"❌ Error cargando modelos: {e}")
        return None


def load_team_database():
    """Carga la base de datos de equipos."""
    try:
        df_teams = pd.read_csv('team_stats_db.csv')
        team_db = df_teams.set_index('Team').to_dict('index')
        logger.info(f"📁 {len(team_db)} equipos en la base de datos")
        return team_db
    except Exception as e:
        logger.error(f"❌ Error cargando team_stats_db.csv: {e}")
        return {}


def get_team_stats(team_name, team_db):
    """Obtiene estadísticas de un equipo (o defaults si no existe)."""
    default_stats = {
        'Last_Form_Pts': 7,
        'Last_Goals_Scored_Avg': 1.4,
        'Last_Goals_Conceded_Avg': 1.4,
        'Last_Over25_Rate': 0.50,
        'Last_BTTS_Rate': 0.50
    }
    
    if team_name in team_db:
        stats = team_db[team_name]
        for key, value in default_stats.items():
            if key not in stats:
                stats[key] = value
        return stats
    
    team_lower = team_name.lower()
    for db_team, stats in team_db.items():
        if db_team.lower() == team_lower:
            for key, value in default_stats.items():
                if key not in stats:
                    stats[key] = value
            return stats
    
    return default_stats.copy()


def calculate_double_chance_probs(models, features):
    """Calcula probabilidades de doble oportunidad desde 1X2."""
    probs_1x2 = models['1x2'].predict_proba(features)[0]
    return {
        '1X': probs_1x2[0] + probs_1x2[1],
        'X2': probs_1x2[2] + probs_1x2[1],
        '12': probs_1x2[0] + probs_1x2[2]
    }


def calculate_over05_ht_prob(prob_over25):
    """Calcula probabilidad de Over 0.5 goles en 1ª parte."""
    base_prob = 0.70
    correlation_factor = prob_over25 * 0.3
    prob_method1 = min(base_prob + correlation_factor, 0.90)
    expected_goals_ht = 2.7 * 0.42
    prob_method2 = 1 - poisson.pmf(0, expected_goals_ht)
    return (prob_method1 * 0.4) + (prob_method2 * 0.6)


def scan_value_bets():
    """Escanea mercados y detecta value bets."""
    logger.info("🚀 Iniciando escaneo automático...")
    
    if not THE_ODDS_API_KEY:
        logger.error("❌ THE_ODDS_API_KEY no configurada")
        return 1
    
    models = load_models()
    if not models:
        return 1
    
    team_db = load_team_database()
    
    # Obtener fixtures de The Odds API
    url = f"https://api.the-odds-api.com/v4/sports/soccer/odds"
    params = {
        "regions": "eu,us",
        "markets": "h2h,totals",
        "oddsFormat": "decimal",
        "apiKey": THE_ODDS_API_KEY
    }
    
    try:
        response = requests.get(url, params=params, timeout=15)
        if response.status_code != 200:
            logger.error(f"❌ Error The Odds API: {response.text}")
            return 1
        fixtures_data = response.json()
    except Exception as e:
        logger.error(f"❌ Error consultando The Odds API: {e}")
        return 1
    
    logger.info(f"📡 {len(fixtures_data)} partidos obtenidos")
    
    # Analizar cada partido
    value_bets = []
    now = datetime.now(timezone.utc)
    stats = {'total': 0, 'api_football': 0, 'calculated': 0, 'today': 0}
    
    for event in fixtures_data:
        league = event.get("sport_title", "Unknown")
        home_team = event["home_team"]
        away_team = event["away_team"]
        commence_time = event["commence_time"]
        
        if commence_time.endswith('Z'):
            match_time = datetime.fromisoformat(commence_time.replace('Z', '+00:00'))
        else:
            match_time = datetime.fromisoformat(commence_time)
        
        match_time_es = match_time.astimezone(ZoneInfo("Europe/Madrid"))
        now_es = now.astimezone(ZoneInfo("Europe/Madrid"))
        
        # Solo partidos que aún no han empezado (filtrado por hora, no solo por fecha)
        if match_time_es <= now_es:
            continue
        
        stats['total'] += 1
        
        home_stats = get_team_stats(home_team, team_db)
        away_stats = get_team_stats(away_team, team_db)
        
        form_diff = home_stats.get('Last_Form_Pts', 7) - away_stats.get('Last_Form_Pts', 7)
        goal_threat_diff = home_stats.get('Last_Goals_Scored_Avg', 1.4) - away_stats.get('Last_Goals_Conceded_Avg', 1.4)
        combined_o25 = (home_stats.get('Last_Over25_Rate', 0.50) + away_stats.get('Last_Over25_Rate', 0.50)) / 2
        combined_btts = (home_stats.get('Last_BTTS_Rate', 0.50) + away_stats.get('Last_BTTS_Rate', 0.50)) / 2
        
        features = pd.DataFrame([{
            'Home_Form_Pts': home_stats.get('Last_Form_Pts', 7),
            'Away_Form_Pts': away_stats.get('Last_Form_Pts', 7),
            'Form_Diff': form_diff,
            'Home_Goals_Scored': home_stats.get('Last_Goals_Scored_Avg', 1.4),
            'Away_Goals_Conceded': away_stats.get('Last_Goals_Conceded_Avg', 1.4),
            'Goal_Threat_Diff': goal_threat_diff,
            'Combined_Over25_Rate': combined_o25,
            'Combined_BTTS_Rate': combined_btts
        }])
        
        probs_1x2 = models['1x2'].predict_proba(features)[0]
        prob_over25 = models['over25'].predict_proba(features)[0][1]
        prob_btts = models['btts'].predict_proba(features)[0][1]
        dc_probs = calculate_double_chance_probs(models, features)
        prob_over05_ht = calculate_over05_ht_prob(prob_over25)
        
        # Buscar mejor cuota para cada mercado
        best_odds = {}
        
        for bookmaker in event.get("bookmakers", []):
            for market in bookmaker.get("markets", []):
                market_key = market["key"]
                for outcome in market.get("outcomes", []):
                    odd = outcome["price"]
                    name = outcome["name"]
                    point = outcome.get("point", 2.5)
                    
                    if odd < 1.3 or odd > 3.0:
                        continue
                    
                    key = None
                    if market_key == "h2h":
                        if name == home_team:
                            key = f"1X2_{home_team}"
                        elif name == "Draw":
                            key = "1X2_Draw"
                        elif name == away_team:
                            key = f"1X2_{away_team}"
                    elif market_key == "totals" and name == "Over":
                        if point in [1.5, 2.5, 3.5]:
                            key = f"Over_{point}"
                    
                    if key is not None:
                        if key not in best_odds or odd > best_odds[key]:
                            best_odds[key] = odd
        
        # Analizar cada mercado con cuota disponible
        for market_key, odd in best_odds.items():
            prob = None
            mercado_name = None
            is_calculated = '_CALC' in market_key
            
            if market_key.startswith("1X2_"):
                team_part = market_key.split("_")[1]
                if team_part == home_team:
                    prob, mercado_name = probs_1x2[0], f"1X2 - {home_team}"
                elif team_part == "Draw":
                    prob, mercado_name = probs_1x2[1], "1X2 - Empate"
                elif team_part == away_team:
                    prob, mercado_name = probs_1x2[2], f"1X2 - {away_team}"
            elif market_key.startswith("Over_"):
                point = float(market_key.split("_")[1])
                if point == 1.5:
                    prob = models['over15'].predict_proba(features)[0][1]
                    mercado_name = "Over 1.5 Goles"
                elif point == 2.5:
                    prob = models['over25'].predict_proba(features)[0][1]
                    mercado_name = "Over 2.5 Goles"
                elif point == 3.5:
                    prob = models['over35'].predict_proba(features)[0][1]
                    mercado_name = "Over 3.5 Goles"
            elif market_key.startswith("DC_"):
                dc_type = market_key.replace("DC_", "").replace("_CALC", "")
                if dc_type == "1X":
                    prob = dc_probs['1X']
                    mercado_name = "Doble Oportunidad - 1X"
                elif dc_type == "X2":
                    prob = dc_probs['X2']
                    mercado_name = "Doble Oportunidad - X2"
                elif dc_type == "12":
                    prob = dc_probs['12']
                    mercado_name = "Doble Oportunidad - 12"
            elif market_key.startswith("HT_Over_0.5"):
                prob = prob_over05_ht
                mercado_name = "Over 0.5 Goles 1ª Parte"
            elif market_key.startswith("BTTS_"):
                btts_type = market_key.split("_")[1]
                if btts_type == "Yes":
                    prob, mercado_name = prob_btts, "BTTS - Sí (Ambos marcan)"
                elif btts_type == "No":
                    prob, mercado_name = 1 - prob_btts, "BTTS - No"
            
            if prob is None:
                continue
            
            ev = (prob * odd) - 1
            ev_percentage = ev * 100
            
            if ev_percentage > EV_THRESHOLD_MIN:
                value_bets.append({
                    "Liga": league,
                    "Partido": f"{home_team} vs {away_team}",
                    "Hora": match_time_es.strftime('%d/%m %H:%M'),
                    "Mercado": mercado_name,
                    "Cuota": odd,
                    "Prob. IA": prob,
                    "Prob. Casa": 1/odd,
                    "EV (%)": ev_percentage,
                    "Fuente": "Cálculo" if is_calculated else "API-Football",
                    "Features": features.iloc[0].to_dict()
                })
    
    # Enviar resumen y alertas
    tracker = StatsTracker()
    
    if value_bets:
        send_telegram_message(format_summary_message(stats, value_bets))
        
        # Máximo 10 alertas por escaneo: las de mayor EV
        top10 = sorted(
            [vb for vb in value_bets if vb['EV (%)'] >= EV_THRESHOLD_NOTIFY],
            key=lambda x: -x['EV (%)']
        )[:10]
        
        if top10:
            ya_registrados = tracker.get_registered_hashes()
            for vb in top10:
                if tracker.hash_pick(vb) in ya_registrados:
                    logger.info(f"⏭️ Ya alertado en un escaneo previo: {vb['Partido']} | {vb['Mercado']}")
                    continue
                msg_id = send_telegram_message(format_value_bet_alert(vb))
                if msg_id:
                    vb['Telegram Msg ID'] = msg_id
                time.sleep(0.5)
        else:
            send_telegram_message(
                f"⚠️ <b>Sin apuestas de valor alto</b>\n\n"
                f"📊 Hay <b>{len(value_bets)}</b> value bets registradas (EV 2-10%), "
                f"pero ninguna supera el umbral de notificación (EV ≥ {EV_THRESHOLD_NOTIFY:.0f}%).\n\n"
                f"💡 Mercado eficiente en las próximas horas."
            )
        
        # Registro en BD después del envío (para guardar el message_id de Telegram)
        registered_count = 0
        for vb in value_bets:
            if tracker.register_pick(vb):
                registered_count += 1
        logger.info(f"💾 {registered_count} picks registrados en base de datos")
    else:
        send_telegram_message("💤 <b>Escaneo completado</b>\n\nSin Value Bets detectadas en las próximas horas.")
    
    return 0


if __name__ == "__main__":
    sys.exit(scan_value_bets())
