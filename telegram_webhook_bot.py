#!/usr/bin/env python3
"""
Bot de comandos de Telegram por WEBHOOK (microservicio Render).
Comandos: /stats /week /today /pending /market /scan /app /glosario /help
Además: reacciones 👌 (won) / 👎 (lost) sobre las alertas = liquidación manual.
Solo responde al chat TELEGRAM_CHAT_ID (seguridad).
"""
import os
import logging
import requests
import unicodedata
import threading
from datetime import datetime, timezone
from flask import Flask, request, jsonify

from stats_tracker import StatsTracker
from reports import compute_for, fmt_stats, fmt_weekly, picks_settled_since

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
CHAT_ID = str(os.getenv('TELEGRAM_CHAT_ID', ''))
APP_URL = "https://football-betting-ai2-xay2ankt3xzaecxpbu6nwf.streamlit.app/"

app = Flask(__name__)

COMMANDS = [
    {"command": "stats", "description": "📊 Rendimiento acumulado"},
    {"command": "week", "description": "🗓️ Resumen últimos 7 días"},
    {"command": "today", "description": "📅 Picks de hoy"},
    {"command": "pending", "description": "⏳ Pendientes de liquidar"},
    {"command": "market", "description": "🎯 Rendimiento por mercado"},
    {"command": "scan", "description": "🔄 Lanzar escaneo ahora"},
    {"command": "app", "description": "🌐 Abrir dashboard"},
    {"command": "glosario", "description": "📚 Glosario de métricas"},
    {"command": "help", "description": "❓ Menú de comandos"},
]

HELP = """
🤖 <b>Comandos disponibles</b>

/stats → rendimiento acumulado
/week → resumen de los últimos 7 días
/today → picks registrados hoy
/pending → picks pendientes de liquidar
/market BTTS → rendimiento de un mercado
/market → lista de mercados con datos
/scan → lanzar un escaneo manual ahora
/app → abrir el dashboard
/glosario → entender cada métrica
/help → este menú

🤙 <b>Liquidación rápida</b>: reacciona a una alerta con
👌 = acertada (WON) · 👎 = fallada (LOST)
""".strip()

GLOSARIO = """📚 <b>GLOSARIO DE MÉTRICAS</b>

🔎 <b>Liquidados (✅/❌)</b> · Picks cerrados. ✅ acierto, ❌ fallo.

🎯 <b>Hit rate</b> · % aciertos. Favorable: >1/cuota media. Con cuota 2.0 necesitas >50%; con 1.5 necesitas >67%.

💰 <b>PnL (u)</b> · Beneficio con stake=1. Favorable: >0.

📈 <b>Yield</b> · PnL/apuestas = rentabilidad real. >5% bueno, >10% excelente (raro de sostener). >20% en pocas semanas suele ser varianza.

🏆 <b>Mejor/peor mercado</b> · Mercado con más/menos PnL. Útil con meses de datos.

🔻 <b>CLV (Closing Line Value)</b> · Tu cuota vs cuota de cierre. Favorable: >0 (batiste al mercado). Negativo: el mercado cerró por encima.

🎯 <b>Bate al cierre</b> · % picks que batieron cierre. Favorable: >50%.

🧠 <b>PnL+CLV juntos</b>:
• PnL>0 y CLV>0 → edge real ✅
• PnL>0 y CLV<0 → ganas sin batir mercado: posible suerte, vigila ⚠️
• PnL<0 y CLV>0 → pierdes pero eliges bien: señal positiva a largo 🟢

🎲 <b>Brier</b> (en /stats) · Error de probabilidades IA. Favorable: <0.20 bueno, <0.15 muy bueno.

⚖️ <b>Gap de calibración</b> (en /stats) · Prob IA prometida − acierto real (pp). Favorable: entre −5 y +5. >+10: IA sobreestima. <−10: IA conservadora.
"""
def _norm(s):
    s = unicodedata.normalize('NFKD', s or '')
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return s.lower().strip()


