import re

def update_gui():
    with open('/home/RK13/RK13/BB-450/dashboard_gui.py', 'r') as f:
        content = f.read()
        
    # 1. Add MarketNarrativePanel before MainDashboard
    narrative_class = """
class MarketNarrativePanel(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: #0b0c10; border-radius: 8px; border: 1px solid #1f2833;")
        self.setMinimumWidth(320)
        self.setMaximumWidth(400)
        
        layout = QVBoxLayout()
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(15)
        
        # Header
        header = QLabel("🧠 NARRATIVA INSTITUCIONAL")
        header.setStyleSheet("color: #66fcf1; font-size: 14px; font-weight: 900; border: none; background: transparent;")
        header.setAlignment(Qt.AlignCenter)
        layout.addWidget(header)
        
        # Helper to create sections
        def create_section(title, initial_text):
            lbl_title = QLabel(title)
            lbl_title.setStyleSheet("color: #c5c6c7; font-size: 11px; font-weight: bold; border: none; background: transparent;")
            lbl_content = QLabel(initial_text)
            lbl_content.setStyleSheet("color: #ffffff; font-size: 13px; border: none; background: transparent;")
            lbl_content.setWordWrap(True)
            layout.addWidget(lbl_title)
            layout.addWidget(lbl_content)
            return lbl_content
            
        self.lbl_sonar = create_section("📡 SONAR DE BALLENAS (Market)", "Escaneando...")
        self.lbl_magnet = create_section("🧱 MAGNETISMO INSTITUCIONAL (Limit)", "Escaneando...")
        self.lbl_micro = create_section("⚖️ ESTADO MICROESTRUCTURAL", "Escaneando...")
        
        layout.addStretch()
        
        # Verdict Box
        self.lbl_decision = QLabel("🎯 DECISIÓN ALGORÍTMICA\\nANALIZANDO...")
        self.lbl_decision.setStyleSheet("color: #fff; font-size: 14px; font-weight: 900; background: #1f2833; padding: 15px; border-radius: 8px; border: 2px solid #45a29e;")
        self.lbl_decision.setWordWrap(True)
        self.lbl_decision.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.lbl_decision)
        
        self.setLayout(layout)
        
    def update_narrative(self, state, order_state):
        if not state or not order_state: return
        
        # Sonar
        buy_vol = state.get('buy_volume', 0)
        sell_vol = state.get('sell_volume', 0)
        if buy_vol > sell_vol * 2 and buy_vol > 10:
            self.lbl_sonar.setText(f"🐳 BALLENA COMPRANDO: Fuerte agresión compradora ({buy_vol:.1f} BTC recientes)")
            self.lbl_sonar.setStyleSheet("color: #00ffcc; font-size: 13px; border: none; background: transparent;")
        elif sell_vol > buy_vol * 2 and sell_vol > 10:
            self.lbl_sonar.setText(f"🐋 BALLENA VENDIENDO: Fuerte agresión vendedora ({sell_vol:.1f} BTC recientes)")
            self.lbl_sonar.setStyleSheet("color: #ff3366; font-size: 13px; border: none; background: transparent;")
        else:
            self.lbl_sonar.setText("Sin anomalías de volumen a mercado.")
            self.lbl_sonar.setStyleSheet("color: #ffffff; font-size: 13px; border: none; background: transparent;")
            
        # Magnetism
        bids = order_state.get('bids', [])
        asks = order_state.get('asks', [])
        bid_walls = [b for b in bids if float(b[1]) >= 5.0]
        ask_walls = [a for a in asks if float(a[1]) >= 5.0]
        
        wall_text = ""
        if bid_walls: wall_text += f"🧱 Soporte Institucional: {bid_walls[0][1]:.1f} BTC @ ${bid_walls[0][0]:,.0f}\\n"
        if ask_walls: wall_text += f"🧱 Resistencia Institucional: {ask_walls[0][1]:.1f} BTC @ ${ask_walls[0][0]:,.0f}"
        if not wall_text: wall_text = "Sin muros de liquidez cercanos."
        self.lbl_magnet.setText(wall_text.strip())
        
        # Microstructure
        delta = state.get('delta', 0)
        imb = state.get('liquidity_data', {}).get('imbalance', 0)
        micro_text = f"Delta: {delta:+.1f} BTC\\nImbalance OB: {imb:+.2f}"
        self.lbl_micro.setText(micro_text)
        
        # Decision
        rsi = state.get('rsi', 50)
        trend = state.get('trend', 'NEUTRAL')
        
        if trend == 'ALCISTA' and imb > 0.3 and delta > 0:
            decision = "🟢 [ OPERAR: LONG ]\\nConfirmación de absorción en soporte + Flujo comprador."
            color = "#00ffcc"
        elif trend == 'BAJISTA' and imb < -0.3 and delta < 0:
            decision = "🔴 [ OPERAR: SHORT ]\\nRechazo en muro de liquidez + Divergencia bajista."
            color = "#ff3366"
        else:
            decision = "🟡 [ DECISIÓN: NO OPERAR ]\\nZona de Choppiness / Sin ventaja estadística."
            color = "#ffcc00"
            
        self.lbl_decision.setText(decision)
        self.lbl_decision.setStyleSheet(f"color: {color}; font-size: 14px; font-weight: 900; background: #1f2833; padding: 15px; border-radius: 8px; border: 2px solid {color};")

class MainDashboard"""

    content = content.replace("class MainDashboard", narrative_class)
    
    # 2. Modify MainDashboard UI Layout
    old_layout = """        # Galaxy Order Flow Chart (Expands fully)
        self.panels['HEATMAP'] = GalaxyOrderFlowChart("GALAXY ORDER FLOW")
        tab1_layout.addWidget(self.panels['HEATMAP'], stretch=1)"""
    
    new_layout = """        # HBox for Chart and Narrative
        hbox = QHBoxLayout()
        hbox.setContentsMargins(0,0,0,0)
        hbox.setSpacing(10)
        
        # Galaxy Order Flow Chart (70%)
        self.panels['HEATMAP'] = GalaxyOrderFlowChart("GALAXY ORDER FLOW")
        hbox.addWidget(self.panels['HEATMAP'], stretch=7)
        
        # Narrative Panel (30%)
        self.panels['NARRATIVE'] = MarketNarrativePanel()
        hbox.addWidget(self.panels['NARRATIVE'], stretch=3)
        
        tab1_layout.addLayout(hbox, stretch=1)"""
        
    content = content.replace(old_layout, new_layout)
    
    # 3. Update update_panels call
    old_update = """        # ═══════════════════════════════════════════════════════════════
        # COL 1: ORDER FLOW & OI"""
        
    new_update = """        if 'NARRATIVE' in self.panels:
            self.panels['NARRATIVE'].update_narrative(self.data, self.data.get('order_book', {}))
            
        # ═══════════════════════════════════════════════════════════════
        # COL 1: ORDER FLOW & OI"""
        
    content = content.replace(old_update, new_update)

    # 4. Implement Topographic Heatmap in _render_static_layer
    old_heatmap = """        # 0. BACKGROUND VOLUME HEATMAP
        if hasattr(self, 'session_profile') and self.session_profile:
            max_vol = max(self.session_profile.values()) if self.session_profile else 1
            painter.setPen(Qt.NoPen)
            for bp, vol in self.session_profile.items():
                if not (min_p <= bp <= max_p) or vol < (max_vol * 0.05): continue
                y = py(bp); yb = py(bp - self.tick_size)
                if y > vp_max_y + 10 or y < vp_min_y - 10: continue
                ch = max(4, abs(yb - y))
                intensity = vol / max_vol
                painter.setBrush(QColor(0, 150, 255, int(intensity * 40)))
                painter.drawRect(draw_rect.left(), int(y) - int(ch/2), w, int(ch))"""

    new_heatmap = """        # 0. BACKGROUND TOPOGRAPHIC HEATMAP (Bookmap style)
        if hasattr(self, 'session_profile') and self.session_profile:
            max_vol = max(self.session_profile.values()) if self.session_profile else 1
            painter.setPen(Qt.NoPen)
            for bp, vol in self.session_profile.items():
                if not (min_p <= bp <= max_p) or vol < (max_vol * 0.05): continue
                y = py(bp); yb = py(bp - self.tick_size)
                if y > vp_max_y + 10 or y < vp_min_y - 10: continue
                ch = max(4, abs(yb - y))
                intensity = vol / max_vol
                
                # Thermal Gradient: Black -> Dark Blue -> Purple -> Red -> Yellow -> White
                if intensity < 0.2:
                    c = QColor(0, 0, int(255 * (intensity / 0.2)), 150)
                elif intensity < 0.5:
                    c = QColor(int(150 * ((intensity - 0.2)/0.3)), 0, 255, 180)
                elif intensity < 0.8:
                    c = QColor(255, 0, int(255 * (1 - (intensity - 0.5)/0.3)), 200)
                elif intensity < 0.95:
                    c = QColor(255, int(255 * ((intensity - 0.8)/0.15)), 0, 220)
                else:
                    c = QColor(255, 255, 255, 240)
                    
                painter.setBrush(c)
                painter.drawRect(draw_rect.left(), int(y) - int(ch/2), w, int(ch))"""
                
    content = content.replace(old_heatmap, new_heatmap)
    
    # 5. Remove Marching Ants Line in paintEvent
    # We will use regex to remove the marching ants block
    pattern = re.compile(r"""\s*import time\n\s*current_time = time\.time\(\)\n\s*dash_offset = int\(\(current_time \* 20\) % 20\)\n\s*pen = QPen\(QColor\(COLORS\['accent_gold'\]\), 2, Qt\.DashLine\)\n\s*pen\.setDashOffset\(dash_offset\)\n\s*painter\.setPen\(pen\)\n\s*start_idx = nc - 1\n\s*last_x = draw_rect\.left\(\) \+ \(start_idx \* cw\) \+ \(candle_zone_w / 2\)\n\s*last_y = py\(float\(self\.klines\[-1\]\[4\]\)\) if self\.klines else draw_rect\.center\(\)\.y\(\)\n\s*for pi, pc in enumerate\(self\.predicted_candles\):\n\s*idx = nc \+ pi\n\s*xc = draw_rect\.left\(\) \+ \(idx \* cw\) \+ \(candle_zone_w / 2\)\n\s*if xc > vp_max_x \+ 50 or xc < vp_min_x - 50: continue\n\s*yc = py\(pc\['c'\]\)\n\s*painter\.drawLine\(int\(last_x\), int\(last_y\), int\(xc\), int\(yc\)\)\n\s*last_x, last_y = xc, yc""")
    content = pattern.sub("", content)

    with open('/home/RK13/RK13/BB-450/dashboard_gui.py', 'w') as f:
        f.write(content)
    print("SUCCESS")

if __name__ == '__main__':
    update_gui()
