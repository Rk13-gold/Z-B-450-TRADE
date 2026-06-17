/* ═══════════════════════════════════════════════════════════════════
   BB-450 — Web Dashboard Application
   ═══════════════════════════════════════════════════════════════════ */

(function () {
    'use strict';

    // ── Config ────────────────────────────────────────────────────
    const RENDER_WS_URL = (() => {
        // 1. Forzar URL desde variable global (definida en index.html)
        if (window.BB450_WS_URL) return window.BB450_WS_URL;

        // 2. Local dev
        const host = window.location.hostname;
        if (host === 'localhost' || host === '127.0.0.1') {
            return 'ws://localhost:8000/ws';
        }

        // 3. GitHub Pages — no auto-detect, usar variable predefinida
        if (host.includes('github.io')) {
            console.warn('[BB450] GitHub Pages detectado — define BB450_WS_URL en index.html');
            return null; // will fail connection, shows disconnected
        }

        // 4. Render directo o custom domain
        const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        return `${proto}//${host}/ws`;
    })();

    const RECONNECT_DELAY = 3000;
    const MAX_CANDLES = 100;

    // ── State ─────────────────────────────────────────────────────
    let ws = null;
    let connected = false;
    let reconnectTimer = null;
    let marketState = {};
    let klines = [];
    let alerts = [];
    let position = null;

    // DOM refs (set in init)
    let els = {};

    // ── Init ──────────────────────────────────────────────────────
    function init() {
        els = {
            connectionBadge: document.getElementById('connectionBadge'),
            connectionDot: document.getElementById('connectionDot'),
            connectionText: document.getElementById('connectionText'),

            price: document.getElementById('price'),
            priceChange: document.getElementById('priceChange'),
            signalBadge: document.getElementById('signalBadge'),
            signalConf: document.getElementById('signalConf'),

            rsi: document.getElementById('rsi'),
            macd: document.getElementById('macd'),
            macdHist: document.getElementById('macdHist'),
            bbPos: document.getElementById('bbPos'),
            delta: document.getElementById('delta'),
            cvd: document.getElementById('cvd'),
            volume: document.getElementById('volume'),
            trend: document.getElementById('trend'),
            funding: document.getElementById('funding'),
            oi: document.getElementById('oi'),

            alertsFeed: document.getElementById('alertsFeed'),

            chart: document.getElementById('chart'),
            tradeBtnLong: document.getElementById('tradeLong'),
            tradeBtnShort: document.getElementById('tradeShort'),
            tradeBtnClose: document.getElementById('tradeClose'),

            modal: document.getElementById('confirmModal'),
            modalText: document.getElementById('modalText'),
            modalConfirm: document.getElementById('modalConfirm'),
            modalCancel: document.getElementById('modalCancel'),

            tradeSl: document.getElementById('tradeSl'),
            tradeTp: document.getElementById('tradeTp'),
            tradeLeverage: document.getElementById('tradeLeverage'),
            tradeCapital: document.getElementById('tradeCapital'),

            positionInfo: document.getElementById('positionInfo'),
            posDirection: document.getElementById('posDirection'),
            posEntry: document.getElementById('posEntry'),
            posPnl: document.getElementById('posPnl'),
            posLiq: document.getElementById('posLiq'),
        };

        // Set default leverage/capital
        els.tradeLeverage.value = '40';
        els.tradeCapital.value = '1.00';

        // Chart setup
        const canvas = els.chart;
        canvas.width = canvas.parentElement.clientWidth;
        canvas.height = canvas.parentElement.clientHeight;

        // Trade buttons
        els.tradeBtnLong.addEventListener('click', () => confirmTrade('LONG'));
        els.tradeBtnShort.addEventListener('click', () => confirmTrade('SHORT'));
        els.tradeBtnClose.addEventListener('click', confirmClose);
        els.modalConfirm.addEventListener('click', executeConfirmedTrade);
        els.modalCancel.addEventListener('click', () => els.modal.classList.remove('active'));

        // Close modal on overlay click
        els.modal.addEventListener('click', (e) => {
            if (e.target === els.modal) els.modal.classList.remove('active');
        });

        // Window resize
        window.addEventListener('resize', () => {
            const canvas = els.chart;
            canvas.width = canvas.parentElement.clientWidth;
            canvas.height = canvas.parentElement.clientHeight;
            drawChart();
        });

        // Keyboard shortcuts
        document.addEventListener('keydown', (e) => {
            if (e.target.tagName === 'INPUT') return;
            if (e.key === 'b' || e.key === 'B') els.tradeBtnLong.click();
            if (e.key === 's' || e.key === 'S') els.tradeBtnShort.click();
            if (e.key === 'c' || e.key === 'C') els.tradeBtnClose.click();
        });

        connect();
    }

    // ── WebSocket ─────────────────────────────────────────────────
    function connect() {
        if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

        setConnectionStatus(false);

        try {
            ws = new WebSocket(RENDER_WS_URL);
        } catch (e) {
            console.error('WS connection error:', e);
            scheduleReconnect();
            return;
        }

        ws.onopen = () => {
            console.log('[WS] Conectado a', RENDER_WS_URL);
            setConnectionStatus(true);
            if (reconnectTimer) {
                clearTimeout(reconnectTimer);
                reconnectTimer = null;
            }
        };

        ws.onclose = () => {
            console.log('[WS] Desconectado');
            setConnectionStatus(false);
            scheduleReconnect();
        };

        ws.onerror = (e) => {
            console.error('[WS] Error:', e);
        };

        ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);
                handleMessage(msg);
            } catch (e) {
                console.error('[WS] Parse error:', e);
            }
        };
    }

    function scheduleReconnect() {
        if (reconnectTimer) return;
        reconnectTimer = setTimeout(() => {
            reconnectTimer = null;
            connect();
        }, RECONNECT_DELAY);
    }

    function sendCommand(cmd) {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify(cmd));
            return true;
        }
        addAlert('⚠️ No conectado al servidor');
        return false;
    }

    function setConnectionStatus(ok) {
        connected = ok;
        const badge = els.connectionBadge;
        if (ok) {
            badge.className = 'connection-badge connected';
            els.connectionText.textContent = 'CONECTADO';
        } else {
            badge.className = 'connection-badge disconnected';
            els.connectionText.textContent = 'DESCONECTADO';
        }
    }

    // ── Message Handler ───────────────────────────────────────────
    function handleMessage(msg) {
        switch (msg.type) {
            case 'market_state':
                updateMarketState(msg.data);
                break;
            case 'command_ack':
                handleCommandAck(msg);
                break;
            case 'error':
                addAlert(`❌ ${msg.message}`);
                break;
            case 'pong':
                break;
            default:
                console.log('[WS] Unknown message type:', msg.type);
        }
    }

    function handleCommandAck(msg) {
        const status = msg.status === 'ok' ? '✅' : '❌';
        addAlert(`${status} ${msg.action}: ${msg.message}`);
        if (msg.action === 'TRADE' && msg.status === 'ok' && msg.data) {
            position = msg.data;
            updatePositionDisplay();
        }
        if (msg.action === 'CLOSE' && msg.status === 'ok') {
            position = null;
            updatePositionDisplay();
        }
    }

    // ── Market State Update ───────────────────────────────────────
    function updateMarketState(data) {
        marketState = data;

        // Price
        const price = data.price || 0;
        els.price.textContent = formatPrice(price);
        const change = data.change_pct || 0;
        els.priceChange.textContent = `${change >= 0 ? '+' : ''}${change.toFixed(2)}%`;
        els.priceChange.className = `card-sub ${change >= 0 ? 'green' : 'red'}`;

        // Signal
        const signal = (data.signal || 'NINGUNA').toUpperCase();
        const badge = els.signalBadge;
        if (signal === 'COMPRA' || signal === 'LONG') {
            badge.className = 'signal-badge long';
            badge.innerHTML = '🟢 LONG';
        } else if (signal === 'VENTA' || signal === 'SHORT') {
            badge.className = 'signal-badge short';
            badge.innerHTML = '🔴 SHORT';
        } else {
            badge.className = 'signal-badge wait';
            badge.innerHTML = '🟡 WAIT';
        }

        // Indicators
        const ind = data.indicators || {};
        const of = data.order_flow || {};

        els.rsi.textContent = (ind.rsi || 50).toFixed(1);
        els.rsi.style.color = ind.rsi > 70 ? 'var(--red)' : ind.rsi < 30 ? 'var(--green)' : 'var(--text-primary)';

        els.macd.textContent = (ind.macd || 0).toFixed(2);
        els.macdHist.textContent = (ind.macd_hist || 0).toFixed(2);
        els.macdHist.style.color = ind.macd_hist >= 0 ? 'var(--green)' : 'var(--red)';

        const bbPos = ind.bb_position || 50;
        els.bbPos.textContent = bbPos.toFixed(1) + '%';
        els.bbPos.style.color = bbPos > 70 ? 'var(--red)' : bbPos < 30 ? 'var(--green)' : 'var(--text-primary)';

        const delta = of.delta || 0;
        els.delta.textContent = (delta >= 0 ? '+' : '') + delta.toFixed(0);
        els.delta.style.color = delta >= 0 ? 'var(--green)' : 'var(--red)';

        const cvd = of.cvd || 0;
        els.cvd.textContent = (cvd >= 0 ? '+' : '') + cvd.toFixed(0);
        els.cvd.style.color = cvd >= 0 ? 'var(--green)' : 'var(--red)';

        const vol = data.volume || 0;
        const avgVol = ind.avg_volume || 1;
        const volRatio = (vol / avgVol).toFixed(1);
        els.volume.textContent = volRatio + 'x';

        const trend = ind.trend || 'NEUTRAL';
        els.trend.textContent = trend;
        els.trend.style.color = trend === 'ALCISTA' ? 'var(--green)' : trend === 'BAJISTA' ? 'var(--red)' : 'var(--text-primary)';

        const funding = of.funding_rate || 0;
        els.funding.textContent = (funding >= 0 ? '+' : '') + funding.toFixed(4) + '%';
        els.funding.style.color = funding >= 0 ? 'var(--green)' : 'var(--red)';

        const oiDelta = of.oi_delta_5m || 0;
        els.oi.textContent = (oiDelta >= 0 ? '+' : '') + oiDelta.toFixed(1) + '%';

        // Klines for chart
        const rawKlines = data.klines || [];
        if (rawKlines.length > 0) {
            klines = rawKlines.map(k => ({
                time: k[0] / 1000,
                open: parseFloat(k[1]),
                high: parseFloat(k[2]),
                low: parseFloat(k[3]),
                close: parseFloat(k[4]),
                volume: parseFloat(k[5]),
            }));
            if (klines.length > MAX_CANDLES) {
                klines = klines.slice(-MAX_CANDLES);
            }
            drawChart();
        }

        // Whale walls
        const walls = data.whale_walls || {};
        // (walls data can be displayed in a future enhancement)

        // Position
        const pos = data.position;
        if (pos) {
            position = pos;
            updatePositionDisplay();
        }
    }

    // ── Chart Drawing ─────────────────────────────────────────────
    function drawChart() {
        const canvas = els.chart;
        const ctx = canvas.getContext('2d');
        const W = canvas.width;
        const H = canvas.height;

        if (W === 0 || H === 0) return;
        if (klines.length < 2) {
            ctx.fillStyle = '#1a1a2e';
            ctx.fillRect(0, 0, W, H);
            ctx.fillStyle = '#555570';
            ctx.font = '14px monospace';
            ctx.textAlign = 'center';
            ctx.fillText('Esperando datos...', W / 2, H / 2);
            return;
        }

        const PADDING = { top: 20, bottom: 25, left: 60, right: 20 };
        const chartH = H * 0.75;
        const volH = H * 0.25;

        const cW = W - PADDING.left - PADDING.right;
        const cTop = PADDING.top;
        const cBottom = cTop + chartH;
        const vTop = cBottom + 5;
        const vBottom = vTop + volH - PADDING.bottom;

        // Price range
        let minP = Infinity, maxP = -Infinity;
        let maxV = 0;
        for (const k of klines) {
            if (k.low < minP) minP = k.low;
            if (k.high > maxP) maxP = k.high;
            if (k.volume > maxV) maxV = k.volume;
        }
        const pad = (maxP - minP) * 0.08 || minP * 0.001;
        minP -= pad;
        maxP += pad;

        const candleW = cW / klines.length;

        // Clear
        ctx.fillStyle = '#0a0a0f';
        ctx.fillRect(0, 0, W, H);

        // Grid
        ctx.strokeStyle = '#1a1a2e';
        ctx.lineWidth = 1;
        const gridLines = 5;
        for (let i = 0; i <= gridLines; i++) {
            const y = cTop + (chartH / gridLines) * i;
            ctx.beginPath();
            ctx.moveTo(PADDING.left, y);
            ctx.lineTo(W - PADDING.right, y);
            ctx.stroke();
            // Price labels
            const price = maxP - ((maxP - minP) / gridLines) * i;
            ctx.fillStyle = '#555570';
            ctx.font = '10px monospace';
            ctx.textAlign = 'right';
            ctx.fillText(formatPrice(price), PADDING.left - 5, y + 4);
        }

        // Candles
        for (let i = 0; i < klines.length; i++) {
            const k = klines[i];
            const x = PADDING.left + i * candleW + candleW * 0.1;
            const w = Math.max(1, candleW * 0.6);

            const yHigh = cTop + (1 - (k.high - minP) / (maxP - minP)) * chartH;
            const yLow = cTop + (1 - (k.low - minP) / (maxP - minP)) * chartH;
            const yOpen = cTop + (1 - (k.open - minP) / (maxP - minP)) * chartH;
            const yClose = cTop + (1 - (k.close - minP) / (maxP - minP)) * chartH;

            const isUp = k.close >= k.open;
            const color = isUp ? '#00ff88' : '#bb00ff';

            // Wick
            ctx.strokeStyle = color;
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(x + w / 2, yHigh);
            ctx.lineTo(x + w / 2, yLow);
            ctx.stroke();

            // Body
            ctx.fillStyle = color;
            const bodyTop = Math.min(yOpen, yClose);
            const bodyH = Math.max(1, Math.abs(yClose - yOpen));
            ctx.fillRect(x, bodyTop, w, bodyH);
        }

        // Volume bars
        for (let i = 0; i < klines.length; i++) {
            const k = klines[i];
            const x = PADDING.left + i * candleW + candleW * 0.15;
            const w = Math.max(1, candleW * 0.7);
            const isUp = k.close >= k.open;
            const volBarH = (k.volume / maxV) * (vBottom - vTop);
            ctx.fillStyle = isUp ? 'rgba(0,255,136,0.3)' : 'rgba(187,0,255,0.3)';
            ctx.fillRect(x, vBottom - volBarH, w, volBarH);
        }

        // Last price line
        const lastK = klines[klines.length - 1];
        const lastY = cTop + (1 - (lastK.close - minP) / (maxP - minP)) * chartH;
        ctx.strokeStyle = 'rgba(255, 204, 0, 0.5)';
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(PADDING.left, lastY);
        ctx.lineTo(W - PADDING.right, lastY);
        ctx.stroke();
        ctx.setLineDash([]);
    }

    // ── Trading ───────────────────────────────────────────────────
    let pendingTrade = null;

    function confirmTrade(direction) {
        if (!connected) {
            addAlert('⚠️ No hay conexión con el servidor');
            return;
        }

        const price = marketState.price || 0;
        const sl = parseFloat(els.tradeSl.value) || 0;
        const tp = parseFloat(els.tradeTp.value) || 0;
        const leverage = parseInt(els.tradeLeverage.value) || 40;
        const capital = parseFloat(els.tradeCapital.value) || 1.0;

        if (sl > 0 && tp > 0 && sl >= tp) {
            addAlert('⚠️ SL debe ser menor que TP');
            return;
        }

        const emoji = direction === 'LONG' ? '🟢' : '🔴';
        pendingTrade = { direction, sl, tp, leverage, capital };
        els.modalText.innerHTML = `
            <b>${emoji} ${direction}</b><br>
            Precio: <code>$${formatPrice(price)}</code><br>
            SL: <code>${sl > 0 ? '$' + formatPrice(sl) : '—'}</code><br>
            TP: <code>${tp > 0 ? '$' + formatPrice(tp) : '—'}</code><br>
            Apalancamiento: <code>${leverage}x</code><br>
            Capital: <code>$${capital.toFixed(2)}</code>
        `;
        els.modal.classList.add('active');
    }

    function confirmClose() {
        if (!connected) {
            addAlert('⚠️ No hay conexión con el servidor');
            return;
        }
        pendingTrade = { action: 'CLOSE' };
        els.modalText.innerHTML = '<b>❌ Cerrar todas las posiciones</b><br><br>¿Estás seguro?';
        els.modal.classList.add('active');
    }

    function executeConfirmedTrade() {
        els.modal.classList.remove('active');

        if (pendingTrade.action === 'CLOSE') {
            sendCommand({ action: 'CLOSE' });
            pendingTrade = null;
            return;
        }

        const t = pendingTrade;
        pendingTrade = null;

        sendCommand({
            action: 'TRADE',
            direction: t.direction,
            sl: t.sl,
            tp: t.tp,
            leverage: t.leverage,
            capital: t.capital,
        });
    }

    // ── Position Display ─────────────────────────────────────────
    function updatePositionDisplay() {
        if (position) {
            els.positionInfo.style.display = 'block';
            const dir = position.direction || '';
            const entry = position.entry_price || 0;
            const pnl = position.pnl || 0;
            const liq = position.liquidation_price || 0;

            els.posDirection.textContent = dir;
            els.posDirection.style.color = dir === 'LONG' ? 'var(--green)' : 'var(--red)';
            els.posEntry.textContent = formatPrice(entry);
            els.posPnl.textContent = (pnl >= 0 ? '+' : '') + pnl.toFixed(2);
            els.posPnl.style.color = pnl >= 0 ? 'var(--green)' : 'var(--red)';
            els.posLiq.textContent = formatPrice(liq);
        } else {
            els.positionInfo.style.display = 'none';
        }
    }

    // ── Alerts ────────────────────────────────────────────────────
    function addAlert(text) {
        const now = new Date();
        const time = now.toLocaleTimeString();
        alerts.push({ time, text });
        if (alerts.length > 50) alerts.shift();
        renderAlerts();
    }

    function renderAlerts() {
        els.alertsFeed.innerHTML = alerts.map(a =>
            `<div class="alert-item"><span class="time">${a.time}</span>${a.text}</div>`
        ).join('');
        els.alertsFeed.scrollTop = els.alertsFeed.scrollHeight;
    }

    // ── Utils ─────────────────────────────────────────────────────
    function formatPrice(p) {
        if (!p || p === 0) return '—';
        if (p >= 1000) return p.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        if (p >= 1) return p.toFixed(2);
        return p.toFixed(4);
    }

    // ── Bootstrap ────────────────────────────────────────────────
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
