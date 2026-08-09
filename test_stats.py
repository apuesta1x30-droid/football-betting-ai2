#!/usr/bin/env python3
"""
Test de integración contra Supabase (tabla picks_test).
Verifica: conexión, registro, deduplicación, liquidación y métricas.
"""
import sys
from stats_tracker import StatsTracker


def main():
    print("=" * 70)
    print("🧪 TEST DE INTEGRACIÓN · Supabase · picks_test")
    print("=" * 70)

    tracker = StatsTracker(table_name="picks_test")

    if not tracker.enabled:
        print("❌ Supabase no configurado. Añade SUPABASE_URL_FB y SUPABASE_KEY_FB")
        print("   en GitHub: Settings → Secrets and variables → Actions")
        sys.exit(1)
    print("✅ Conexión a Supabase OK")

    # Limpieza previa para test idempotente
    tracker.clear_all()
    print("✅ Tabla picks_test limpiada")

    # 1) Registro de 5 picks de prueba
    test_picks = [
        {"Liga": "La Liga", "Partido": "Real Madrid vs Barcelona", "Hora": "09/08 21:00",
         "Mercado": "Over 2.5 Goles", "Cuota": 1.85, "Prob. IA": 0.65, "Prob. Casa": 0.54,
         "EV (%)": 20.25, "Fuente": "API-Football"},
        {"Liga": "Premier League", "Partido": "Manchester City vs Arsenal", "Hora": "09/08 17:30",
         "Mercado": "BTTS - Sí (Ambos marcan)", "Cuota": 1.72, "Prob. IA": 0.68, "Prob. Casa": 0.58,
         "EV (%)": 16.96, "Fuente": "API-Football"},
        {"Liga": "Serie A", "Partido": "Inter Milan vs AC Milan", "Hora": "09/08 20:45",
         "Mercado": "1X2 - Inter Milan", "Cuota": 2.10, "Prob. IA": 0.58, "Prob. Casa": 0.48,
         "EV (%)": 21.80, "Fuente": "API-Football"},
        {"Liga": "Bundesliga", "Partido": "Bayern Munich vs Dortmund", "Hora": "09/08 18:30",
         "Mercado": "Over 1.5 Goles", "Cuota": 1.45, "Prob. IA": 0.78, "Prob. Casa": 0.69,
         "EV (%)": 13.10, "Fuente": "Cálculo"},
        {"Liga": "Ligue 1", "Partido": "PSG vs Marseille", "Hora": "09/08 21:00",
         "Mercado": "Doble Oportunidad - 1X", "Cuota": 1.25, "Prob. IA": 0.88, "Prob. Casa": 0.80,
         "EV (%)": 10.00, "Fuente": "API-Football"},
    ]

    registered = sum(1 for p in test_picks if tracker.register_pick(p))
    print(f"📝 Registrados: {registered}/{len(test_picks)}")
    if registered != len(test_picks):
        print("❌ FALLO: no se registraron todos los picks")
        sys.exit(1)

    # 2) Deduplicación: reinsertar el primero no debe crear fila nueva
    tracker.register_pick(test_picks[0])
    all_picks = tracker.get_all_picks()
    print(f"📊 Filas en BD tras reinsertar duplicado: {len(all_picks)}")
    if len(all_picks) != 5:
        print("❌ FALLO: deduplicación no funciona")
        sys.exit(1)
    print("✅ Deduplicación OK")

    # 3) Liquidación: 3 ganados, 1 perdido, 1 pendiente
    pendientes = [p for p in all_picks if p['status'] == 'pending'][:4]
    for pick, status in zip(pendientes, ['won', 'won', 'won', 'lost']):
        if not tracker.settle_pick(pick['id'], status):
            print(f"❌ FALLO liquidando pick {pick['id']}")
            sys.exit(1)
        print(f"  ✓ #{pick['id']} → {status} | {pick['partido']} | {pick['mercado']} @ {pick['cuota']}")

    # 4) Métricas y verificación de valores
    stats = tracker.calculate_stats()
    print("\n📈 MÉTRICAS CALCULADAS:")
    print(f"  Total: {stats['total']} | Liquidados: {stats['settled']} | Pendientes: {stats['pending']}")
    print(f"  Aciertos: {stats['wins']} | Errores: {stats['losses']} | Hit Rate: {stats['hit_rate']:.1f}%")
    print(f"  PnL: {stats['pnl']:+.2f} u | Yield: {stats['yield']:+.1f}%")
    if stats['brier_score'] is not None:
        print(f"  Brier: {stats['brier_score']:.3f} | Gap calibración: {stats['calibration_gap']:+.1f} pp")

    errores = []
    if stats['total'] != 5: errores.append(f"total={stats['total']} (esperado 5)")
    if stats['settled'] != 4: errores.append(f"settled={stats['settled']} (esperado 4)")
    if stats['wins'] != 3: errores.append(f"wins={stats['wins']} (esperado 3)")
    if stats['losses'] != 1: errores.append(f"losses={stats['losses']} (esperado 1)")
    if stats['pending'] != 1: errores.append(f"pending={stats['pending']} (esperado 1)")
    if abs(stats['hit_rate'] - 75.0) > 0.1: errores.append(f"hit_rate={stats['hit_rate']} (esperado 75)")

    if errores:
        print("\n❌ FALLO DE VERIFICACIÓN:")
        for e in errores:
            print(f"  - {e}")
        sys.exit(1)

    # 5) Agrupaciones
    mk = tracker.get_stats_by_market()
    lg = tracker.get_stats_by_league()
    print(f"\n📊 Mercados con datos: {len(mk)} | Ligas con datos: {len(lg)}")
    if not mk or not lg:
        print("❌ FALLO: agrupaciones vacías")
        sys.exit(1)

    print("\n" + "=" * 70)
    print("✅ TEST DE INTEGRACIÓN COMPLETADO · Supabase operativo")
    print("   Revisa los datos en: Supabase → Table Editor → picks_test")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
