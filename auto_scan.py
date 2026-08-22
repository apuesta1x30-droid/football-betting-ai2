#!/usr/bin/env python3
"""
v0.5-B · Escaneo automático de value bets con auto-ajuste dinámico.
- Lee configuración de auto_tune.py (EV mínimo + Kelly) desde Supabase
- Envía alertas a Telegram (máx 10 por escaneo, las de mayor EV)
- Modo seguridad (gap > +10): envía banner de aviso PERO notifica los picks
  en formato normal para poder liquidarlos manualmente con 👌/👎
- Registra picks en Supabase con features del modelo y message_id de Telegram
- Filtra por hora actual (solo partidos futuros)
- Deduplica picks ya alertados (no re-alerta en scans posteriores)
- Lista negra empírica de ligas (n≥8 y PnL≤-5)
- The Odds API con rotación de claves (odds_client)
"""
import os
import sys
import time
import json
import logging
import requests
import joblib
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from scipy.stats import poisson

from stats_tracker import StatsTracker
from odds_client import odds_get, get_keys

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

EV_THRESHOLD_MIN = 2.0
DEFAULT_EV_NOTIFY = 10.0
DEFAULT_KELLY = 4
META_KEY_AUTO_TUNE = 'auto_tune'


def load_auto_tune_config(tracker):
    cfg = {'ev_notify': DEFAULT_EV_NOTIFY, 'kelly': DEFAULT_KELLY, 'gap': None, 'n': None}
    if not tracker or not tracker.enabled:
        return cfg
    try:
        resp = tracker.client.table('meta').select('value').eq('key', META_KEY_AUTO_TUNE).execute()
        if resp.data:
            data = json.loads(resp.data[0]['value'])
            saved = data.get('cfg', {})
            cfg['ev_notify'] = float(saved.get('ev_notify', DEFAULT_EV_NOTIFY))
            cfg['kelly'] = int(saved.get('kelly', DEFAULT_KELLY))
            cfg['gap'] = data.get('gap')
            cfg['n'] = data.get('n')
    except Exception as e:
        logger.debug(f"No se pudo leer auto_tune config: {e}")
    return cfg


def league_blacklist(tracker, min_n=8, min_pnl=-5.0):
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

def compute_recalib(tracker, min_n=50):
    """Capa B: recalibración empírica (mínimos cuadrados) de la Prob. IA.
    Devuelve {'alpha', 'beta', 'n'} o None si hay pocos liquidados."""
    if not tracker or not tracker.enabled:
        return None
    xs, ys = [], []
    for p in tracker.get_all_picks():
        if p.get('status') in ('won', 'lost') and p.get('prob_ia') is not None:
            try:
                xs.append(float(p['prob_ia']))
                ys.append(1.0 if p['status'] == 'won' else 0.0)
            except Exception:
                continue
    n = len(xs)
    if n < min_n:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 1e-9:
        return None
    beta = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    alpha = my - beta * mx
    return {'alpha': alpha, 'beta': beta, 'n': n}

def send_telegram_message(message, parse_mode="HTML"):
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


def format_value_bet_alert(vb, kelly_fraction):
    stake = calculate_kelly_stake(vb['Prob. IA'], vb['Cuota'], fraction=kelly_fraction)
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
        f"💵 Stake sugerido: <b>{stake:.1%}</b> de banca (Kelly 1/{kelly_fraction})\n\n"
        f"🔖 Fuente: {vb['Fuente']}"
    )


def format_summary_message(stats, value_bets, cfg, n_blacklist=0):
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


def calculate_kelly_stake(prob, odd, fraction=4):
    if prob <= 0 or odd <= 1:
        return 0.0
    kelly = (prob * odd - 1) / (odd - 1)
    return max(0.0, kelly / fraction)


def load_models():
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
    try:
        df_teams = pd.read_csv('team_stats_db.csv')
        team_db = df_teams.set_index('Team').to_dict('index')
        logger.info(f"📁 {len(team_db)} equipos en la base de datos")
        return team_db
    except Exception as e:
        logger.error(f"❌ Error cargando team_stats_db.csv: {e}")
        return {}


def get_team_stats(team_name, team_db):
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
    probs_1x2 = models['1x2'].predict_proba(features)[0]
    return {
        '1X': probs_1x2[0] + probs_1x2[1],
        'X2': probs_1x2[2] + probs_1x2[1],
        '12': probs_1x2[0] + probs_1x2[2]
    }


def calculate_over05_ht_prob(prob_over25):
    base_prob = 0.70
    correlation_factor = prob_over25 * 0.3
    prob_method1 = min(base_prob + correlation_factor, 0.90)
    expected_goals_ht = 2.7 * 0.42
    prob_method2 = 1 - poisson.pmf(0, expected_goals_ht)
    return (prob_method1 * 0.4) + (prob_method2 * 0.6)


