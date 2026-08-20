#!/usr/bin/env python3
"""
Limpieza de pendientes: marca como void los picks pendientes más antiguos
de CLEANUP_DAYS días (partidos ya jugados en ligas sin cobertura gratuita
de resultados). Los recientes/futuros quedan para el cron diario.
Los void NO computan en PnL ni hit rate.
"""
import os
import sys
import logging
import requests
from datetime import datetime, timezone, timedelta

from stats_tracker import StatsTracker

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

DAYS = int(os.getenv('CLEANUP_DAYS', '3'))


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10)
    except Exception as e:
        logger.error(f"❌ Telegram: {e}")


def parse_pick_date(hora):
    try:
        date_part = hora.split()[0]
        day, month = date_part.split('/')
        today = datetime.now(timezone.utc).date()
        d = datetime(today.year, int(month), int(day)).date()
        if d > today + timedelta(days=1):
            d = datetime(today.year - 1, int(month), int(day)).date()
        return d
    except Exception:
        return None


def main():
    logger.info(f"🧹 Iniciando limpieza de pendientes (>{DAYS} días → void)...")

    tracker = StatsTracker()
    if not tracker.enabled:
        logger.error("❌ Supabase no configurado")
        return 1

    pending = [p for p in tracker.get_all_picks() if p.get('status') == 'pending']
    if not pending:
        logger.info("✅ No hay picks pendientes")
        return 0

    today = datetime.now(timezone.utc).date()
    to_void = []
    for p in pending:
        d = parse_pick_date(p.get('hora', ''))
        if d and (today - d).days > DAYS:
            to_void.append(p)

    if not to_void:
        logger.info(f"✅ Nada que limpiar: ningún pendiente >{DAYS} días")
        send_telegram(f"🧹 <b>Limpieza de pendientes</b>\n\nNada que anular (ningún pendiente >{DAYS} días).")
        return 0

    leagues = set()
    for p in to_void:
        tracker.settle_pick(p['id'], 'void')
        leagues.add(p.get('liga', '?'))
        logger.info(f"➖ #{p['id']} {p['partido']} · {p['mercado']} → void")

    remaining = len(pending) - len(to_void)
    summary = (f"🧹 <b>LIMPIEZA DE PENDIENTES</b>\n\n"
               f"➖ Anulados: <b>{len(to_void)}</b> picks (ligas sin cobertura de resultados)\n"
               f"⏳ Siguen pendientes: {remaining} (futuros o recientes)\n\n"
               f"📋 Ligas anuladas: {', '.join(sorted(leagues)[:8])}"
               f"{'…' if len(leagues) > 8 else ''}\n\n"
               f"ℹ️ Los void no computan en PnL ni hit rate.")
    send_telegram(summary)
    logger.info(f"✅ Limpieza completada: {len(to_void)} void, {remaining} pendientes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
