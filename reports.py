"""
Informes de rendimiento para Telegram (v0.3 + v0.4-D CLV).
Funciones compartidas por el bot de comandos y el resumen semanal.
"""
from datetime import datetime, timedelta, timezone


def compute_for(picks):
    """Métricas sobre una lista de picks (dicts de Supabase)."""
    settled = [p for p in picks if p['status'] in ('won', 'lost')]
    n = len(settled)
    wins = sum(1 for p in settled if p['status'] == 'won')
    losses = n - wins
    pnl = sum(((p['cuota'] or 0) - 1) if p['status'] == 'won' else -1 for p in settled)
    hit = (wins / n * 100) if n else 0.0
    yld = (pnl / n * 100) if n else 0.0
    evs = [p['ev_percentage'] for p in settled if p['ev_percentage'] is not None]
    bs = [(p['prob_ia'] - (1 if p['status'] == 'won' else 0)) ** 2
          for p in settled if p['prob_ia'] is not None]

    # CLV: no requiere liquidación, solo cuota tomada y de cierre
    clvs = [((p['cuota'] / p['closing_odds']) - 1) * 100
            for p in picks if p.get('closing_odds') and p.get('cuota')]
    beat = sum(1 for c in clvs if c > 0)

    return {
        'total': len(picks),
        'settled': n,
        'wins': wins,
        'losses': losses,
        'pending': len(picks) - n,
        'pnl': pnl,
        'yield_pct': yld,
        'hit': hit,
        'brier': (sum(bs) / len(bs)) if bs else None,
        'gap': ((sum(evs) / len(evs)) - yld) if evs else None,
        'clv_n': len(clvs),
        'avg_clv': (sum(clvs) / len(clvs)) if clvs else None,
        'beat_close': (beat / len(clvs) * 100) if clvs else None,
    }


def picks_settled_since(tracker, days):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for p in tracker.get_all_picks():
        if p['status'] not in ('won', 'lost') or not p.get('settled_at'):
            continue
        try:
            dt = datetime.fromisoformat(p['settled_at'])
        except Exception:
            continue
        if dt >= cutoff:
            out.append(p)
    return out


def market_pnl(picks):
    agg = {}
    for p in picks:
        m = p['mercado'] or 'Desconocido'
        agg[m] = agg.get(m, 0.0) + (((p['cuota'] or 0) - 1) if p['status'] == 'won' else -1)
    return agg

def interpret_stats(stats):
    """Genera una interpretación textual del rendimiento."""
    lines = []
    
    # Lectura correcta de claves (coinciden con compute_for)
    hit = stats.get('hit', 0)
    pnl = stats.get('pnl', 0)
    yield_val = stats.get('yield_pct', 0)
    gap = stats.get('gap')
    clv = stats.get('avg_clv')
    brier = stats.get('brier')
    settled = stats.get('settled', 0)
    
    # Hit rate vs equilibrio (asumiendo cuota media ~2.0)
    if hit < 40:
        lines.append("🔻 <b>Hit rate bajo</b> para la cuota media del sistema (≈2.0). "
                     "Necesitarías acertar ≥50% para estar en equilibrio.")
    elif hit < 50:
        lines.append("⚠️ Hit rate justo en el límite de equilibrio. "
                     "Cualquier racha negativa te pone en rojo.")
    else:
        lines.append("✅ Hit rate sano, por encima del punto de equilibrio.")
    
    # PnL/Yield
    if yield_val < -10:
        lines.append(f"📉 <b>Pérdidas sostenidas</b>: {pnl:+.1f} u ({yield_val:+.1f}% yield). "
                     "El sistema está por debajo del punto de equilibrio.")
    elif yield_val < 0:
        lines.append(f"⚠️ Pérdidas moderadas: {pnl:+.1f} u. Vigilar si persiste.")
    else:
        lines.append(f"✅ Beneficio real: {pnl:+.1f} u ({yield_val:+.1f}% yield).")
    
    # Gap de calibración (la pieza clave)
    if gap is not None:
        if gap > 10:
            lines.append(f"🚨 <b>Gap +{gap:.0f} pp</b>: el modelo sobreestima sistemáticamente. "
                         "Modo seguridad activo y Capa B corrigiendo probabilidades.")
        elif gap > 5:
            lines.append(f"⚠️ Gap +{gap:.0f} pp: ligera sobreestimación. "
                         "Auto-ajuste exigiendo más EV.")
        elif gap < -5:
            lines.append(f"✅ Gap {gap:+.0f} pp: modelo conservador, rinde más de lo que anuncia.")
        else:
            lines.append(f"✅ Gap {gap:+.0f} pp: modelo bien calibrado.")
    
    # CLV (validación del mercado)
    if clv is not None and settled >= 20:
        if clv < -2:
            lines.append("🔻 CLV negativo: el mercado no valida tus señales. "
                         "Posible ruido del modelo.")
        elif clv > 0:
            lines.append("✅ CLV positivo: bates al cierre. Señal de edge real.")
        else:
            lines.append("⚠️ CLV cercano a 0: ni bates ni pierdes al cierre.")
    
    # Brier
    if brier is not None:
        if brier > 0.25:
            lines.append("📊 Predicciones de baja calidad (Brier alto).")
        elif brier > 0.20:
            lines.append("📊 Predicciones aceptables, margen de mejora.")
        else:
            lines.append("📊 Buen poder predictivo del modelo.")
    
    # Recomendación final
    if settled >= 50:
        if gap is not None and gap > 10:
            lines.append("💡 <b>Qué hacer ahora</b>: confiar en el modo seguridad. "
                         "No apuestes hasta que el gap baje de +10 (semanas).")
        elif yield_val < -5 and (clv is None or clv < 0):
            lines.append("💡 <b>Qué hacer ahora</b>: sistema en revisión. "
                         "Pausar apuestas reales hasta ver mejora.")
        elif yield_val > 0 and (clv is None or clv > 0):
            lines.append("💡 <b>Qué hacer ahora</b>: sistema validado. Mantener el rumbo.")
        else:
            lines.append("💡 <b>Qué hacer ahora</b>: seguir acumulando muestra antes de juzgar.")
    
    return "\n".join(lines)
    
