#!/usr/bin/env python3
"""
Liquidación masiva de picks pendientes usando The Odds API.
Consulta resultados de partidos ya jugados y actualiza Supabase.
"""
import os
import sys
import logging
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from stats_tracker import StatsTracker

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

THE_ODDS_API_KEY = os.getenv('THE_ODDS_API_KEY', '')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')


def send_telegram(message):
    """Envía mensaje a Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=10
        )
    except Exception as e:
        logger.error(f"❌ Telegram: {e}")


def get_match_result(home_team, away_team, match_date):
    """
    Consulta The Odds API para obtener el resultado de un partido.
    Devuelve: {'home_score': X, 'away_score': Y, 'status': 'finished'} o None si no se encuentra.
    """
    # The Odds API endpoint de scores históricos
    url = "https://api.the-odds-api.com/v4/sports/soccer/scores"
    params = {
        "apiKey": THE_ODDS_API_KEY,
        "daysFrom": 7  # Últimos 7 días
    }
    
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code != 200:
            logger.error(f"❌ The Odds API scores: HTTP {r.status_code}")
            return None
        
        events = r.json()
        
        # Buscar el partido por equipos y fecha
        for event in events:
            event_home = event.get('home_team', '')
            event_away = event.get('away_team', '')
            
            # Normalizar nombres (quitar espacios extra, minúsculas)
            if (event_home.lower().strip() == home_team.lower().strip() and
                event_away.lower().strip() == away_team.lower().strip()):
                
                # Verificar fecha (permitir ±1 día por zonas horarias)
                commence_time = event.get('commence_time', '')
                if commence_time:
                    event_date = datetime.fromisoformat(commence_time.replace('Z', '+00:00'))
                    event_date_str = event_date.astimezone(ZoneInfo("Europe/Madrid")).strftime('%Y-%m-%d')
                    
                    if event_date_str == match_date or (
                        datetime.strptime(event_date_str, '%Y-%m-%d') - 
                        datetime.strptime(match_date, '%Y-%m-%d')
                    ).days in [-1, 0, 1]:
                        
                        scores = event.get('scores', [])
                        if scores and len(scores) >= 2:
                            home_score = scores[0].get('score', '0')
                            away_score = scores[1].get('score', '0')
                            
                            return {
                                'home_score': int(home_score) if home_score.isdigit() else 0,
                                'away_score': int(away_score) if away_score.isdigit() else 0,
                                'status': 'finished'
                            }
        
        return None
    except Exception as e:
        logger.error(f"❌ Error consultando resultado: {e}")
        return None


def evaluate_pick(pick, result):
    """
    Evalúa si un pick es won, lost o void basado en el resultado.
    Devuelve: 'won', 'lost' o 'void'
    """
    mercado = pick.get('mercado', '').lower()
    home_goals = result['home_score']
    away_goals = result['away_score']
    total_goals = home_goals + away_goals
    
    # Mercados de goles
    if 'over 0.5' in mercado and '1ª parte' not in mercado:
        return 'won' if total_goals >= 1 else 'lost'
    elif 'over 1.5' in mercado:
        return 'won' if total_goals >= 2 else 'lost'
    elif 'over 2.5' in mercado:
        return 'won' if total_goals >= 3 else 'lost'
    elif 'over 3.5' in mercado:
        return 'won' if total_goals >= 4 else 'lost'
    elif 'under 0.5' in mercado:
        return 'won' if total_goals == 0 else 'lost'
    elif 'under 1.5' in mercado:
        return 'won' if total_goals <= 1 else 'lost'
    elif 'under 2.5' in mercado:
        return 'won' if total_goals <= 2 else 'lost'
    elif 'under 3.5' in mercado:
        return 'won' if total_goals <= 3 else 'lost'
    
    # BTTS (ambos marcan)
    elif 'btts' in mercado and 'sí' in mercado:
        return 'won' if home_goals >= 1 and away_goals >= 1 else 'lost'
    elif 'btts' in mercado and 'no' in mercado:
        return 'won' if home_goals == 0 or away_goals == 0 else 'lost'
    
    # 1X2
    elif '1x2' in mercado:
        if home_goals > away_goals:
            # Ganó local
            if pick['mercado'].lower().endswith('vs ' + pick['partido'].split(' vs ')[0]):
                return 'won'  # Apostó al local
            else:
                return 'lost'
        elif home_goals < away_goals:
            # Ganó visitante
            if pick['mercado'].lower().endswith('vs ' + pick['partido'].split(' vs ')[1]):
                return 'won'  # Apostó al visitante
            else:
                return 'lost'
        else:
            # Empate
            if 'empate' in pick['mercado'].lower():
                return 'won'
            else:
                return 'lost'
    
    # Doble oportunidad
    elif 'doble oportunidad' in mercado or '1x' in mercado or 'x2' in mercado or '12' in mercado:
        if home_goals > away_goals:
            return 'won' if '1x' in mercado or '12' in mercado else 'lost'
        elif home_goals < away_goals:
            return 'won' if 'x2' in mercado or '12' in mercado else 'lost'
        else:  # empate
            return 'won' if '1x' in mercado or 'x2' in mercado else 'lost'
    
    # Mercado no reconocido
    else:
        logger.warning(f"⚠️ Mercado no reconocido: {pick['mercado']}")
        return 'void'


def main():
    logger.info("🚀 Iniciando liquidación masiva de picks pendientes...")
    
    tracker = StatsTracker()
    if not tracker.enabled:
        logger.error("❌ Supabase no configurado")
        return 1
    
    # Obtener picks pendientes
    all_picks = tracker.get_all_picks()
    pending = [p for p in all_picks if p.get('status') == 'pending']
    
    if not pending:
        logger.info("✅ No hay picks pendientes")
        send_telegram("✅ <b>Liquidación masiva</b>\n\nNo hay picks pendientes de liquidar.")
        return 0
    
    logger.info(f"📋 {len(pending)} picks pendientes encontrados")
    
    settled_count = 0
    won_count = 0
    lost_count = 0
    void_count = 0
    not_found_count = 0
    
    for pick in pending:
        partido = pick.get('partido', '')
        if ' vs ' not in partido:
            logger.warning(f"⚠️ Formato de partido inválido: {partido}")
            continue
        
        home_team, away_team = partido.split(' vs ', 1)
        hora = pick.get('hora', '')
        
        # Extraer fecha de 'hora' (formato: 'DD/MM HH:MM')
        try:
            if '/' in hora:
                date_part = hora.split()[0]  # 'DD/MM'
                year = datetime.now().year
                match_date = f"{year}-{date_part.split('/')[1]}-{date_part.split('/')[0]}"
            else:
                logger.warning(f"⚠️ Formato de hora inválido: {hora}")
                continue
        except Exception as e:
            logger.warning(f"⚠️ Error parseando fecha {hora}: {e}")
            continue
        
        # Consultar resultado
        result = get_match_result(home_team, away_team, match_date)
        
        if result is None:
            logger.info(f"⏳ {partido} ({match_date}): resultado no encontrado (¿partido futuro?)")
            not_found_count += 1
            continue
        
        # Evaluar pick
        status = evaluate_pick(pick, result)
        
        # Actualizar en Supabase
        tracker.settle_pick(pick['id'], status)
        settled_count += 1
        
        if status == 'won':
            won_count += 1
            logger.info(f"✅ {partido}: WON ({result['home_score']}-{result['away_score']})")
        elif status == 'lost':
            lost_count += 1
            logger.info(f"❌ {partido}: LOST ({result['home_score']}-{result['away_score']})")
        else:
            void_count += 1
            logger.info(f"➖ {partido}: VOID")
    
    # Enviar resumen
    summary = (
        f"🤖 <b>LIQUIDACIÓN MASIVA COMPLETADA</b>\n\n"
        f"📊 Picks procesados: {settled_count}\n"
        f"✅ Aciertos: {won_count}\n"
        f"❌ Fallos: {lost_count}\n"
        f"➖ Anulados: {void_count}\n"
        f"⏳ No encontrados: {not_found_count}\n\n"
        f"💡 Usa /stats para ver el rendimiento actualizado."
    )
    
    send_telegram(summary)
    logger.info(f"✅ Liquidación completada: {settled_count} picks procesados")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
