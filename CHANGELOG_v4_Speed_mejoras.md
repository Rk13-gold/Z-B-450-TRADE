# CHANGELOG v4-Speed — Mejoras 1–7

## Mejora 1 — Filtro de profundidad mínima del book
**Archivos:** `dashboard_gui.py` — `_compute_signal()`
**Descripción:** Si el volumen relativo de un lado del book es < 5% del total,
el depth_imb_pct es < 5% y el ba_ratio está entre 0.95–1.05, se fuerza
NEUTRAL/ESPERAR con régimen `NO_PROFUNDIDAD`.
**Nuevos campos:** `book_depth_bids_volume`, `book_depth_asks_volume`

## Mejora 2 — Confirmación tick-by-tick (últimos 3 segundos)
**Archivos:** `dashboard_gui.py` — `update_battle()`, `_compute_signal()`
**Descripción:** Se añadió un `deque` de 30 posiciones que acumula `tick_speed`
en los últimos ~3s. Si el promedio cae por debajo de 3 t/s, se aplica un
`tick_penalty` de 0.7× al composite. Si además los ticks están en declive
monótono, la penalización se extiende a 0.4×.
**Nuevos campos:** `_tick_history_3s`, `_tick_integrity_score`

## Mejora 3 — Age check + validación del Liquidity Magnet
**Archivos:** `dashboard_gui.py` — `_execute_signal()`, `update_battle()`
**Descripción:** Se almacena el timestamp y precio exacto en que se fija el
magnet. En el momento de ejecutar la señal se verifica que:
- El magnet no tenga más de 120s de antigüedad.
- El precio no se haya desviado más de 0.5% del magnet original.
Si alguna condición falla, la señal se aborta.
**Nuevos campos:** `_magnet_timestamp`, `_magnet_price_at_set`

## Mejora 4 — Filtro de sesión UTC
**Archivos:** `dashboard_gui.py` — `_execute_signal()`
**Descripción:** Se bloquean señales durante las tres transiciones de sesión
(23:55–00:05, 07:55–08:05, 15:55–16:05 UTC). En ventanas de baja liquidez
(00:05–02:30 y 12:00–13:30 UTC) se reduce el multiplicador de posición al 50 %.

## Mejora 5 — Filtro de Funding Rate y Open Interest delta
**Archivos:** `dashboard_gui.py` — `_execute_signal()`; `gemini_brain.py`
**Descripción:** En `_execute_signal` se bloquean señales si:
- `abs(funding_rate) > 0.05%`
- `abs(oi_delta_5min) > 15%`
Se añadieron los campos al schema de Gemini (`GeminiTradingDecision.funding_rate`,
`GeminiTradingDecision.oi_delta_5min`), al `_compact_snapshot` y al prompt
(`_ENGINEER_PROMPT`) con reglas de decisión basadas en ellos.
**Nuevos campos:** `_funding_rate`, `_oi_delta_5min`

## Mejora 6 — SL dinámico basado en estructura del book
**Archivos:** `dashboard_gui.py` — `_execute_signal()`
**Descripción:** Reemplaza el anterior SL por ATR+wall por un SL estructural:
- Si el 2do nivel de muro está a más de 0.15% del precio, SL se coloca detrás
  del 2do nivel (sl = wall_bid + 2 o wall_ask - 2).
- Si está cerca, SL se coloca justo detrás del 1er nivel.
- El TP sigue siendo entry * 1.02 / 0.98.

## Mejora 7 — Cooldown adaptativo post-pérdida
**Archivos:** `src/engine/order_executor.py`
**Descripción:** En `check_position_status()`, al detectar cierre de posición:
- Se consulta `_get_last_trade_outcome()` desde la DB `bb450_trades.db`.
- Si fue SL: se incrementa `_consecutive_sl_count`.
  - 1 SL → cooldown base de 600s (300s × 2).
  - 2+ SL consecutivos → cooldown de 900s.
- Si fue TP: resetea el contador.
- Factor ATR: si `atr > 0.75% del precio`, el cooldown se multiplica hasta 2×.
- Mínimo siempre 300s.
**Nuevos campos:** `_consecutive_sl_count`, `_adaptive_cooldown_base`, `_atr`

## Telegram — Nuevos campos en snapshot y alertas
**Archivos:** `src/telegram_bot.py`
**Descripción:** Se agregaron `tick_integrity_score`, `funding_rate`,
`oi_delta_5min`, `book_depth_bids_volume`, `book_depth_asks_volume` a:
- `_send_signal_alert()` — cuerpo del mensaje
- `_send_brain_alert()` — caption del chart
Se muestran condicionalmente (solo cuando hay valores relevantes).

## Pipeline de datos — Nuevos campos
**Archivos:** `dashboard_gui.py` — `update_signal_data()`, `_build_snapshot()`
**Descripción:** Todos los nuevos campos viajan desde `update_battle()` →
`_build_snapshot()` → `update_signal_data()` → Telegram/execution.
