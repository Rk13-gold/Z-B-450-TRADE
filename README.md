# BB-450 Bot de Scalping

## Instalación

```bash
pip install -r requirements.txt
```

## Configuración

Edita el archivo `.env` con tus credenciales:

```
BINANCE_API_KEY=tu_api_key
BINANCE_SECRET_KEY=tu_secret_key
GEMINI_API_KEY=tu_gemini_key
SUPABASE_URL=tu_url
SUPABASE_KEY=tu_key
```

## Ejecución

```bash
python src/main.py
```

## Estructura

- `config/settings.py` - Configuración centralizada
- `src/engine/binance_client.py` - WebSockets y conexión Binance
- `src/engine/order_flow.py` - Calcula Delta/CVD y detecta spoofing
- `src/engine/strategy.py` - Estrategia con BB, RSI, MACD y confirmación Delta
- `src/engine/ai_analyst.py` - Integración Gemini AI
- `src/database/supabase_manager.py` - Persistencia de trades
- `src/ui/app.py` - Interfaz TUI con Textual
- `src/main.py` - Punto de entrada# BB-450