def tg(method, **data):
    try:
        return requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
                             json=data, timeout=10)
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        return None


def _run_scan_async():
    try:
        from auto_scan import scan_value_bets
        scan_value_bets()
    except Exception as e:
        logger.error(f"❌ Error en escaneo manual: {e}")


def handle_reaction(reaction):
    """👌 = won · 👎 = lost, sobre el pick asociado al mensaje reaccionado.
    Siempre confirma el resultado de la reacción para que sepas qué pasó."""
    msg_id = reaction.get('message_id')
    new = reaction.get('new_reaction') or []
    emojis = [r.get('emoji') for r in new if r.get('type') == 'emoji']
    status = 'won' if '👌' in emojis else ('lost' if '👎' in emojis else None)
    if not status or not msg_id:
        return  # reacción distinta a 👌/ (p. ej. ): no hacer nada
    
    tracker = StatsTracker()
    if not tracker.enabled:
        return
    resp = tracker.client.table(tracker.table).select('*').eq('telegram_message_id', msg_id).execute()
    if not resp.data:
        tg('sendMessage', chat_id=CHAT_ID,
           text="⚠️ <b>Alerta sin identificador</b>\nEste mensaje es anterior al sistema de reacciones. "
                "El cron lo liquidará con el resultado real de API-Football.",
           parse_mode='HTML')
        return
    
    pick = resp.data[0]
    
    if pick['status'] != 'pending':
        emoji = {'won': '✅', 'lost': '❌', 'void': '➖'}.get(pick['status'], '❓')
        tg('sendMessage', chat_id=CHAT_ID,
           text=f"ℹ️ <b>Ya liquidado por resultado real</b>\n{pick['partido']} · {pick['mercado']} "
                f"→ {emoji} {pick['status'].upper()}\n\nTu reacción no lo modifica: el dato real manda.",
           parse_mode='HTML')
        return
    
    tracker.settle_pick(pick['id'], status)
    emoji = '✅' if status == 'won' else '❌'
    logger.info(f"🤝 Reacción → #{pick['id']} {pick['partido']} = {status}")
    tg('sendMessage', chat_id=CHAT_ID,
       text=f"📝 <b>Liquidado manualmente</b>\n{pick['partido']} · {pick['mercado']} → {emoji} {status.upper()}",
       parse_mode='HTML')


@app.route('/')
def health():
    return jsonify(ok=True, service="valuebets-bot")


@app.route('/telegram', methods=['POST'])
def telegram():
    update = request.get_json(silent=True) or {}
    
    # 1) Reacciones a mensajes del bot (liquidación manual)
    reaction = update.get('message_reaction')
    if reaction:
        if str(((reaction.get('user') or {}).get('id', ''))) == CHAT_ID:
            handle_reaction(reaction)
        return jsonify(ok=True)
    
    # 2) Mensajes de texto con comandos
    message = update.get('message') or update.get('edited_message') or {}
    chat_id = str((message.get('chat') or {}).get('id', ''))
    text = (message.get('text') or '').strip()
    
    logger.info(f"📨 Recibido: chat_id={chat_id}, text={text}, CHAT_ID={CHAT_ID}")
    
    if chat_id != CHAT_ID or not text.startswith('/'):
        return jsonify(ok=True)
    
    cmd = text.split()[0].lower().split('@')[0]
    
    if cmd == '/app':
        tg('sendMessage', chat_id=CHAT_ID,
           text="🌐 <b>Dashboard Value Bet Scanner</b>", parse_mode='HTML',
           reply_markup={"inline_keyboard": [[{"text": "🌐 Abrir scanner", "url": APP_URL}]]})
        return jsonify(ok=True)
    
    if cmd == '/scan':
        threading.Thread(target=_run_scan_async, daemon=True).start()
        tg('sendMessage', chat_id=CHAT_ID,
           text="🔄 <b>Escaneo manual iniciado</b>\n\nTe envío el resumen y las mejores apuestas en ~1 minuto.",
           parse_mode='HTML')
        return jsonify(ok=True)
    
    reply = handle(text)
    if reply:
        tg('sendMessage', chat_id=CHAT_ID, text=reply, parse_mode='HTML')
    return jsonify(ok=True)


