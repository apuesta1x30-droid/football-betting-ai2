"""
 Football Betting AI - Escaneo Automático
Ejecutado por GitHub Actions 2 veces al día
Envía notificaciones a Telegram cuando EV > 10%
"""

import os
import sys
import time
import pandas as pd
import numpy as np
import requests
import joblib
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from scipy.stats import poisson
import warnings
warnings.filterwarnings('ignore')

# ==========================================
# CONFIGURACIÓN Y LOGGING
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Variables de entorno (desde GitHub Secrets)
THE_ODDS_API_KEY = os.getenv('THE_ODDS_API_KEY', '')
API_FOOTBALL_KEY = os.getenv('API_FOOTBALL_KEY', '00599a23daf70c08d47f1db56dfe5eb5')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

# Parámetros del escaneo
MIN_ODD = 1.3
MAX_ODD = 3.0
EV_THRESHOLD_NOTIFY = 10.0 # Notificar solo EV > 10%
EV_THRESHOLD_MIN = 2.0 # Mínimo para considerar Value Bet

API_FOOTBALL_HEADERS = {
    'x-rapidapi-key': API_FOOTBALL_KEY,
    'x-rapidapi-host': "v3.football.api-sports.io"
}

# ==========================================
# FUNCIONES DE TELEGRAM
# ==========================================
def send_telegram_message(message, parse_mode="HTML"):
    """Envía mensaje a Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("⚠️ Telegram no configurado")
        return False
    
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
            return True
        else:
            logger.error(f"❌ Error Telegram: {response.text}")
            return False
    except Exception as e:
        logger.error(f"❌ Error enviando Telegram: {e}")
        return False

def format_value_bet_alert(vb):
    """Formatea una Value Bet para alerta de Telegram."""
    if vb['EV (%)'] >= 20:
        emoji, level = "🚀", "EXCEPCIONAL"
    elif vb['EV (%)'] >= 10:
        emoji, level = "🟢", "ALTO"
    else:
        emoji, level = "🟡", "MODERADO"
    
    return f"""
{emoji} <b>VALUE BET {level}</b> {emoji}

🏆 <b>{vb['Liga']}</b>
⚽ <b>{vb['Partido']}</b>
⏰ {vb['Hora']}

📊 <b>Mercado:</b> {vb['Mercado']}
💰 <b>Cuota:</b> {vb['Cuota']:.2f}
🤖 <b>Prob. IA:</b> {vb['Prob. IA']:.1%}
📉 <b>Prob. Casa:</b> {vb['Prob. Casa']:.1%}

💚 <b>EV: +{vb['EV (%)']:.1f}%</b>
 Fuente: {vb.get('Fuente', 'N/A')}

<i>Apuesta con responsabilidad. Gestiona tu banca.</i>
""".strip()

def format_summary_message(stats, value_bets):
    """Formatea el resumen del escaneo."""
   now = datetime.now(ZoneInfo("Europe/Madrid")).strftime('%d/%m/%Y %H:%M')
 # now = datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M UTC')
    high_ev = len([vb for vb in value_bets if vb['EV (%)'] >= 10])
    return f"""
📊 <b>RESUMEN DEL ESCANEO</b>
📅 {now}

🔍 Partidos analizados: <b>{stats['total']}</b>
✅ Value Bets encontradas: <b>{len(value_bets)}</b>

🟢 Alto valor (EV ≥ 10%): <b>{high_ev}</b>

