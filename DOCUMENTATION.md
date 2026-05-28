# BB-450 — Architectural Map

## Project Overview

BB-450 is a **scalping trading bot** for Binance Futures (BTCUSDT) that combines:

- **Real-time order flow analysis** (CVD, Delta, Tick Speed, Whale Walls)
- **Classical technical indicators** (RSI, MACD, Bollinger, ATR, VWAP)
- **PyTorch neural network** (`BrainAgent`) for inference
- **Gemini 2.0 Flash LLM** (`GeminiBrainManager`) for hybrid decision support
- **Telegram alerts & chat** via `TelegramBot`
- **PyQt5 GUI dashboard** (`dashboard_gui.py`) for real-time visual monitoring

---

## 1. File Map & Purpose

| File | Role | Key Classes / Functions |
|------|------|------------------------|
| `dashboard_gui.py` | **PyQt5 UI** — real-time visual dashboard with OpenGL charts, 50+ metrics, order flow terminal, quantum brain panel, Gemini integration | `MainDashboard`, `OrderFlowNumericFeed`, `GalaxyOrderFlowChart`, `BrainInferenceWorker`, `GeminiInferenceWorker` |
| `src/telegram_bot.py` | **Telegram Bot** — async daemon bot pushing market alerts, slash commands, free-form Gemini chat, voice/TTS pipeline | `TelegramBot`, `AlertState` |
| `src/engine/quantum_brain.py` | **PyTorch AI** — LSTM neural network + `BrainAgent` inference manager, 50-feature pipeline, hysteresis/cooldown logic | `BrainAgent`, `QuantumBrainNetwork`, `FeaturePipeline` |
| `src/engine/gemini_brain.py` | **Gemini 2.0 Flash LLM** — structured JSON decision engine with Pydantic schema `GeminiTradingDecision` | `GeminiBrainManager`, `GeminiTradingDecision`, `BracketRisk` |
| `src/engine/strategy.py` | **Classical Indicators** — Bollinger/RSI/MACD/ATR signal engine | `TradingStrategy` (singleton) |
| `src/engine/order_flow.py` | **CVD & Delta** — accumulates aggTrades, computes CVD, delta, buy/sell volume, spoofing detection | `OrderFlowEngine` (singleton) |
| `src/engine/binance_client.py` | **Binance API** — WebSocket streams + REST for klines, depth, trades, positions, orders | `BinanceClient` (singleton) |
| `src/engine/async_data_engine.py` | **Background Engine** — daemon thread polling REST for OI, MTF, depth, funding, HFT metrics | `AsyncDataEngine` |
| `src/engine/ai_analyst.py` | **Gemini Legacy** — simpler Gemini sentiment analysis (superseded by `gemini_brain.py`) | `AIAnalyst` |
| `src/database/supabase_manager.py` | **Local SQLite** — trades, signals, daily_stats persistence | `SupabaseManager` |
| `config/settings.py` | **Configuration** — env vars: API keys, thresholds, symbols, Telegram tokens | `Settings` singleton |

---

## 2. Module Responsibilities & Data Processing

### `quantum_brain.py` — PyTorch Neural Inference
**Input (snapshot dict):** 50 features:
- Price structure: `price`, `change_pct`, `vwap`, `atr`, `ema_20`, `ema_50`
- Technicals: `rsi`, `macd`, `bb_position`, `bb_squeeze`
- Order flow: `delta`, `delta_accel`, `cvd`, `cumulative_delta`, `imbalance`, `ba_ratio`
- Microstructure: `kaufman_eff` (Kaufman Efficiency), `tick_speed`, `spread_velocity`, `cancel_rate`, `skewness`, `pinam`
- Walls: `wall_bid_size`, `wall_ask_size`, `liq_zones`
- Categorical: `trend`, `signal_text`, `force`, multi-timeframe trends

**Output:** `brain_decision` dict:
- `direction` (ALZA/BAJA/INCIERTO), `confidence_pct`
- `prob_alza`, `prob_baja`, `prob_incierto`
- `risk_bracket` (entry, sl, tp1, tp2)
- `market_rationale` (top-5 feature z-score explanation)
- `flip_blocked` (if hysteresis prevents direction flip)

