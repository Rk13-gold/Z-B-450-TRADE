# ARCHIVO 3: FILTROS DE ALTA PRECISIÓN Y GESTIÓN DE RIESGO DE ALTA FRECUENCIA (SCALPING)
Este archivo actúa como el filtro supremo del cerebro cuántico. Define las condiciones necesarias para pasar de un estado de observación pasiva a una ejecución agresiva en el mercado de futuros de Binance.

## 1. FILTROS DE CONFLUENCIA MULTITIEMPO (MTF)
- Para ejecutar una operación de Scalping en gráficos de 1 minuto con máxima probabilidad de éxito, el Cerebro debe contrastar la tendencia macro del panel `QUANTITATIVE MATRIX`.
- Si se detecta una señal de `BEAR_TRAP` en 1m, pero la columna MTF indica que la tendencia de 5m, 15m y 1h es bajista, el Cerebro debe reducir la fuerza de la inferencia mediante el factor de temperatura (`temperature`), catalogando la oportunidad como de mediano riesgo.
- La precisión perfecta ocurre con **Alineación Total**: `BEAR_TRAP` en 1m coincidiendo con tendencias alcistas en gráficos de 5m y 15m. En este escenario, la probabilidad base debe ser impulsada por encima del 85%.

## 2. GESTIÓN CRÍTICA DE RIESGO DINÁMICO (Risk Money Management)
- El tamaño de la posición (`Lot Size`) nunca es fijo. Se calcula dinámicamente en cada ciclo de 100ms.
- El script lee la variable de configuración de riesgo fijo (ej. `RISK_PER_TRADE = 10 USD` o el 1% de la cuenta según el archivo .env).
- **Fórmula de Lotaje:** `lot_size = RISK_PER_TRADE / abs(precio_entrada - stop_loss)`. 
- Si la volatilidad del mercado expande la distancia al Stop Loss de forma excesiva, el software debe contraer automáticamente el tamaño del lote. Esto garantiza que sin importar qué tan grande sea la mecha de la trampa de la ballena, tu pérdida máxima siempre estará estrictamente controlada a los dólares estipulados en tu archivo de configuración.

## 3. FILTRO DE CONGESTIÓN (CHOP ZONE DEFENSE)
- Si el indicador de eficiencia o el panel inferior reporta un estado de `CHOP ZONE - NO EDGE` o un mercado en rango hiperestrecho con velocidad de ticks (`Tick Speed`) inferior a 2.0 t/s, el cerebro debe ignorar las señales automáticas y abstenerse de enviar órdenes para proteger la cuenta de comisiones innecesarias y falsas rupturas.