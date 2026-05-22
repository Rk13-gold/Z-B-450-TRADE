import os

def insert_quant_sidebar():
    with open('/home/RK13/RK13/BB-450/dashboard_gui.py', 'r') as f:
        content = f.read()

    sidebar_class = """
class QuantSidebarWidget(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(280)
        self.setStyleSheet("background: #0b0c10; border-right: 1px solid #1f2833; border-top: none; border-bottom: none; border-left: none;")
        layout = QVBoxLayout()
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)
        
        title_style = "color: #66fcf1; font-size: 11px; font-weight: 900; background: transparent; padding: 2px; border: none;"
        
        # --- BLOCK A: AI BRACKET & RISK ---
        self.box_a = QFrame()
        self.box_a.setStyleSheet("border: 2px solid #555; border-radius: 6px; background: #111;")
        la = QVBoxLayout()
        la.setContentsMargins(8, 8, 8, 8)
        lbl_a = QLabel("🤖 AI BRACKET & RISK")
        lbl_a.setStyleSheet(title_style)
        la.addWidget(lbl_a)
        
        self.lbl_status = QLabel("STATUS: WAITING")
        self.lbl_trigger = QLabel("EXEC TRIGGER: NONE")
        self.lbl_sl = QLabel("DYN STOP LOSS: 0.00")
        self.lbl_tp = QLabel("DYN TP1: 0.00")
        self.lbl_lot = QLabel("LOT SIZE ($10 Risk): 0.000")
        
        for lbl in [self.lbl_status, self.lbl_trigger, self.lbl_sl, self.lbl_tp, self.lbl_lot]:
            lbl.setStyleSheet("color: #ccc; font-size: 11px; border: none; font-family: monospace; background: transparent;")
            lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            
            # Create horizontal layout for left-right alignment
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            name_lbl = QLabel(lbl.text().split(':')[0] + ":")
            name_lbl.setStyleSheet("color: #888; font-size: 11px; border: none; background: transparent;")
            lbl.setText(lbl.text().split(':')[-1].strip())
            
            row.addWidget(name_lbl)
            row.addWidget(lbl)
            
            # Store references to update later
            setattr(self, f"val_{lbl.objectName()}", lbl)
            la.addLayout(row)
            
        self.box_a.setLayout(la)
        layout.addWidget(self.box_a)
        
        # Fix the setattr references manually
        self.val_status = la.itemAt(1).layout().itemAt(1).widget()
        self.val_trigger = la.itemAt(2).layout().itemAt(1).widget()
        self.val_sl = la.itemAt(3).layout().itemAt(1).widget()
        self.val_tp = la.itemAt(4).layout().itemAt(1).widget()
        self.val_lot = la.itemAt(5).layout().itemAt(1).widget()
        
        # --- BLOCK B: OI MOMENTUM PRO ---
        box_b = QFrame()
        box_b.setStyleSheet("background: #111; border-radius: 6px; border: 1px solid #333;")
        lb = QVBoxLayout()
        lb.setContentsMargins(8, 8, 8, 8)
        lbl_b = QLabel("⚡ OI MOMENTUM PRO")
        lbl_b.setStyleSheet(title_style)
        lb.addWidget(lbl_b)
        
        self.lbl_oi_1s = QLabel("0.0% | x1.0")
        self.lbl_oi_5s = QLabel("0.0% | x1.0")
        self.lbl_oi_1m = QLabel("0.0% | x1.0")
        
        for prefix, lbl in [("Δ 1s:", self.lbl_oi_1s), ("Δ 5s:", self.lbl_oi_5s), ("Δ 1m:", self.lbl_oi_1m)]:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            name_lbl = QLabel(prefix)
            name_lbl.setStyleSheet("color: #888; font-size: 11px; border: none; background: transparent;")
            lbl.setStyleSheet("color: #ccc; font-size: 11px; font-family: monospace; border: none; background: transparent;")
            lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            row.addWidget(name_lbl)
            row.addWidget(lbl)
            lb.addLayout(row)
            
        box_b.setLayout(lb)
        layout.addWidget(box_b)
        
        # --- BLOCK C: MTF CONFLUENCE MATRIX ---
        box_c = QFrame()
        box_c.setStyleSheet("background: #111; border-radius: 6px; border: 1px solid #333;")
        lc = QVBoxLayout()
        lc.setContentsMargins(8, 8, 8, 8)
        lbl_c = QLabel("🎯 MTF CONFLUENCE MATRIX")
        lbl_c.setStyleSheet(title_style)
        lc.addWidget(lbl_c)
        
        grid = QGridLayout()
        grid.setSpacing(4)
        headers = ["IND", "1M", "5M", "15M", "1H"]
        for col, h in enumerate(headers):
            lbl = QLabel(h)
            lbl.setStyleSheet("color: #888; font-size: 10px; font-weight: bold; border: none; background: transparent;")
            grid.addWidget(lbl, 0, col)
            
        self.mtf_labels = {}
        inds = ["EMA", "SUP", "WAV"]
        for row, ind in enumerate(inds, start=1):
            lbl_ind = QLabel(ind)
            lbl_ind.setStyleSheet("color: #aaa; font-size: 10px; border: none; background: transparent;")
            grid.addWidget(lbl_ind, row, 0)
            for col, tf in enumerate(["1M", "5M", "15M", "1H"], start=1):
                lbl_val = QLabel("-")
                lbl_val.setStyleSheet("color: #555; font-size: 10px; border: none; background: transparent;")
                grid.addWidget(lbl_val, row, col)
                self.mtf_labels[f"{ind}_{tf}"] = lbl_val
                
        lc.addLayout(grid)
        
        self.lbl_score = QLabel("SCORE: 50% NEUTRAL")
        self.lbl_score.setStyleSheet("color: #fff; background: #333; padding: 4px; border-radius: 4px; font-size: 12px; font-weight: bold; border: none;")
        self.lbl_score.setAlignment(Qt.AlignCenter)
        lc.addWidget(self.lbl_score)
        
        box_c.setLayout(lc)
        layout.addWidget(box_c)
        
        # --- BLOCK D: HFT LIQUIDITY ENGINE ---
        box_d = QFrame()
        box_d.setStyleSheet("background: #111; border-radius: 6px; border: 1px solid #333;")
        ld = QVBoxLayout()
        ld.setContentsMargins(8, 8, 8, 8)
        lbl_d = QLabel("⚙️ HFT LIQUIDITY ENGINE")
        lbl_d.setStyleSheet(title_style)
        ld.addWidget(lbl_d)
        
        self.lbl_tick = QLabel("0/s")
        self.lbl_kaufman = QLabel("0.00")
        self.lbl_spread = QLabel("0.0")
        
        for prefix, lbl in [("TICK SPEED:", self.lbl_tick), ("KAUFMAN EFF:", self.lbl_kaufman), ("SPREAD SPREAD:", self.lbl_spread)]:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            name_lbl = QLabel(prefix)
            name_lbl.setStyleSheet("color: #888; font-size: 11px; border: none; background: transparent;")
            lbl.setStyleSheet("color: #ccc; font-size: 11px; font-family: monospace; border: none; background: transparent;")
            lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            row.addWidget(name_lbl)
            row.addWidget(lbl)
            ld.addLayout(row)
            
        self.depth_bar_bg = QFrame()
        self.depth_bar_bg.setFixedHeight(10)
        self.depth_bar_bg.setStyleSheet("background: #222; border-radius: 5px; border: none;")
        
        self.depth_bar_fill = QFrame(self.depth_bar_bg)
        self.depth_bar_fill.setFixedHeight(10)
        self.depth_bar_fill.setStyleSheet("background: #00ffcc; border-radius: 5px; border: none;")
        
        ld.addWidget(self.depth_bar_bg)
        box_d.setLayout(ld)
        layout.addWidget(box_d)
        
        layout.addStretch()
        self.setLayout(layout)
        
        self.active_signal = None
        self.frozen_risk_data = {}
        
    def update_data(self, data, m_state):
        price = data.get('price', 0)
        trend = m_state.get('trend', 'NEUTRAL')
        
        if trend != 'NEUTRAL' and self.active_signal != trend:
            self.active_signal = trend
            sl_pct = 0.005
            tp_pct = 0.015
            
            if trend == 'ALCISTA':
                sl = price * (1 - sl_pct)
                tp = price * (1 + tp_pct)
            else:
                sl = price * (1 + sl_pct)
                tp = price * (1 - tp_pct)
                
            risk_usd = 10.0
            price_risk = abs(price - sl)
            lot_size = risk_usd / price_risk if price_risk > 0 else 0
            
            self.frozen_risk_data = {
                'status': 'LONG' if trend == 'ALCISTA' else 'SHORT',
                'trigger': price,
                'sl': sl,
                'tp': tp,
                'lot': lot_size
            }
        elif trend == 'NEUTRAL':
            self.active_signal = None
            self.frozen_risk_data = {}
        
        if self.frozen_risk_data:
            st = self.frozen_risk_data['status']
            c_border = "#00ffcc" if st == 'LONG' else "#ff3366"
            self.box_a.setStyleSheet(f"border: 2px solid {c_border}; border-radius: 6px; background: #111;")
            self.val_status.setText(f"{st} ACTIVE")
            self.val_status.setStyleSheet(f"color: {c_border}; font-weight: bold; font-size: 11px; background: transparent;")
            self.val_trigger.setText(f"${self.frozen_risk_data['trigger']:,.1f}")
            self.val_sl.setText(f"${self.frozen_risk_data['sl']:,.1f}")
            self.val_tp.setText(f"${self.frozen_risk_data['tp']:,.1f}")
            self.val_lot.setText(f"{self.frozen_risk_data['lot']:.3f} BTC")
        else:
            self.box_a.setStyleSheet("border: 2px solid #555; border-radius: 6px; background: #111;")
            self.val_status.setText("WAITING")
            self.val_status.setStyleSheet("color: #888; font-size: 11px; background: transparent;")
            self.val_trigger.setText("NONE")
            self.val_sl.setText("0.00")
            self.val_tp.setText("0.00")
            self.val_lot.setText("0.000")
            
        oi_1s = m_state.get('oi_delta_1s', 0)
        oi_5s = m_state.get('oi_delta_5s', 0)
        oi_1m = m_state.get('oi_delta_1m', 0)
        
        def fmt_oi(lbl, val):
            acc = 1.0 + (abs(val) * 10)
            color = "#00ffcc" if val > 0.1 else "#ff3366" if val < -0.1 else "#ccc"
            lbl.setText(f"{val:+.2f}% | x{acc:.1f}")
            lbl.setStyleSheet(f"color: {color}; font-size: 11px; font-family: monospace; background: transparent;")
            
        fmt_oi(self.lbl_oi_1s, oi_1s)
        fmt_oi(self.lbl_oi_5s, oi_5s)
        fmt_oi(self.lbl_oi_1m, oi_1m)
        
        score = 50
        for tf in ["1M", "5M", "15M", "1H"]:
            e_val = "BULL" if trend == 'ALCISTA' else "BEAR" if trend == 'BAJISTA' else "NEUT"
            s_val = e_val
            w_val = "UP" if m_state.get('delta', 0) > 0 else "DN"
            
            self.mtf_labels[f"EMA_{tf}"].setText(e_val)
            self.mtf_labels[f"SUP_{tf}"].setText(s_val)
            self.mtf_labels[f"WAV_{tf}"].setText(w_val)
            
            c_bull = "#00ffcc"
            c_bear = "#ff3366"
            c_neut = "#555"
            
            self.mtf_labels[f"EMA_{tf}"].setStyleSheet(f"color: {c_bull if e_val=='BULL' else c_bear if e_val=='BEAR' else c_neut}; font-size:10px; background: transparent;")
            self.mtf_labels[f"SUP_{tf}"].setStyleSheet(f"color: {c_bull if s_val=='BULL' else c_bear if s_val=='BEAR' else c_neut}; font-size:10px; background: transparent;")
            self.mtf_labels[f"WAV_{tf}"].setStyleSheet(f"color: {c_bull if w_val=='UP' else c_bear if w_val=='DN' else c_neut}; font-size:10px; background: transparent;")
            
            if e_val == 'BULL': score += 5
            elif e_val == 'BEAR': score -= 5
            
        score = max(0, min(100, score))
        c_score = "#00ffcc" if score > 60 else "#ff3366" if score < 40 else "#ffcc00"
        s_text = "BULLISH" if score > 60 else "BEARISH" if score < 40 else "NEUTRAL"
        self.lbl_score.setText(f"SCORE: {score}% {s_text}")
        self.lbl_score.setStyleSheet(f"color: #000; background: {c_score}; padding: 4px; border-radius: 4px; font-size: 12px; font-weight: bold; border: none;")
        
        ts = m_state.get('tick_speed', 0)
        ke = m_state.get('kaufman_eff', 0.5)
        ss = m_state.get('spread_velocity', 0)
        imb = m_state.get('depth_imbalance', 0)
        
        self.lbl_tick.setText(f"{ts:.1f}/s")
        self.lbl_kaufman.setText(f"{ke:.2f}")
        self.lbl_spread.setText(f"{ss:.1f}")
        
        fill_pct = max(0.0, min(1.0, (imb + 1) / 2))
        w = int(244 * fill_pct)
        self.depth_bar_fill.setFixedWidth(w)
        self.depth_bar_fill.setStyleSheet(f"background: {'#00ffcc' if imb > 0 else '#ff3366'}; border-radius: 5px; border: none;")

class MarketNarrativePanel"""

    content = content.replace("class MarketNarrativePanel", sidebar_class)
    
    # 2. Modify MainDashboard UI Layout
    old_layout = """        # HBox for Chart and Narrative
        hbox = QHBoxLayout()
        hbox.setContentsMargins(0,0,0,0)
        hbox.setSpacing(10)
        
        # Galaxy Order Flow Chart (70%)
        self.panels['HEATMAP'] = GalaxyOrderFlowChart("GALAXY ORDER FLOW")
        hbox.addWidget(self.panels['HEATMAP'], stretch=7)"""
        
    new_layout = """        # HBox for Chart and Narrative
        hbox = QHBoxLayout()
        hbox.setContentsMargins(0,0,0,0)
        hbox.setSpacing(10)
        
        # New Quant Sidebar (Left)
        self.panels['SIDEBAR'] = QuantSidebarWidget()
        hbox.addWidget(self.panels['SIDEBAR'], stretch=0)
        
        # Galaxy Order Flow Chart (Center)
        self.panels['HEATMAP'] = GalaxyOrderFlowChart("GALAXY ORDER FLOW")
        hbox.addWidget(self.panels['HEATMAP'], stretch=1)"""
        
    content = content.replace(old_layout, new_layout)
    
    # 3. Update update_panels call
    old_update = """        if 'NARRATIVE' in self.panels:
            self.panels['NARRATIVE'].update_narrative(self.data, self.data.get('order_book', {}))"""
            
    new_update = """        if 'NARRATIVE' in self.panels:
            self.panels['NARRATIVE'].update_narrative(self.data, self.data.get('order_book', {}))
        if 'SIDEBAR' in self.panels:
            self.panels['SIDEBAR'].update_data(self.data, self.market_state)"""
            
    content = content.replace(old_update, new_update)

    with open('/home/RK13/RK13/BB-450/dashboard_gui.py', 'w') as f:
        f.write(content)
    print("SUCCESS")

if __name__ == '__main__':
    insert_quant_sidebar()
