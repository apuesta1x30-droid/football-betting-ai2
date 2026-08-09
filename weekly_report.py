#!/usr/bin/env python3
"""Resumen semanal automático (cron: domingos 22:00 UTC)."""
import os
import sys
import logging
import requests
from stats_tracker import StatsTracker
from reports import compute_for, picks_settled_since, fmt_weekly

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')


def send(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": message,
              "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=10
    )
    return r.status_code == 200


def main():
    tracker = StatsTracker()
    if not tracker.enabled:
        logger.error("❌ Supabase no configurado")
        return 1
    week = picks_settled_since(tracker, 7)
    msg = fmt_weekly(week, compute_for(tracker.get_all_picks()))
    ok = send(msg)
    logger.info("✅ Resumen semanal enviado" if ok else "❌ No se pudo enviar")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