**Key logic:** Hysteresis (8% margin to flip), low-confidence cooldown (180s), exhaustion check via Kaufman Efficiency < 0.15.

### `gemini_brain.py` — Gemini 2.0 Flash LLM
**Input (compacted snapshot):** 37 metrics mapped to short keys (`p`, `d`, `cvd`, `ke`, `ts`, `wb`, `wa`, `rsi`, etc.) for token efficiency.

**Output:** `GeminiTradingDecision` (Pydantic validated):
- `decision` (ALZA/BAJA/INCIERTO), `confidence` (0–100), `exhaustion_detected`
- `score_order_flow`, `score_momentum`, `score_trend` (0–10)
- `reasoning` (max 500 chars)
- `bracket` (entry, sl, tp1, tp2)

**Key logic:** Ultra-defensive prompt: "ante la menor duda → INCIERTO con confianza < 55%". 512 max tokens, temperature 0.1.

### `telegram_bot.py` — Alert Engine & User Interface
**Alert types:**
| Alert | Trigger | Cooldown |
|-------|---------|----------|
| Flash Crash/Pump | Price drop/rise ≥ 0.35% in 60s | Rearm when < half threshold |
| Volume Spike | `volume / avg_volume ≥ 3.0x` | Rearm when < 1.5x |
| Whale Instant | `abs(delta_accel) > 100` AND `tick_speed > 25` | 12 cycles (~12s) |
| Whale Accum/Dist | `abs(delta_accel) > 100` AND `volume > 5` AND `tick_speed > 30` | 12 cycles |
| Trap | `directional_probability ≥ 60%` + trap status change | Each new trap type once |
| Signal Change | `signal_text` changes, `confidence > 55` AND `vol_ratio ≥ 1.0` | Hysteresis |
| Signal Strength | Confidence changes by ≥ 20 points | Per change |
| Trend Change | `trend` field switches value | Per change |
| Brain Alert | `brain_direction` with `confidence ≥ 60%` | Direction repetition block, 180s low-conf cooldown |
| Radar | Volume > 3.0x OR tick_speed > 30 + delta_accel > 100 OR RSI overbought/oversold breakout OR BB extreme | 10s cooldown |

**Chat commands:** `/start`, `/info`, `/signal`, `/alerts`, `/trampas`, `/bracket`, `/chart`, `/micro`, `/scalp`, `/status`, `/config`, `/ai {question}`, `/refresh`, `/ultimo`

### `dashboard_gui.py` — PyQt5 Visual Terminal
**Panels updated every 1s:**
- **Tab 1 (ORDER FLOW TERMINAL):** Galaxy chart (OpenGL), market narrative, battle bar (buy/sell/imbalance), trend signal, whale walls
- **Tab 2 (QUANTITATIVE MATRIX):** 50 grid labels (5 cols × 10 rows) covering price, volume, CVD, walls, MTF trends, RSI, MACD, AI scores; plus 5 bottom widgets (OI, liquidity, confluence, HFT risk, AI bracket)
- **Tab 3 (QUANTUM BRAIN):** Knowledge base loader, inference viewer, console log, temperature slider

---

## 3. Data Flow

