# ARCHIVO 1: MATRIZ DE DETECCIÓN DE TRAMPAS INSTITUCIONALES (ALTA FRECUENCIA)
Este archivo define la lógica estructural para identificar cuándo las ballenas y los algoritmos HFT barren la liquidez minorista para acumular o distribuir posiciones en gráficos de 1 minuto.

## 1. TRAMPA BAJISTA (BEAR_TRAP / LIQUIDITY HUNT DOWN)
Ocurre cuando las instituciones empujan el precio con violencia por debajo de un soporte clave, mínimos del día anterior, o el dPOC actual para activar los Stop Loss de los compradores (órdenes de venta a mercado) y saltar las órdenes pendientes Sell Stops.

### Firmas Microestructurales Obligatorias:
- **Acción del Precio:** La vela de 1 minuto perfora el soporte creando un nuevo mínimo local (`trap_low`), pero cierra en el tercio superior de su rango total, dejando una mecha larga inferior.
- **Volumen Operativo:** El volumen de la vela es significativamente alto (mínimo 1.5x por encima de la media de las últimas 20 velas), confirmando participación institucional.
- **Comportamiento del Delta y CVD:** El Delta de la vela es fuertemente negativo y la línea del `CVD` cae en picada. Esto demuestra que la masa está vendiendo presa del pánico.
- **Mecanismo de Absorción (Muros):** En el libro de órdenes, el muro de compra (`wall_bid_size`) absorbe toda la presión de venta a mercado. El precio no logra bajar más a pesar del Delta negativo debido a órdenes institucionales pasivas limitadas (Limit Bids).

### Inferencia del Cerebro:
- **Sesgo Directriz:** ALZA (LONG)
- **Confirmación Semántica:** Si se detecta divergencia en el CVD (CVD haciendo mínimos más bajos mientras el precio hace un cierre más alto), la probabilidad se incrementa en +30%. El sesgo de mercado debe pasar inmediatamente a ALZA.

---

## 2. TRAMPA ALCISTA (BULL_TRAP / STOP RUN UP)
Ocurre cuando las instituciones generan un impulso alcista artificial por encima de una resistencia relevante o el dPOC para activar las órdenes de compra de los traders de ruptura (Buy Stops) y los Stop Loss de los vendedores en corto.

### Firmas Microestructurales Obligatorias:
- **Acción del Precio:** La vela de 1 minuto supera la resistencia creando un nuevo máximo local (`trap_high`), pero cierra en el tercio inferior de su cuerpo, dejando una mecha larga superior.
- **Volumen Operativo:** Volumen climático alto (1.5x o superior), lo que indica transferencia masiva de contratos.
- **Comportamiento del Delta y CVD:** Delta altamente positivo y `CVD` con un pico agresivo al alza, indicando compras compulsivas a mercado de los minoristas atrapados.
- **Mecanismo de Absorción (Muros):** El muro de venta institucional (`wall_ask_size`) frena en seco el precio. Los algoritmos institucionales absorben toda la demanda a mercado con órdenes límite de venta (Limit Asks).

### Inferencia del Cerebro:
- **Sesgo Directriz:** BAJA (SHORT)
- **Confirmación Semántica:** Divergencia bajista en el CVD. Si la confianza es alta, rechazar cualquier señal de compra y forzar el estado general a BAJA.