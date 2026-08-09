# ⚽ football-betting-ai2 · Value Bets + Sistema de Rendimiento

Detección, registro, liquidación y análisis de value bets de fútbol multi-mercado.
IA (XGBoost) + cuotas reales + seguimiento completo del rendimiento.

## 🧩 Componentes

| Componente | Archivo | Dónde corre | Función |
|---|---|---|---|
| Escáner | `auto_scan.py` | GitHub Actions · 04:15 y 14:15 UTC | Detecta value bets, envía alertas Telegram y registra picks |
| Liquidación | `settle_picks.py` | GitHub Actions · 07:15 y 13:15 UTC | Liquida con resultados REALES (API-Football) y envía resumen |
| Cierre (CLV) | `closing_odds.py` | GitHub Actions · 08:00, 14:00, 20:00 UTC | Captura cuota de cierre para calcular CLV |
| Calibración | `calibration_alert.py` | GitHub Actions · 13:45 UTC | Alerta si la IA sobreestima probabilidades |
| Semanal | `weekly_report.py` | GitHub Actions · domingo 20:00 UTC | Resumen semanal por Telegram |
| Bot comandos | `telegram_webhook_bot.py` | Render (webhook) | `/stats /week /today /market /help` |
| Dashboard | `app.py` | Render (Streamlit) | Escáner interactivo + histórico + gráficos |

## 🗄️ Base de datos (Supabase, tier gratuito)

- `picks` → historial de picks: cuota, Prob. IA, EV, status, closing_odds…
- `picks_test` → tests de integración (no contamina datos reales)
- `meta` → estado del sistema (anti-spam de alertas, caché de deportes)

## 🔑 Secrets necesarios (GitHub Actions y Render)

`THE_ODDS_API_KEY` · `API_FOOTBALL_KEY` · `TELEGRAM_BOT_TOKEN` ·
`TELEGRAM_CHAT_ID` · `SUPABASE_URL_FB` · `SUPABASE_KEY_FB`

⚠️ Ninguna clave va en el código: solo en Secrets/Environment.

## 📊 Mercados analizados

Over 1.5/2.5/3.5 · 1X2 · BTTS · Doble Oportunidad · Over 0.5 1ª Parte

## 📚 Métricas clave

- **EV** = (Prob. IA × cuota) − 1 → valor esperado de cada pick.
- **Yield** = PnL / apuestas → rentabilidad real por unidad apostada.
- **Hit rate** = % de aciertos.
- **Brier** = precisión de las probabilidades (menor = mejor).
- **CLV** = (cuota tomada / cuota de cierre − 1) → ¿bates al mercado?
  El mejor predictor de rentabilidad a largo plazo.

## 🤖 Comandos de Telegram

`/stats` acumulado · `/week` 7 días · `/today` picks de hoy ·
`/market X` rendimiento por mercado · `/help` menú

## ⚠️ Juego responsable

Herramienta de análisis estadístico. Las apuestas conllevan riesgo.
Apuesta con responsabilidad y gestiona tu banca.
