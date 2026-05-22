import re

def main():
    with open('/home/RK13/RK13/BB-450/dashboard_gui.py', 'r') as f:
        content = f.read()

    # 1. New TrendSignalBar class
    trend_bar_class = """
class TrendSignalBar(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(28)
        self.setStyleSheet("border: none; background: transparent;")
        self.trend_direction = "NEUTRAL"
        self.trend_text = "WAIT ── NO CLEAR EDGE"
        self.pulse_phase = 0
        self.flash_alpha = 0
        
        self.anim_timer = QTimer(self)
        self.anim_timer.timeout.connect(self.animate_step)
        self.anim_timer.start(16)
        
    def animate_step(self):
        self.pulse_phase = (self.pulse_phase + 2) % 360
        if self.flash_alpha > 0:
            self.flash_alpha = max(0, self.flash_alpha - 25)
        self.update()
        
    def trigger_flash(self):
        self.flash_alpha = 255
        
    def update_signal(self, direction, text):
        if self.trend_direction != direction:
            self.trigger_flash()
        self.trend_direction = direction
        self.trend_text = text
        
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(10, 2, -10, -2)
        
        import math
        pulse = (math.sin(math.radians(self.pulse_phase)) + 1) / 2
        
        # Base background
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#111"))
        painter.drawRoundedRect(rect, 8, 8)
        
        # Signal Background
        a = int(120 + pulse * 60)
        if self.trend_direction == "LONG":
            color = QColor(0, 245, 255, a)
        elif self.trend_direction == "SHORT":
            color = QColor(208, 0, 255, a)
        else:
            color = QColor(30, 30, 30, a)
            
        if self.flash_alpha > 0:
            color = QColor(255, 255, 255, self.flash_alpha)
            
        painter.setBrush(color)
        painter.drawRoundedRect(rect, 8, 8)
        
        # Text
        font = painter.font(); font.setBold(True); font.setPointSize(10); painter.setFont(font)
        text_color = QColor("#ffcc00") if self.trend_direction == "NEUTRAL" else QColor("#fff")
        painter.setPen(text_color)
        painter.drawText(rect, Qt.AlignCenter, self.trend_text)
"""
    # Insert TrendSignalBar before OrderFlowBattleBar
    content = content.replace("class OrderFlowBattleBar(QFrame):", trend_bar_class + "\nclass OrderFlowBattleBar(QFrame):")
    
    # 2. Update OrderFlowBattleBar to have height 28 and same layout
    content = content.replace("self.setFixedHeight(30)", "self.setFixedHeight(28)")
    
    # 3. Replace bottom panels layout in MainDashboard
    old_layout = """        # Battle Bar
        self.battle_bar = OrderFlowBattleBar()
        tab1_layout.addWidget(self.battle_bar)
        
        # Trend Signal Label
        self.trend_signal_label = QLabel("◆ ANALYZING...")
        self.trend_signal_label.setStyleSheet(f"color: {COLORS['accent_gold']}; font-size: 14px; font-weight: bold; background: transparent;")
        self.trend_signal_label.setAlignment(Qt.AlignCenter)
        self.trend_signal_label.setFixedHeight(20)
        tab1_layout.addWidget(self.trend_signal_label)"""
        
    new_layout = """        # Symmetric Bottom Panels
        bottom_hbox = QHBoxLayout()
        bottom_hbox.setContentsMargins(0, 0, 0, 0)
        bottom_hbox.setSpacing(10)
        
        self.battle_bar = OrderFlowBattleBar()
        bottom_hbox.addWidget(self.battle_bar, stretch=1)
        
        self.trend_signal_bar = TrendSignalBar()
        bottom_hbox.addWidget(self.trend_signal_bar, stretch=1)
        
        tab1_layout.addLayout(bottom_hbox)"""
        
    content = content.replace(old_layout, new_layout)
    
    # 4. Replace MarketNarrativePanel full logic
    new_narrative = """class MarketNarrativePanel(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: #0b0c10; border-radius: 8px; border: 1px solid #1f2833;")
        self.setMinimumWidth(320)
        self.setMaximumWidth(400)
        
        layout = QVBoxLayout()
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(15)
        
        header = QLabel("🧠 NARRATIVA INSTITUCIONAL")
        header.setStyleSheet("color: #66fcf1; font-size: 14px; font-weight: 900; border: none; background: transparent;")
        header.setAlignment(Qt.AlignCenter)
        layout.addWidget(header)
        
        def create_section(title, initial_text):
            lbl_title = QLabel(title)
            lbl_title.setStyleSheet("color: #c5c6c7; font-size: 11px; font-weight: bold; border: none; background: transparent;")
            lbl_content = QLabel(initial_text)
            lbl_content.setStyleSheet("color: #ffffff; font-size: 13px; border: none; background: transparent;")
            lbl_content.setWordWrap(True)
            layout.addWidget(lbl_title)
            layout.addWidget(lbl_content)
            return lbl_content
            
        self.lbl_sonar = create_section("📡 SONAR DE BALLENAS (Market Aggression)", "Escaneando...")
        self.lbl_magnet = create_section("🧱 MAGNETISMO INSTITUCIONAL (Limit Orders)", "Escaneando...")
        
        # Custom progress bar for Depth Imbalance
        self.depth_imb_bg = QFrame()
        self.depth_imb_bg.setFixedHeight(6)
        self.depth_imb_bg.setStyleSheet("background: #222; border-radius: 3px; border: none;")
        self.depth_imb_fill = QFrame(self.depth_imb_bg)
        self.depth_imb_fill.setFixedHeight(6)
        self.depth_imb_fill.setStyleSheet("background: #00ffcc; border-radius: 3px; border: none;")
        layout.addWidget(self.depth_imb_bg)
        
        self.lbl_micro = create_section("⚖️ ESTADO MICROESTRUCTURAL (Math Log)", "Escaneando...")
        
        layout.addStretch()
        
        self.lbl_decision = QLabel("🎯 DECISIÓN ALGORÍTMICA\\nANALIZANDO...")
        self.lbl_decision.setStyleSheet("color: #fff; font-size: 14px; font-weight: 900; background: #1f2833; padding: 15px; border-radius: 8px; border: 2px solid #45a29e;")
        self.lbl_decision.setWordWrap(True)
        self.lbl_decision.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.lbl_decision)
        
        self.setLayout(layout)
        self.flash_timer = QTimer()
        self.flash_timer.setSingleShot(True)
        self.flash_timer.timeout.connect(self.reset_flash)
        self.last_state = ""
        
    def reset_flash(self):
        self.setStyleSheet("background: #0b0c10; border-radius: 8px; border: 1px solid #1f2833;")
        
    def trigger_flash(self, color):
        self.setStyleSheet(f"background: #0b0c10; border-radius: 8px; border: 2px solid {color};")
        self.flash_timer.start(100) # Soft flash duration ~3-5 frames at 60fps
        
    def update_narrative(self, state, order_state):
        if not state or not order_state: return
        
        # Sonar
        buy_vol = state.get('buy_volume', 0)
        sell_vol = state.get('sell_volume', 0)
        ts = state.get('tick_speed', 0)
        
        if buy_vol > sell_vol * 2 and buy_vol > 10:
            self.lbl_sonar.setText(f"Detalle: [COMPRA AGRESIVA]\\nVelocidad de Cintas: {ts:.1f} c/s")
            self.lbl_sonar.setStyleSheet("color: #00ffcc; font-size: 13px; border: none; background: transparent; font-family: monospace;")
            self.trigger_flash("#00ffcc")
        elif sell_vol > buy_vol * 2 and sell_vol > 10:
            self.lbl_sonar.setText(f"Detalle: [VENTA AGRESIVA]\\nVelocidad de Cintas: {ts:.1f} c/s")
            self.lbl_sonar.setStyleSheet("color: #ff3366; font-size: 13px; border: none; background: transparent; font-family: monospace;")
            self.trigger_flash("#ff3366")
        else:
            self.lbl_sonar.setText("Sin anomalías de volumen a mercado.")
            self.lbl_sonar.setStyleSheet("color: #888; font-size: 13px; border: none; background: transparent; font-family: monospace;")
            
        # Magnetism
        bids = order_state.get('bids', [])
        asks = order_state.get('asks', [])
        bid_walls = [(float(b[0]), float(b[1])) for b in bids if float(b[1]) >= 5.0]
        ask_walls = [(float(a[0]), float(a[1])) for a in asks if float(a[1]) >= 5.0]
        bid_walls.sort(key=lambda x: x[1], reverse=True)
        ask_walls.sort(key=lambda x: x[1], reverse=True)
        
        wall_text = ""
        if bid_walls:
            wall_text += f"<div style='background:#004d40; padding:2px; margin:2px;'><b style='color:#00ffcc;'>Soporte Inst 1:</b> {bid_walls[0][1]:.1f} BTC @ ${bid_walls[0][0]:,.0f}</div>"
            if len(bid_walls) > 1:
                wall_text += f"<div style='background:#00332a; padding:2px; margin:2px;'><b style='color:#00e6b8;'>Soporte Inst 2:</b> {bid_walls[1][1]:.1f} BTC @ ${bid_walls[1][0]:,.0f}</div>"
        
        if ask_walls:
            wall_text += f"<div style='background:#4d0019; padding:2px; margin:2px;'><b style='color:#ff3366;'>Resistencia Inst 1:</b> {ask_walls[0][1]:.1f} BTC @ ${ask_walls[0][0]:,.0f}</div>"
            if len(ask_walls) > 1:
                wall_text += f"<div style='background:#330011; padding:2px; margin:2px;'><b style='color:#e6004c;'>Resistencia Inst 2:</b> {ask_walls[1][1]:.1f} BTC @ ${ask_walls[1][0]:,.0f}</div>"
                
        if not wall_text: wall_text = "<div style='color:#888;'>Sin muros de liquidez cercanos.</div>"
        self.lbl_magnet.setText(wall_text.strip())
        
        imb = state.get('liquidity_data', {}).get('imbalance', 0)
        fill_pct = max(0.0, min(1.0, (imb + 1) / 2))
        w = int((self.width() - 30) * fill_pct)
        if w > 0:
            self.depth_imb_fill.setFixedWidth(w)
            self.depth_imb_fill.setStyleSheet(f"background: {'#00ffcc' if imb > 0 else '#ff3366'}; border-radius: 3px; border: none;")
        
        # Microstructure
        cvd_slope = state.get('cvd', 0)
        slope_text = "RISING" if cvd_slope > 10 else "FALLING" if cvd_slope < -10 else "FLAT"
        slope_color = "#00ffcc" if slope_text == "RISING" else "#ff3366" if slope_text == "FALLING" else "#888"
        
        ba_ratio = buy_vol / max(0.001, sell_vol)
        ba_color = "#00ffcc" if ba_ratio > 1.2 else "#ff3366" if ba_ratio < 0.8 else "#888"
        
        skew = state.get('kaufman_eff', 0.5) * 2 - 1 # Approx skew
        skew_color = "#00ffcc" if skew > 0 else "#ff3366"
        
        kurt = state.get('spread_velocity', 0) * 1.5 # Approx kurtosis
        kurt_color = "#fff" if kurt > 5 else "#888"
        
        micro = f"<table width='100%' style='font-family:monospace; font-size:11px;'>"
        micro += f"<tr><td style='color:#888;'>CVD Slope</td><td align='right' style='color:{slope_color};'>{slope_text}</td></tr>"
        micro += f"<tr><td style='color:#888;'>Bid/Ask Ratio</td><td align='right' style='color:{ba_color};'>{ba_ratio:.2f}</td></tr>"
        micro += f"<tr><td style='color:#888;'>Skewness Coef</td><td align='right' style='color:{skew_color};'>{skew:+.2f}</td></tr>"
        micro += f"<tr><td style='color:#888;'>Kurtosis</td><td align='right' style='color:{kurt_color};'>{kurt:.2f}</td></tr>"
        micro += "</table>"
        self.lbl_micro.setText(micro)
        
        # Decision
        trend = state.get('trend', 'NEUTRAL')
        if trend == 'ALCISTA' and imb > 0.3 and cvd_slope > 0:
            decision = "🟢 [ OPERAR: LONG ]\\nConfirmación de absorción en soporte + Flujo comprador."
            color = "#00ffcc"
        elif trend == 'BAJISTA' and imb < -0.3 and cvd_slope < 0:
            decision = "🔴 [ OPERAR: SHORT ]\\nRechazo en muro de liquidez + Divergencia bajista."
            color = "#ff3366"
        else:
            decision = "🟡 [ DECISIÓN: NO OPERAR ]\\nZona de Choppiness / Sin ventaja estadística."
            color = "#ffcc00"
            
        self.lbl_decision.setText(decision)
        self.lbl_decision.setStyleSheet(f"color: {color}; font-size: 14px; font-weight: 900; background: #1f2833; padding: 15px; border-radius: 8px; border: 2px solid {color};")
        
        new_state = decision.split()[2]
        if new_state != self.last_state:
            self.trigger_flash(color)
            self.last_state = new_state"""
            
    content = re.sub(r'class MarketNarrativePanel\(QFrame\):.*?(?=class MainDashboard)', new_narrative + "\n", content, flags=re.DOTALL)
    
    # 5. Fix update_panels loop for TrendSignalBar
    old_update = """        # Determine signal
        self.confidence = abs(composite - 50) * 2  # 0-100
        if composite > 62:
            self.trend_direction = "LONG"
            self.trend_label = f"▲ GO LONG — {self.confidence:.0f}% FORCE"
        elif composite < 38:
            self.trend_direction = "SHORT"
            self.trend_label = f"▼ GO SHORT — {self.confidence:.0f}% FORCE"
        else:
            self.trend_direction = "NEUTRAL"
            self.trend_label = f"◆ WAIT — NO CLEAR EDGE\"\"\"""" # this is part of battle bar
            
    # wait, the logic for updating the trend bar goes into MainDashboard.update_panels
    
    old_panel_update = """        if 'SIDEBAR' in self.panels:
            self.panels['SIDEBAR'].update_data(self.data, self.market_state)"""
            
    new_panel_update = """        if 'SIDEBAR' in self.panels:
            self.panels['SIDEBAR'].update_data(self.data, self.market_state)
            
        if hasattr(self, 'trend_signal_bar'):
            self.trend_signal_bar.update_signal(self.battle_bar.trend_direction, self.battle_bar.trend_label)
        
        # update battle bar itself
        self.battle_bar.update_battle(
            buy_volume=self.data['buy_volume'],
            sell_volume=self.data['sell_volume'],
            imbalance=self.data['liquidity_data'].get('imbalance', 0),
            trend=self.market_state.get('trend', 'NEUTRAL'),
            rsi=self.data['rsi'],
            cvd=self.data['cvd'],
            prediction_dir='PUMP' if self.data['delta'] > 0 else 'DUMP',
            prediction_conf=50
        )"""
        
    content = content.replace(old_panel_update, new_panel_update)

    with open('/home/RK13/RK13/BB-450/dashboard_gui.py', 'w') as f:
        f.write(content)
    print("SUCCESS")

if __name__ == "__main__":
    main()
