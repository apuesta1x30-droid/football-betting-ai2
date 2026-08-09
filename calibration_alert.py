#!/usr/bin/env python3
"""
v0.4-E · Alerta de calibración IA vs resultados reales.
Compara la Prob. IA media con el hit rate real de los picks liquidados
y avisa por Telegram si el gap supera el umbral.

Reglas:
- Muestra mínima: MIN_SAMPLE picks liquidados
- Alerta si |gap| > THRESHOLD_PP (puntos porcentuales)
- Tras una alerta, no repite hasta CHECK_EVERY picks liquidados nuevos
"""
import os
import sys
import logging
import argparse
import requests
from stats_tracker import StatsTracker

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

MIN_SAMPLE = 20        # mínimos picks liquidados para evaluar
THRESHOLD_PP = 10.0    # umbral de alerta (puntos porcentuales)
CHECK_EVERY = 10       # tras una alerta, esperar este nº de picks nuevos
META_KEY = 'calibration_last_alert_n'


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


def calibration_metrics(picks):
    settled = [p for p in picks if p['status'] in ('won', 'lost')]
    n = len(settled)
    if not n:
        return None
    wins = sum(1 for p in settled if p['status'] == 'won')
    hit = wins / n
    pias = [p['prob_ia'] for p in settled if p['prob_ia'] is not None]
    if not pias:
        return None
    avg_pia = sum(pias) / len(pias)
    brier = sum((p['prob_ia'] - (1 if p['status'] == 'won' else 0)) ** 2
                for p in settled if p['prob_ia'] is not None) / len(pias)
    return {'n': n, 'hit': hit, 'avg_pia': avg_pia,
            'gap_pp': (avg_pia - hit) * 100, 'brier': brier}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--test', action='store_true',
                    help='Envía el estado actual como info, sin umbrales')
    args = ap.parse_args()

    tracker = StatsTracker()
    if not tracker.enabled:
        logger.error("❌ Supabase no configurado")
        return 1

    m = calibration_metrics(tracker.get_all_picks())

    # Modo test: siempre envía mensaje para verificar el circuito
    if args.test:
        if m is None:
            msg = ("🧪 <b>TEST CALIBRACIÓN</b>\n\n⏳ Aún sin picks liquidados.\n"
                   "Sistema listo: verás tus métricas aquí en cuanto haya liquidaciones.")
        else:
            msg = (f"🧪 <b>TEST CALIBRACIÓN</b>\n\n🔎 Muestra: <b>{m['n']}</b> liquidados\n"
                   f"🎯 Hit rate real: <b>{m['hit']*100:.1f}%</b>\n"
                   f"🤖 Prob. IA media: <b>{m['avg_pia']*100:.1f}%</b>\n"
                   f"⚖️ Gap: <b>{m['gap_pp']:+.1f} pp</b>\n🎲 Brier: {m['brier']:.3f}")
        ok = send(msg)
        logger.info("📨 Test enviado" if ok else "❌ Test no enviado")
        return 0 if ok else 1

    if m is None:
        logger.info("Sin datos liquidados para calibración")
        return 0

    logger.info(f"📐 n={m['n']} hit={m['hit']*100:.1f}% "
                f"avg_pia={m['avg_pia']*100:.1f}% gap={m['gap_pp']:+.1f} pp")

    if m['n'] < MIN_SAMPLE:
        logger.info(f"Muestra insuficiente ({m['n']}/{MIN_SAMPLE}). Sin evaluar.")
        return 0

    # Anti-spam: leer cuántos liquidados había en la última alerta
    try:
        resp = tracker.client.table('meta').select('value').eq('key', META_KEY).execute()
        last_n = int(resp.data[0]['value']) if resp.data else 0
    except Exception as e:
        logger.error(f"Error leyendo meta: {e}")
        last_n = 0

    if last_n and (m['n'] - last_n) < CHECK_EVERY:
        logger.info(f"Esperando muestra: {m['n'] - last_n}/{CHECK_EVERY} "
                    f"picks nuevos desde la última alerta")
        return 0

    if abs(m['gap_pp']) <= THRESHOLD_PP:
        logger.info(f"✅ Calibración correcta (gap {m['gap_pp']:+.1f} pp dentro de ±{THRESHOLD_PP})")
        return 0

    if m['gap_pp'] > 0:
        body = (f"⚠️ <b>ALERTA DE CALIBRACIÓN</b>\n\n🔎 Muestra: <b>{m['n']}</b> liquidados\n"
                f"🎯 Hit rate real: <b>{m['hit']*100:.1f}%</b>\n"
                f"🤖 Prob. IA media: <b>{m['avg_pia']*100:.1f}%</b>\n"
                f"⚖️ Gap: <b>{m['gap_pp']:+.1f} pp</b>\n🎲 Brier: {m['brier']:.3f}\n\n"
                "📉 La IA está <b>SOBREESTIMANDO</b> probabilidades.\n"
                "💡 Recomendación: sube el EV mínimo exigido (5–8%) o reduce "
                "stakes hasta que el gap baje de 10 pp.")
    else:
        body = (f"✅ <b>CALIBRACIÓN (modo conservador)</b>\n\n🔎 Muestra: <b>{m['n']}</b> liquidados\n"
                f"🎯 Hit rate real: <b>{m['hit']*100:.1f}%</b>\n"
                f"🤖 Prob. IA media: <b>{m['avg_pia']*100:.1f}%</b>\n"
                f"⚖️ Gap: <b>{m['gap_pp']:+.1f} pp</b>\n🎲 Brier: {m['brier']:.3f}\n\n"
                "📈 La IA entrega <b>más</b> de lo que promete. "
                "Puedes mantener stakes actuales con tranquilidad.")

    if send(body):
        try:
            tracker.client.table('meta').upsert(
                {'key': META_KEY, 'value': str(m['n'])}, on_conflict='key').execute()
        except Exception as e:
            logger.error(f"Error guardando meta: {e}")
        logger.info("📨 Alerta de calibración enviada")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