def handle(text):
    parts = text.split()
    cmd = parts[0].lower().split('@')[0]
    
    if cmd in ('/start', '/help'):
        return HELP
    
    if cmd == '/glosario':
        tg('sendMessage', chat_id=CHAT_ID,
           text="📚 <b>Glosario de métricas</b>\n\nExplicación detallada de cada indicador, qué valores son favorables y cómo leerlos juntos.",
           parse_mode='HTML',
           reply_markup={"inline_keyboard": [[{"text": "📖 Abrir glosario completo", "url": APP_URL + "?glosario=1"}]]})
        return None
    
    tracker = StatsTracker()
    if not tracker.enabled:
        return "⚠️ Estadísticas no configuradas (Supabase)"
    
    all_picks = tracker.get_all_picks()
    
    if cmd == '/stats':
        return fmt_stats(compute_for(all_picks), "RENDIMIENTO ACUMULADO")
    
    if cmd == '/week':
        return fmt_weekly(picks_settled_since(tracker, 7), compute_for(all_picks))
    
    if cmd == '/today':
        today = datetime.now(timezone.utc).date().isoformat()
        todays = [p for p in all_picks if (p.get('timestamp') or '').startswith(today)]
        if not todays:
            return "💤 Hoy no se han registrado picks todavía."
        L = [f"📅 <b>Picks de hoy ({len(todays)})</b>", ""]
        for p in todays[:15]:
            st_ = {'pending': '⏳', 'won': '✅', 'lost': '❌', 'void': '➖'}.get(p['status'], '⏳')
            cuota = p['cuota'] or 0
            ev = p['ev_percentage'] or 0
            L.append(f"{st_} {p['partido']}\n   {p['mercado']} @ {cuota:.2f} · EV {ev:+.1f}%")
        return "\n".join(L)
    
    if cmd == '/pending':
        pend = [p for p in all_picks if p['status'] == 'pending']
        if not pend:
            return "✅ No hay picks pendientes de liquidar."
        L = [f"⏳ <b>Pendientes de liquidar ({len(pend)})</b>", ""]
        for p in pend[:15]:
            cuota = p['cuota'] or 0
            ev = p['ev_percentage'] or 0
            L.append(f"⏳ {p['partido']}\n   {p['mercado']} @ {cuota:.2f} · EV {ev:+.1f}% · {p['hora']}")
        if len(pend) > 15:
            L.append(f"… y {len(pend) - 15} más")
        return "\n".join(L)
    
    if cmd == '/market':
        q = _norm(' '.join(parts[1:])) if len(parts) > 1 else ''
        if not q:
            mkts = sorted({(p['mercado'] or '') for p in all_picks if p['mercado']})
            return "📊 <b>Mercados con datos</b>\n" + "\n".join(f"· {m}" for m in mkts)
        sel = [p for p in all_picks if q in _norm(p['mercado'])]
        if not sel:
            return f"🤷 Sin picks para «{' '.join(parts[1:])}»"
        return fmt_stats(compute_for(sel), f"MERCADO: {' '.join(parts[1:]).upper()}")
    
    return HELP


def _startup():
    base = os.getenv('WEBHOOK_URL', '').strip()
    if not base:
        ext = os.getenv('RENDER_EXTERNAL_URL', '').strip().rstrip('/')
        if ext:
            base = f"{ext}/telegram"
    if base and BOT_TOKEN:
        r = tg('setWebhook', url=base, allowed_updates=["message", "message_reaction"])
        logger.info(f"🔗 Webhook: {base} → HTTP {r.status_code if r is not None else 'ERROR'}")
    if BOT_TOKEN:
        r = tg('setMyCommands', commands=COMMANDS)
        logger.info(f"📋 Botón Menú registrado → HTTP {r.status_code if r is not None else 'ERROR'}")


threading.Thread(target=_startup, daemon=True).start()