def scan_value_bets():
    logger.info("🚀 Iniciando escaneo automático...")
    
    if not get_keys():
        logger.error("❌ No hay claves de The Odds API configuradas")
        return 1
    
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
        logger.info(f"🚫 MODO SEGURIDAD: gap {cfg['gap']:+.1f} pp > +10 → "
                    f"aviso enviado, picks notificados para liquidación manual")
    
    models = load_models()
    if not models:
        return 1
    
    team_db = load_team_database()
    
    # Capa B: recalibración empírica de probabilidades
    recalib = compute_recalib(tracker)
    if recalib:
        logger.info(f"🧮 Capa B activa: p_corr = {recalib['alpha']:.2f} + {recalib['beta']:.2f}·p (n={recalib['n']})")
    else:
        logger.info("🧮 Capa B inactiva (n<50 liquidados)")
    
    response = odds_get("sports/soccer/odds", {
        "regions": "eu,us",
        "markets": "h2h,totals",
        "oddsFormat": "decimal",
    }, tracker=tracker)
    if response is None or response.status_code != 200:
        logger.error("❌ Error The Odds API (claves agotadas o tope mensual)")
        return 1
    fixtures_data = response.json()
    
    logger.info(f"📡 {len(fixtures_data)} partidos obtenidos")
    
    value_bets = []
    now = datetime.now(timezone.utc)
    stats = {'total': 0, 'api_football': 0, 'calculated': 0, 'today': 0}
    
    for event in fixtures_data:
        league = event.get("sport_title", "Unknown")
        if league in blacklist:
            continue
        home_team = event["home_team"]
        away_team = event["away_team"]
        commence_time = event["commence_time"]
        
        if commence_time.endswith('Z'):
            match_time = datetime.fromisoformat(commence_time.replace('Z', '+00:00'))
        else:
            match_time = datetime.fromisoformat(commence_time)
        
        match_time_es = match_time.astimezone(ZoneInfo("Europe/Madrid"))
        now_es = now.astimezone(ZoneInfo("Europe/Madrid"))
        
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
            
            # Capa B: corregir la probabilidad antes de calcular el EV
            if recalib:
                prob = max(0.03, min(0.97, recalib['alpha'] + recalib['beta'] * prob))
            
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
    
    if value_bets:
        send_telegram_message(format_summary_message(stats, value_bets, cfg, len(blacklist)))
        
        top10 = sorted(
            [vb for vb in value_bets if vb['EV (%)'] >= ev_notify],
            key=lambda x: -x['EV (%)']
        )[:10]
        
        if safety_mode:
            send_telegram_message(
                f"🚫 <b>MODO SEGURIDAD ACTIVO</b>\n\n"
                f"⚖️ Gap {cfg['gap']:+.1f} pp: el modelo está sobreestimando.\n"
                f"NO apuestes dinero real: picks enviados solo para registro.\n"
                f"Puedes liquidarlos manualmente con 👌/👎.\n"
                f"🚫 Ligas excluidas por historial: {len(blacklist)}\n\n"
                f"ℹ️ Más info: /glosario")
        
        if top10:
            # Deduplicación por telegram_message_id (no por registro en BD)
            todos = tracker.get_all_picks()
            con_msg = {p.get('raw_hash') for p in todos if p.get('telegram_message_id')}
            sin_msg = {p.get('raw_hash'): p['id'] for p in todos
                       if not p.get('telegram_message_id')}
            for vb in top10:
                h = tracker.hash_pick(vb)
                if h in con_msg:
                    logger.info(f"⏭️ Ya alertado en un escaneo previo: {vb['Partido']} | {vb['Mercado']}")
                    continue
                msg_id = send_telegram_message(format_value_bet_alert(vb, kelly_fraction))
                if msg_id:
                    vb['Telegram Msg ID'] = msg_id
                    if h in sin_msg:
                        tracker.client.table(tracker.table).update(
                            {'telegram_message_id': msg_id}).eq('id', sin_msg[h]).execute()
                time.sleep(0.5)
        else:
            send_telegram_message(
                f"⚠️ <b>Sin apuestas de valor alto</b>\n\n"
                f"📊 Hay <b>{len(value_bets)}</b> value bets registradas (EV 2-{ev_notify:.0f}%), "
                f"pero ninguna supera el umbral de notificación (EV ≥ {ev_notify:.0f}%).\n\n"
                f"🤖 Config activa: Kelly 1/{kelly_fraction}\n"
                f"💡 Mercado eficiente en las próximas horas."
            )
        
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