📈 Mejor EV: <b>{max([vb['EV (%)'] for vb in value_bets], default=0):.1f}%</b>
📊 EV Promedio: <b>{np.mean([vb['EV (%)'] for vb in value_bets]) if value_bets else 0:.1f}%</b>
""".strip()

# ==========================================
# CARGA DE MODELOS Y DATOS
# ==========================================
def load_models():
    """Carga todos los modelos de IA."""
    models = {}
    model_files = {
        'over15': 'model_over15.pkl',
        'over25': 'model_over25.pkl',
        'over35': 'model_over35.pkl',
        'btts': 'model_btts.pkl',
        '1x2': 'model_1x2.pkl'
    }
    for name, filename in model_files.items():
        if os.path.exists(filename):
            models[name] = joblib.load(filename)
            logger.info(f"✅ Modelo cargado: {name}")
        else:
            logger.error(f"❌ Modelo no encontrado: {filename}")
            return None
    return models

def load_team_database():
    """Carga la base de datos de equipos."""
    if not os.path.exists('team_stats_db.csv'):
        logger.error("❌ team_stats_db.csv no encontrado")
        return {}
    
    df_teams = pd.read_csv('team_stats_db.csv')
    team_db = df_teams.set_index('Team').to_dict('index')
    logger.info(f"📁 {len(team_db)} equipos en la base de datos")
    return team_db

def get_team_stats(team_name, team_db):
    """
    Busca las stats del equipo. Si no existe o faltan campos,
    usa valores por defecto. USAMOS .get() PARA EVITAR KeyError.
    """
    default_stats = {
        'Last_Form_Pts': 7,
        'Last_Goals_Scored_Avg': 1.4,
        'Last_Goals_Conceded_Avg': 1.4,
        'Last_Over25_Rate': 0.50,
        'Last_BTTS_Rate': 0.50
    }
    
    if team_name in team_db:
        stats = team_db[team_name]
        # Rellenar campos faltantes con valores por defecto
        for key, value in default_stats.items():
            if key not in stats:
                stats[key] = value
        return stats
    
    # Búsqueda case-insensitive
    for db_team, stats in team_db.items():
        if db_team.lower() == team_name.lower():
            for key, value in default_stats.items():
                if key not in stats:
                    stats[key] = value
            return stats
    
    # Si no se encuentra el equipo, devolver defaults
    return default_stats.copy()

# ==========================================
# FUNCIONES AUXILIARES
# ==========================================
def get_fixture_id(home_team, away_team, match_date):
    """Busca el fixture_id en API-Football."""
    try:
        response = requests.get(
            "https://v3.football.api-sports.io/fixtures",
            headers=API_FOOTBALL_HEADERS,
            params={"date": match_date[:10], "timezone": "UTC"},
            timeout=10
        )
        for fixture in response.json().get("response", []):
            h = fixture["teams"]["home"]["name"]
            a = fixture["teams"]["away"]["name"]
            if home_team.lower() in h.lower() and away_team.lower() in a.lower():
                return fixture["fixture"]["id"]
    except Exception as e:
        logger.debug(f"Error buscando fixture: {e}")
    return None

def get_api_football_odds(fixture_id):
    """Obtiene cuotas de API-Football."""
    if not fixture_id:
        return {}
    try:
        response = requests.get(
            "https://v3.football.api-sports.io/odds",
            headers=API_FOOTBALL_HEADERS,
            params={"fixture": fixture_id},
            timeout=10
        )
        odds_data = {}
        for item in response.json().get("response", []):
            for bookmaker in item.get("bookmakers", []):
                for bet in bookmaker.get("bets", []):
                    for value in bet.get("values", []):
                        if value["odd"]:
                            key = f"{bet['id']}_{value['value']}"
                            if key not in odds_data or float(value["odd"]) > float(odds_data[key]):
                                odds_data[key] = float(value["odd"])
        return odds_data
    except Exception as e:
        logger.debug(f"Error obteniendo odds: {e}")
        return {}

def calculate_dc_probs(probs_1x2):
    """Calcula probabilidades de Doble Oportunidad."""
    return {
        '1X': probs_1x2[0] + probs_1x2[1],
        'X2': probs_1x2[2] + probs_1x2[1],
        '12': probs_1x2[0] + probs_1x2[2]
    }

def calculate_ht_prob(prob_over25):
    """Calcula probabilidad Over 0.5 Primera Parte."""
    p1 = min(0.70 + prob_over25 * 0.3, 0.90)
    p2 = 1 - poisson.pmf(0, 2.7 * 0.42)
    return (p1 * 0.4) + (p2 * 0.6)

# ==========================================
# ESCANEO PRINCIPAL
# ==========================================
def scan_value_bets():
    """Función principal de escaneo."""
    logger.info("🚀 Iniciando escaneo automático...")
    if not THE_ODDS_API_KEY:
        logger.error("❌ THE_ODDS_API_KEY no configurada")
        return
    
    models = load_models()
    if not models:
        logger.error("❌ No se pudieron cargar los modelos")
        return
    
    team_db = load_team_database()
    
    url = "https://api.the-odds-api.com/v4/sports/soccer/odds"
    params = {
        "regions": "eu,us",
        "markets": "h2h,totals",
        "oddsFormat": "decimal",
        "apiKey": THE_ODDS_API_KEY
    }
    
    try:
        response = requests.get(url, params=params, timeout=15)
        if response.status_code != 200:
            logger.error(f"❌ Error API: {response.text}")
            return
        fixtures = response.json()
        logger.info(f"📡 {len(fixtures)} partidos obtenidos")
    except Exception as e:
        logger.error(f" Error API: {e}")
        return
    
    value_bets = []
    now = datetime.now(timezone.utc)
    stats = {'total': 0, 'api_football': 0, 'calculated': 0}
    
    for event in fixtures:
        commence_time = event["commence_time"]
        if 'Z' in commence_time:
            match_time = datetime.fromisoformat(commence_time.replace('Z', '+00:00'))
        else:
            match_time = datetime.fromisoformat(commence_time)
        
        # Solo partidos de HOY
        if match_time.date() != now.date():
            continue
        
        stats['total'] += 1
        
        league = event.get("sport_title", "Unknown")
        home_team = event["home_team"]
        away_team = event["away_team"]
        
        home_stats = get_team_stats(home_team, team_db)
        away_stats = get_team_stats(away_team, team_db)
        
        # ✅ CORREGIDO: Usar .get() para evitar KeyError
        features = pd.DataFrame([{
            'Home_Form_Pts': home_stats.get('Last_Form_Pts', 7),
            'Away_Form_Pts': away_stats.get('Last_Form_Pts', 7),
            'Form_Diff': home_stats.get('Last_Form_Pts', 7) - away_stats.get('Last_Form_Pts', 7),
            'Home_Goals_Scored': home_stats.get('Last_Goals_Scored_Avg', 1.4),
            'Away_Goals_Conceded': away_stats.get('Last_Goals_Conceded_Avg', 1.4),
            'Goal_Threat_Diff': home_stats.get('Last_Goals_Scored_Avg', 1.4) - away_stats.get('Last_Goals_Conceded_Avg', 1.4),
            'Combined_Over25_Rate': (home_stats.get('Last_Over25_Rate', 0.50) + away_stats.get('Last_Over25_Rate', 0.50)) / 2,
            'Combined_BTTS_Rate': (home_stats.get('Last_BTTS_Rate', 0.50) + away_stats.get('Last_BTTS_Rate', 0.50)) / 2
        }])
        
        probs_1x2 = models['1x2'].predict_proba(features)[0]
        prob_over25 = models['over25'].predict_proba(features)[0][1]
        dc_probs = calculate_dc_probs(probs_1x2)
        prob_ht = calculate_ht_prob(prob_over25)
        
        fixture_id = get_fixture_id(home_team, away_team, commence_time)
        api_odds = get_api_football_odds(fixture_id)
        best_odds = {}
        
        # The Odds API
        for bookmaker in event.get("bookmakers", []):
            for market in bookmaker.get("markets", []):
                mk = market["key"]
                for outcome in market.get("outcomes", []):
                    odd = outcome["price"]
                    if odd < MIN_ODD or odd > MAX_ODD:
                        continue
                    key = None
                    if mk == "h2h":
                        if outcome["name"] == home_team:
                            key = f"1X2_{home_team}"
                        elif outcome["name"] == "Draw":
                            key = "1X2_Draw"
                        elif outcome["name"] == away_team:
                            key = f"1X2_{away_team}"
                    elif mk == "totals" and outcome["name"] == "Over" and outcome.get("point", 2.5) in [1.5, 2.5, 3.5]:
                        key = f"Over_{outcome.get('point')}"
                    if key and (key not in best_odds or odd > best_odds[key]):
                        best_odds[key] = odd
        
        # Doble Oportunidad
        for dc_key, prob_key in [('1X', '1X'), ('X2', 'X2'), ('12', '12')]:
            api_val = api_odds.get(f'12_{dc_key}')
            calc_val = 1 / dc_probs[prob_key]
            if api_val and MIN_ODD <= api_val <= MAX_ODD:
                best_odds[f'DC_{dc_key}'] = api_val
                stats['api_football'] += 1
            elif MIN_ODD <= calc_val <= MAX_ODD:
                best_odds[f'DC_{dc_key}_CALC'] = calc_val
                stats['calculated'] += 1
        
        # Over 0.5 Primera Parte
        ht_api = next((v for k, v in api_odds.items() if '6_Over' in k and '0.5' in k), None)
        ht_calc = 1 / prob_ht
        if ht_api and MIN_ODD <= ht_api <= MAX_ODD:
            best_odds['HT_Over_0.5'] = ht_api
            stats['api_football'] += 1
        elif MIN_ODD <= ht_calc <= MAX_ODD:
            best_odds['HT_Over_0.5_CALC'] = ht_calc
            stats['calculated'] += 1
        
        # Calcular EV
        for mk, odd in best_odds.items():
            prob, name, is_calc = None, None, '_CALC' in mk
            
            if mk.startswith("1X2_"):
                tp = mk.split("_")[1]
                if tp == home_team:
                    prob, name = probs_1x2[0], f"1X2 - {home_team}"
                elif tp == "Draw":
                    prob, name = probs_1x2[1], "1X2 - Empate"
                elif tp == away_team:
                    prob, name = probs_1x2[2], f"1X2 - {away_team}"
            elif mk.startswith("Over_"):
                p = float(mk.split("_")[1])
                if p == 1.5:
                    prob, name = models['over15'].predict_proba(features)[0][1], "Over 1.5 Goles"
                elif p == 2.5:
                    prob, name = models['over25'].predict_proba(features)[0][1], "Over 2.5 Goles"
                elif p == 3.5:
                    prob, name = models['over35'].predict_proba(features)[0][1], "Over 3.5 Goles"
            elif mk.startswith("DC_"):
                tp = mk.replace("DC_", "").replace("_CALC", "")
                prob, name = dc_probs[tp], f"Doble Oportunidad - {tp}"
            elif mk.startswith("HT_Over_0.5"):
                prob, name = prob_ht, "Over 0.5 Goles 1ª Parte"
            
            if prob:
                ev = (prob * odd) - 1
                if ev * 100 > EV_THRESHOLD_MIN:
                  #  match_dt = datetime.fromisoformat(commence_time.replace('Z', '+00:00'))
                   # match_dt_local = match_dt.astimezone(ZoneInfo("Europe/Madrid"))
                    value_bets.append({
                        "Liga": league,
                        "Partido": f"{home_team} vs {away_team}",
                      #  "H inicio": match_dt_local.strftime('%d/%m %H:%M'),
                     "Hora": commence_time[:16].replace('T', ' '),
                        "Mercado": name,
                        "Cuota": odd,
                        "Prob. IA": prob,
                        "Prob. Casa": 1/odd,
                        "EV (%)": ev * 100,
                        "Fuente": "Cálculo" if is_calc else "API-Football"
                    })

    if value_bets:
        df = pd.DataFrame(value_bets).drop_duplicates()
        value_bets = df.to_dict('records')
    
    logger.info(f"✅ {len(value_bets)} Value Bets encontradas")
    
    if value_bets:
        send_telegram_message(format_summary_message(stats, value_bets))
        for vb in value_bets:
            if vb['EV (%)'] >= EV_THRESHOLD_NOTIFY:
                send_telegram_message(format_value_bet_alert(vb))
                time.sleep(0.5)
    else:
         send_telegram_message(f"💤 <b>Escaneo completado - Sin Value Bets</b>\n\n {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M UTC')}\n🔍 Partidos: <b>{stats['total']}</b>\n\n<i>Cuotas eficientes hoy.</i>")
       # send_telegram_message(f"💤 <b>Escaneo completado - Sin Value Bets</b>\n\n {datetime.now(ZoneInfo('Europe/Madrid')).strftime('%d/%m/%Y %H:%M')}\n🔍 Partidos: <b>{stats['total']}</b>\n\n<i>Cuotas eficientes hoy.</i>")

# ==========================================
# EJECUCIÓN
# ==========================================
if __name__ == "__main__":
    try:
        scan_value_bets()
    except Exception as e:
        logger.error(f"❌ Error fatal: {e}", exc_info=True)
        send_telegram_message(f"❌ <b>Error en el escaneo</b>\n\n{str(e)}")
        sys.exit(1)
