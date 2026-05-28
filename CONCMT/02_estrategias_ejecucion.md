# ARCHIVO 2: PROTOCOLO DE ESTRATEGIA Y REGLAS DE EJECUCIÓN ASIMÉTRICA
Este archivo define cómo debe el cerebro procesar la información de las trampas para calcular los puntos exactos de ejecución, eliminando la improvisación manual y aprovechando la marea de las ballenas.

## 1. PROTOCOLO LONG (Operar tras la validación de una BEAR_TRAP)
No se entra al mercado persiguiendo la vela de ruptura. Se ejecuta una entrada quirúrgica esperando que las ballenas dejen de presionar el precio.

### Parámetros de Configuración del Bracket de Riesgo:
- **Punto de Entrada:** Se establece una orden a mercado o límite en la zona del dPOC de la vela de la trampa, o en el retesteo lento del bloque de órdenes reclamado cuando la velocidad de ticks (`Tick Speed`) baje, lo que demuestra agotamiento vendedor.
- **Ubicación del Stop Loss (SL):** El SL se calcula de forma dinámica basándose en la volatilidad estructural. La fórmula estricta es: `SL = trap_low - (1 * ATR)`. Si el precio perfora este nivel, la manipulación falló y se invalida la estructura.
- **Objetivo de Salida Principal (TP1):** Relación de Riesgo:Beneficio asimétrica fija de 1:2. `TP1 = Entrada + (2 * distancia_SL)`. Esto asegura consistencia matemática de largo plazo.
- **Objetivos Institucionales (TP2):** Alineado de forma dinámica con el bloque límite de venta (`wall_ask_size`) más pesado registrado en el libro de órdenes del panel derecho (Narrativa Institucional). Aquí es donde las ballenas cerrarán sus posiciones.

---

## 2. PROTOCOLO SHORT (Operar tras la validación de una BULL_TRAP)
Se ejecuta la orden aprovechando la gravedad del mercado una vez que los compradores minoristas quedan sin gasolina.

### Parámetros de Configuración del Bracket de Riesgo:
- **Punto de Entrada:** En el tercio inferior de la vela de confirmación de la trampa alcista, idealmente buscando una confluencia con un desequilibrio de mercado (`Depth Imbalance` favorable a los Asks).
- **Ubicación del Stop Loss (SL):** Colocado exactamente por encima de la mecha de manipulación: `SL = trap_high + (1 * ATR)`.
- **Objetivo de Salida Principal (TP1):** Ratio estricto 1:2 respecto a la distancia al Stop Loss.
- **Objetivo de Salida Secundario (TP2):** Localizado en el bloque límite de compra (`wall_bid_size`) más robusto del Order Book o sobre la línea del dPOC del día actual.