def fmt_stats(s, title):
    L = [f"📊 <b>{title}</b>", ""]
    L.append(f"🔎 Picks: <b>{s['total']}</b>  (✅ {s['wins']} · ❌ {s['losses']} · ⏳ {s['pending']})")
    if s['settled']:
        L.append(f"🎯 Hit rate: <b>{s['hit']:.1f}%</b>")
        L.append(f"💰 PnL: <b>{s['pnl']:+.2f} u</b> · 📈 Yield: <b>{s['yield_pct']:+.1f}%</b>")
        if s['brier'] is not None:
            L.append(f"🎲 Brier: {s['brier']:.3f}")
        if s['gap'] is not None:
            L.append(f"⚖️ Calibración IA−real: {s['gap']:+.1f} pp")
    else:
        L.append("⏳ Aún sin picks liquidados.")
    if s.get('avg_clv') is not None:
        L.append(f"🔻 CLV medio: <b>{s['avg_clv']:+.1f}%</b> ({s['clv_n']} picks) "
                 f"· bate al cierre: <b>{s['beat_close']:.0f}%</b>")
    L.append("")
    L.append("🧠 <b>Interpretación</b>")
    L.append(interpret_stats(s))
    return "\n".join(L)


def fmt_weekly(week_picks, all_stats):
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=7)
    s = compute_for(week_picks)
    L = ["🗓️ <b>RESUMEN SEMANAL</b>", f"📅 {since:%d/%m} – {now:%d/%m}", ""]
    if not week_picks:
        L.append("💤 Sin picks liquidados esta semana.")
    else:
        L.append(f"🔎 Liquidados: <b>{s['settled']}</b>  (✅ {s['wins']} · ❌ {s['losses']})")
        L.append(f"🎯 Hit rate: <b>{s['hit']:.1f}%</b>")
        L.append(f"💰 PnL semana: <b>{s['pnl']:+.2f} u</b> · 📈 Yield: <b>{s['yield_pct']:+.1f}%</b>")
        agg = market_pnl(week_picks)
        if agg:
            bm = max(agg, key=agg.get)
            wm = min(agg, key=agg.get)
            L.append(f"🏆 Mejor mercado: <b>{bm}</b> ({agg[bm]:+.2f} u)")
            L.append(f"📉 Peor mercado: <b>{wm}</b> ({agg[wm]:+.2f} u)")
        if s.get('avg_clv') is not None:
            L.append(f"🔻 CLV semana: <b>{s['avg_clv']:+.1f}%</b> · bate al cierre: {s['beat_close']:.0f}%")
    L.append("")
    L.append(f"💼 Acumulado: PnL <b>{all_stats['pnl']:+.2f} u</b> · Yield <b>{all_stats['yield_pct']:+.1f}%</b>")
    if all_stats.get('avg_clv') is not None:
        L.append(f"🔻 CLV acumulado: <b>{all_stats['avg_clv']:+.1f}%</b>")
    L.append("")
    L.append("💡 Usa /glosario para entender cada métrica.")
    return "\n".join(L)