```
BINANCE (WebSocket + REST)
│
├── klines (1m) ──────────────────────┐
├── aggTrades ──→ OrderFlowEngine ──→ delta, CVD, buy/sell vol
├── depth ─────→ Whale Walls (z-score) ──→ liquidity_data
└── REST polling ──→ AsyncDataEngine (daemon thread)
                        ├── MTF klines (5m, 15m, 1h, 4h)
                        ├── Open Interest deltas
                        ├── Funding Rate
                        ├── HFT metrics (tick_speed, kaufman_eff,
                        │   spread_velocity, cancel_rate, skewness, PINAM)
                        └── market_state dict (shared memory)
                                │
                                ▼
                     ┌──────────────────────┐
                     │   dashboard_gui.py   │  ← QTimer @ 1 Hz
                     │   refresh_data()     │
                     └──────────┬───────────┘
                                │
                    ┌───────────┼───────────┐
                    │           │           │
                    ▼           ▼           ▼
              ┌────────┐ ┌──────────┐ ┌──────────┐
              │ Update │ │ Brain    │ │ Telegram │
              │ Panels │ │ Workers  │ │ push     │
              │ (UI)   │ │ (QThread)│ │ (queue)  │
              └────────┘ └────┬─────┘ └────┬─────┘
                              │             │
              ┌───────────────┘             │
              ▼                              ▼
    ┌──────────────────┐          ┌──────────────────┐
    │ BrainAgent       │          │  telegram_bot.py │
    │ (PyTorch LSTM)   │          │  _process_queue  │
    │ infer_sync()     │          │  (daemon thread) │
    │ → direction      │          │                  │
    │ → confidence     │          ├── crash/pump     │
    │ → risk_bracket   │          ├── volume spike   │
    │ → rationale      │          ├── whale instant  │
    └────────┬─────────┘          ├── trap alerts    │
             │                    ├── trend change   │
             │                    ├── signal change  │
             ▼                    ├── brain alert    │
    ┌──────────────────┐          ├── radar alert    │
    │ GeminiBrainMgr   │          ├── AI trade opp   │
    │ (async HTTP)     │          └──────┬───────────┘
    │ execute_inference│                  │
    │ → GeminiDecision │                  ▼
    │ → bracket_risk   │          ┌──────────────────┐
    └────────┬─────────┘          │  Telegram API    │
             │                    │  sendMessage     │
             ▼                    │  sendPhoto       │
    ┌──────────────────┐          │  sendVoice       │
    │ UI Panel Update  │          └──────────────────┘
    │ COL 5 AI ENGINE  │
    │ + bracket widget │
    └──────────────────┘

  TELEGRAM USER ──→ slash command / chat ──→ _handle_update
                                                 │
                                           ┌─────┴──────┐
                                           │            │
                                      /command    free text
                                           │            │
                                      _cmd_*()   _chat_gemini()
                                                      │
                                                 Gemini 2.0 Flash
                                                 (system_instruction
                                                  + compact snapshot)
                                                      │
                                                 Formatted HTML post
                                                 (parsed as Telegram
                                                  premium message)
```

### Key Thread Architecture

```
┌─────────────────────────────────────────────────────┐
│  MAIN THREAD (PyQt5)                                │
│  dashboard_gui.py                                   │
│  ├── QTimer @ 1000ms → refresh_data()               │
│  │     → REST Binance: price, klines, trades, depth  │
│  │     → OrderFlowEngine: delta, CVD                │
│  │     → calculate_all_indicators()                  │
│  │     → update_panels() (50+ UI labels)             │
│  │     → telegram_bot.push_update(snapshot)          │
│  │     → BrainInferenceWorker (QThread, ~1s throttle)│
│  │     → GeminiInferenceWorker (QThread, ~3s throttle)│
│  └── BrainInferenceWorker (finished → _clear_brain_worker)│
│  └── GeminiInferenceWorker (finished → _clear_gemini_worker)│
│                                                      │
│  BACKGROUND (AsyncDataEngine daemon thread)          │
│  ├── asyncio loop polling REST endpoints              │
│  └── Writes to market_state dict (shared)             │
│                                                      │
│  BACKGROUND (TelegramBot daemon thread)               │
│  ├── asyncio event loop                              │
│  │   ├── _process_queue (snapshot consumer @ ~1Hz)   │
│  │   ├── _poll_updates (Telegram long-poll @ 25s)    │
│  │   └── _alert_loop (periodic checks @ 5s)           │
│  └── queue filled via call_soon_threadsafe from PyQt5 │
└─────────────────────────────────────────────────────┘
```

### Cooldown & Filtering Summary

| Filter | Scope | Duration |
|--------|-------|----------|
| Brain direction repetition | Same direction skip | Per snapshot |
| Brain low-confidence flip block | 50–60% conf | 180s |
| Empty bracket without trap | Skip brain alert | Per snapshot |
| Volume spike rearm | Ratio < 1.5x | Hysteresis |
| Crash/pump rearm | Drop/rise < 0.175% | Hysteresis |
| Whale cooldown | Counter | ~12s |
| Trade opportunity throttle | Timer | 30s |
| Gemini inference | Timer | 3s |
| Radar alert | Timer | 10s |
| Brain inference (PyTorch) | Timer | 1s |
| Telegram poll | HTTP long-poll | 25s |
