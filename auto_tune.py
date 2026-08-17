#!/usr/bin/env python3
"""
v0.5-A · Auto-ajuste operativo.
Lee los picks liquidados, mide el gap de calibración (EV declarado − yield real)
y escribe en Supabase (tabla meta, clave 'auto_tune') la configuración que
auto_scan debe aplicar: umbral de EV de notificación y fracción Kelly.

Reglas:
- gap > +10 pp  → sobreestima  → sube EV mínimo, baja Kelly (1/8)
- gap +5..+10   → leve         → EV 11%, Kelly 1/6
- gap -5..+5    → calibrado    → EV 10%, Kelly 1/4 (default)
- gap < -5      → conservador  → baja EV mínimo, Kelly 1/2
Avisa por Telegram del cambio.
"""
import os
import sys
import json
import logging
import requests
from datetime import datetime, timezone

from stats_tracker import StatsTracker
from reports import compute_for

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

MIN_SAMPLE = 20
META_KEY = 'auto_tune'

DEFAULTS = {'ev_notify': 10.0, 'kelly': 4}


def send(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10)
        return r.status_code == 200
    except Exception as e:
        logger.error(f"❌ Telegram: {e}")
        return False


def decide(gap):
    if gap > 10:
        return {'ev_notify': 12.0, 'kelly': 8}, "sobreestimación fuerte"
    if gap > 5:
        return {'ev_notify': 11.0, 'kelly': 6}, "sobreestimación leve"
    if gap < -5:
        return {'ev_notify': 6.0, 'kelly': 2}, "modo conservador"
    return dict(DEFAULTS), "calibrado"


def main():
    tracker = StatsTracker()
    if not tracker.enabled:
        logger.error("❌ Supabase no configurado")
        return 1

    s = compute_for(tracker.get_all_picks())
    if s['settled'] < MIN_SAMPLE or s['gap'] is None:
        logger.info(f"Muestra insuficiente ({s['settled']}/{MIN_SAMPLE}) o sin gap. "
                    f"Mantengo defaults.")
        return 0

    gap = s['gap']
    new_cfg, motivo = decide(gap)

    # Leer configuración previa para detectar cambio
    prev = dict(DEFAULTS)
    try:
        resp = tracker.client.table('meta').select('value').eq('key', META_KEY).execute()
        if resp.data:
            prev = json.loads(resp.data[0]['value']).get('cfg', prev)
    except Exception as e:
        logger.debug(f"No hay config previa: {e}")

    changed = (prev.get('ev_notify') != new_cfg['ev_notify']) or (prev.get('kelly') != new_cfg['kelly'])

    payload = {
        'key': META_KEY,
        'value': json.dumps({
            'cfg': new_cfg,
            'gap': round(gap, 1),
            'n': s['settled'],
            'updated_at': datetime.now(timezone.utc).isoformat(),
        })
    }
    tracker.client.table('meta').upsert(payload, on_conflict='key').execute()
    logger.info(f"🤖 Auto-ajuste guardado: EV≥{new_cfg['ev_notify']}% Kelly 1/{new_cfg['kelly']} "
                f"(gap {gap:+.1f} pp, n={s['settled']})")

    if changed:
        # Interpretación del cambio para el usuario
        if new_cfg['ev_notify'] > prev.get('ev_notify', 10):
            lectura = "📈 IA más afinada: subo el listón de calidad y protejo banca"
        elif new_cfg['ev_notify'] < prev.get('ev_notify', 10):
            lectura = "📉 IA conservadora: bajo el listón para no perder oportunidades"
        else:
            lectura = "🔄 Ajuste de Kelly según calibración detectada"
        
        msg = (f"🤖 <b>AUTO-AJUSTE DEL SISTEMA</b>\n\n"
               f"⚖️ Gap: <b>{gap:+.1f} pp</b> ({motivo})\n"
               f"🎯 EV mínimo: {prev.get('ev_notify', 10):.0f}% → <b>{new_cfg['ev_notify']:.0f}%</b>\n"
               f"💰 Kelly: 1/{prev.get('kelly', 4)} → <b>1/{new_cfg['kelly']}</b>\n"
               f"📊 Muestra: {s['settled']} liquidados\n\n"
               f"<i>{lectura}</i>\n\n"
               f"ℹ️ Más info: /glosario")
        send(msg)
        logger.info("📨 Aviso de auto-ajuste enviado")
    else:
        logger.info("Sin cambios de configuración respecto al ajuste anterior.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
