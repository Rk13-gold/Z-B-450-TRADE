# BB-450 Professional Day Trading Plan

## Goal
Transform BB-450 into a professional day trading platform with:
- Clean professional chart for 1min timeframe scalping
- Enhanced price bar (left side) with current price highlight, more ticks, key levels
- Floating trading control panel (QDockWidget) with signal auth + manual trading

## Files to Modify

| File | Action |
|------|--------|
| `dashboard_gui.py` | Remove CAPA 4B,5,6,7 overlays; improve price bar; chart layout |
| `src/ui/trading_control.py` | CREATE new TradingControlPanel |

## Phase 1: Clean paintEvent
Remove from GalaxyOrderFlowChart.paintEvent:
- CAPA 4B (Price Bar Markers - lines 1926-2002)
- CAPA 5 (Position Markers on candles - lines 2004-2058)
- CAPA 6 (Full-width Projection Indicators - lines 2060-2140)
- CAPA 7 (CandleAnnotation diamonds - lines 2142-2178)

Keep data collection in refresh_data() (whale_walls, _price_markers) for future use.

## Phase 2: Price Bar Improvements
In `_render_static_layer` (GalaxyOrderFlowChart):
- Change draw_rect left margin: 50 → 70px for more price info space
- Increase price ticks: 9 → dynamic tick_spacing based on get_tick_info(price)
- Add subtle horizontal grid lines for each tick
- Keep price labels at `self.rect().left() + 2`

In paintEvent overlay (after static buffer):
- Draw current price horizontal line: 2px solid, white/cyan glow effect, full width
- Draw current price label on left bar: highlighted pill with bright text
- Draw VAH/VAL/POC lines: thin dashed lines at computed levels

## Phase 3: Chart Professional Layout
- max_candles: 50 → 80 (80 minutes of 1min candles for day trading)
- vp_w: 0 → 40 (enable narrow volume profile sidebar on right)
- Remove `_price_markers` data collection code from refresh_data (cleanup)

## Phase 4: Create TradingControlPanel
File: `src/ui/trading_control.py`

Class: `TradingControlPanel(QDockWidget)`
- Title: "🎮 Trading Control"
- Sections:
  1. Price header (current price large, bid/ask spread)
  2. Signal display (direction badge, confidence bar, entry/SL/TP1/TP2/RR)
  3. Signal auth button ("🔒 AUTORIZAR SEÑAL")
  4. Manual controls (LONG/SHORT buttons, capital % presets, leverage)
  5. Position status (if open: side, entry, size, PnL, close button)
  6. Order history log (last 5 actions)
- Data-driven via `update_signal(signal_data_dict)` method

Integration:
- Import in dashboard_gui.py
- Create dock widget, add to right side, F6 shortcut
- Connect to `self._executor` (OrderExecutor)
- Update every refresh_data cycle

## Phase 5: Verification
- `python3 -c "import py_compile; py_compile.compile('dashboard_gui.py', doraise=True)"`
- `python3 -c "import py_compile; py_compile.compile('src/ui/trading_control.py', doraise=True)"`
- Full import chain test
