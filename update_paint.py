import sys

new_code = """
    def _get_cached_text(self, text, font, color):
        key = (text, font.pointSize(), color.name())
        if key not in self.text_cache:
            fm = self.fontMetrics()
            tw = fm.horizontalAdvance(text)
            th = fm.height()
            pm = QPixmap(max(1, tw), max(1, th))
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            p.setFont(font)
            p.setPen(color)
            p.drawText(0, fm.ascent(), text)
            p.end()
            self.text_cache[key] = pm
        return self.text_cache[key]

    def _render_static_layer(self, draw_rect, cw, min_p, ps, h, fp_max, tier_medium, tier_whale, base_font_size, font, vp_w, candle_zone_w, fp_zone_w, bw):
        pm = QPixmap(self.size())
        pm.fill(Qt.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.Antialiasing)
        
        vp_min_x = draw_rect.left()
        vp_max_x = draw_rect.right()
        vp_min_y = draw_rect.top()
        vp_max_y = draw_rect.bottom()
        w = draw_rect.width()
        nc = len(self.klines)
        
        def py(p): return draw_rect.bottom() - ((p - min_p) / ps * h)

        # 0. BACKGROUND VOLUME HEATMAP
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
                painter.drawRect(draw_rect.left(), int(y) - int(ch/2), w, int(ch))

        # 1. PRICE SCALE
        scale_font = QFont(font)
        scale_font.setPointSize(7); scale_font.setBold(False); painter.setFont(scale_font)
        painter.setPen(QColor(COLORS['text_secondary']))
        for t in range(9):
            tp = min_p + ps * (t / 8)
            ty = py(tp)
            painter.drawText(self.rect().left() + 2, int(ty) + 3, f"${tp:,.0f}")
            painter.setPen(QPen(QColor("#1a1a1a"), 1))
            painter.drawLine(draw_rect.left(), int(ty), draw_rect.right() + vp_w, int(ty))
            painter.setPen(QColor(COLORS['text_secondary']))

        # 2. BOUNCE ZONES
        for zone in self.bounce_zones:
            zp = zone['price']
            if min_p <= zp <= max_p:
                zy = py(zp)
                if zy > vp_max_y + 10 or zy < vp_min_y - 10: continue
                intens = min(40, int(zone['score'] * 0.5))
                if zone['side'] == 'LONG':
                    zc = QColor(0, 245, 255, intens); lc = QColor(0, 245, 255, 120); tag = "▲"
                else:
                    zc = QColor(208, 0, 255, intens); lc = QColor(208, 0, 255, 120); tag = "▼"
                painter.setPen(Qt.NoPen); painter.setBrush(zc)
                painter.drawRect(draw_rect.left(), int(zy) - 5, w + vp_w, 10)
                painter.setPen(QPen(lc, 1, Qt.DotLine))
                painter.drawLine(draw_rect.left(), int(zy), draw_rect.right() + vp_w, int(zy))
                scale_font.setPointSize(6); scale_font.setBold(True); painter.setFont(scale_font); painter.setPen(lc)
                painter.drawText(draw_rect.left() + 2, int(zy) - 3, f"{tag}${zp:,.0f}({min(99, int(zone['score']))}%)")

        # 2.2 SESSION POC, VAH, VAL
        if hasattr(self, 'poc_price') and self.poc_price:
            if min_p <= self.poc_price <= max_p:
                poc_y = py(self.poc_price)
                if vp_min_y <= poc_y <= vp_max_y:
                    painter.setPen(QPen(QColor(0, 255, 255, 200), 2))
                    painter.drawLine(draw_rect.left(), int(poc_y), draw_rect.right() + vp_w, int(poc_y))
                    painter.setPen(QColor(0, 255, 255))
                    painter.drawText(draw_rect.right() + vp_w - 40, int(poc_y) - 2, "dPOC")
                
            if hasattr(self, 'vah') and min_p <= self.vah <= max_p:
                vah_y = py(self.vah)
                if vp_min_y <= vah_y <= vp_max_y:
                    painter.setPen(QPen(QColor(0, 255, 255, 100), 1, Qt.DotLine))
                    painter.drawLine(draw_rect.left(), int(vah_y), draw_rect.right() + vp_w, int(vah_y))
                    painter.drawText(draw_rect.right() + vp_w - 30, int(vah_y) - 2, "VAH")
            
            if hasattr(self, 'val') and min_p <= self.val <= max_p:
                val_y = py(self.val)
                if vp_min_y <= val_y <= vp_max_y:
                    painter.setPen(QPen(QColor(0, 255, 255, 100), 1, Qt.DotLine))
                    painter.drawLine(draw_rect.left(), int(val_y), draw_rect.right() + vp_w, int(val_y))
                    painter.drawText(draw_rect.right() + vp_w - 30, int(val_y) - 2, "VAL")

        # 2.3 VWAP LINE
        vwap = self.indicators.get('vwap', 0)
        if vwap and min_p <= vwap <= max_p:
            vwap_y = py(vwap)
            if vp_min_y <= vwap_y <= vp_max_y:
                painter.setPen(QPen(QColor(255, 204, 0, 200), 2, Qt.DashLine))
                painter.drawLine(draw_rect.left(), int(vwap_y), draw_rect.right() + vp_w, int(vwap_y))
                painter.setPen(QColor(255, 204, 0))
                painter.drawText(draw_rect.left() + 2, int(vwap_y) - 2, "VWAP")

        # 3. FOOTPRINT CELLS - HISTORICAL
        painter.setFont(font)
        for idx in range(nc - 1):
            xl_cell = draw_rect.left() + (idx * cw)
            if xl_cell > vp_max_x or (xl_cell + cw) < vp_min_x:
                continue
                
            if idx not in self.trade_grid: continue
            bands = self.trade_grid[idx]
            xl_fp = xl_cell + candle_zone_w
            
            candle_max_vol = 0.001
            poc_bp = None
            for bp, vols in bands.items():
                if not (min_p <= bp <= max_p): continue
                tot = vols['bid_vol'] + vols['ask_vol']
                if tot > candle_max_vol:
                    candle_max_vol = tot
                    poc_bp = bp
            
            for bp, vols in bands.items():
                if not (min_p <= bp <= max_p): continue
                y = py(bp)
                if y > vp_max_y + 10 or y < vp_min_y - 10: continue
                
                yb = py(bp - self.tick_size)
                ch = max(4, abs(yb - y))
                bv = vols['bid_vol']; av = vols['ask_vol']
                total_vol = bv + av
                delta = av - bv
                
                if total_vol < 0.01: continue
                
                center_x = xl_fp + (fp_zone_w / 2)
                bid_w = (bv / candle_max_vol) * (fp_zone_w / 2)
                ask_w = (av / candle_max_vol) * (fp_zone_w / 2)
                
                bg_alpha = 60
                if total_vol >= tier_whale: bg_alpha = 180
                elif total_vol >= tier_medium: bg_alpha = 100
                elif total_vol < VOLUME_THRESHOLD: bg_alpha = 20
                
                painter.setPen(Qt.NoPen)
                if bid_w > 0:
                    painter.setBrush(QColor(255, 50, 100, bg_alpha))
                    painter.drawRect(int(center_x - bid_w), int(y) - int(ch / 2) + 1, int(bid_w), int(ch) - 2)
                if ask_w > 0:
                    painter.setBrush(QColor(0, 245, 255, bg_alpha))
                    painter.drawRect(int(center_x), int(y) - int(ch / 2) + 1, int(ask_w), int(ch) - 2)
                
                if bp == poc_bp and total_vol > VOLUME_THRESHOLD:
                    painter.setPen(QPen(QColor(255, 200, 0, 150), 1))
                    painter.setBrush(Qt.NoBrush)
                    painter.drawRect(int(xl_fp), int(y) - int(ch / 2), int(fp_zone_w), int(ch))
                
                if total_vol >= tier_whale:
                    glow_color = QColor(0, 255, 255) if delta > 0 else QColor(255, 0, 255)
                    painter.setPen(QPen(glow_color, 1))
                    painter.setBrush(Qt.NoBrush)
                    painter.drawRect(int(xl_fp), int(y) - int(ch / 2), int(fp_zone_w), int(ch))
                
                if total_vol >= VOLUME_THRESHOLD and fp_zone_w > 15 and ch > (base_font_size - 2):
                    bt = f"{bv:.0f}" if bv >= 1 else f"{bv:.1f}"
                    at = f"{av:.0f}" if av >= 1 else f"{av:.1f}"
                    bid_color = QColor(255, 100, 150); ask_color = QColor(0, 255, 255)
                    if av > bv * 3 and av > tier_medium: ask_color = QColor(255, 255, 0)
                    if bv > av * 3 and bv > tier_medium: bid_color = QColor(255, 255, 0)
                    if total_vol >= tier_whale: bid_color = QColor(255, 255, 255); ask_color = QColor(255, 255, 255)
                    
                    pm_b = self._get_cached_text(bt, font, bid_color)
                    painter.drawPixmap(int(center_x - pm_b.width() - 3), int(y - pm_b.height()/2), pm_b)
                    pm_a = self._get_cached_text(at, font, ask_color)
                    painter.drawPixmap(int(center_x + 3), int(y - pm_a.height()/2), pm_a)

        # 4. CANDLESTICKS - HISTORICAL
        for i in range(nc - 1):
            xl_cell = draw_rect.left() + (i * cw)
            if xl_cell > vp_max_x or (xl_cell + cw) < vp_min_x:
                continue
                
            k = self.klines[i]
            o, hi, lo, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
            xc = xl_cell + (candle_zone_w / 2)
            yo = py(o); yc = py(c); yh = py(hi); yl = py(lo)
            bull = c >= o
            if bull: bc = QColor(0, 245, 255, 200); wc = QColor(0, 245, 255, 150)
            else: bc = QColor(208, 0, 255, 200); wc = QColor(208, 0, 255, 150)
            painter.setPen(QPen(wc, 1))
            painter.drawLine(int(xc), int(yh), int(xc), int(yl))
            bt = min(yo, yc); bh = max(1, abs(yo - yc))
            painter.setPen(Qt.NoPen); painter.setBrush(bc)
            painter.drawRect(int(xc - bw / 2), int(bt), int(bw), int(bh))
            
            if hasattr(self, 'candle_absorptions') and i in self.candle_absorptions:
                abs_type, abs_price = self.candle_absorptions[i]
                abs_y = py(abs_price)
                if abs_y > vp_max_y + 10 or abs_y < vp_min_y - 10: continue
                if abs_type == 'BUY_ABSORPTION': 
                    painter.setPen(Qt.NoPen); painter.setBrush(QColor(255, 0, 0, 150))
                    painter.drawEllipse(QPointF(xc, abs_y + 10), 8, 8)
                else: 
                    painter.setPen(Qt.NoPen); painter.setBrush(QColor(0, 255, 255, 150))
                    painter.drawEllipse(QPointF(xc, abs_y - 10), 8, 8)

        # 6. PER-CANDLE DELTA BARS - HISTORICAL
        dh = 15; dy = draw_rect.bottom() - dh
        for idx in range(nc - 1):
            xl_cell = draw_rect.left() + (idx * cw)
            if xl_cell > vp_max_x or (xl_cell + cw) < vp_min_x:
                continue
            if idx not in self.trade_grid: continue
            bands = self.trade_grid[idx]
            tb = sum(v['bid_vol'] for v in bands.values())
            ta = sum(v['ask_vol'] for v in bands.values())
            d = ta - tb
            xc = xl_cell + (candle_zone_w / 2)
            dbh = min(dh, max(2, abs(d) / fp_max * dh))
            painter.setPen(Qt.NoPen)
            if d > 0:
                painter.setBrush(QColor(0, 245, 255, 140))
                painter.drawRect(int(xc - bw / 2), int(dy + dh - dbh), int(bw), int(dbh))
            else:
                painter.setBrush(QColor(208, 0, 255, 140))
                painter.drawRect(int(xc - bw / 2), int(dy), int(bw), int(dbh))

        painter.end()
        return pm

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.klines or len(self.klines) < 2: return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        vp_w = 40
        draw_rect = self.rect().adjusted(50, 30, -(vp_w + 5), -10)
        vp_x = draw_rect.right() + 3
        
        vp_min_x = draw_rect.left()
        vp_max_x = draw_rect.right()
        vp_min_y = draw_rect.top()
        vp_max_y = draw_rect.bottom()
        
        w = draw_rect.width()
        h = draw_rect.height()
        nc = len(self.klines)
        total_slots = nc + self.num_predictions
        cw = w / total_slots
        candle_zone_w = min(cw * 0.3, 15)
        fp_zone_w = cw - candle_zone_w
        bw = max(2, candle_zone_w * 0.8)
        
        all_hi = [float(k[2]) for k in self.klines]
        all_lo = [float(k[3]) for k in self.klines]
        for pc in self.predicted_candles:
            all_hi.append(pc['h'])
            all_lo.append(pc['l'])
        raw_hi = max(all_hi); raw_lo = min(all_lo)
        raw_span = raw_hi - raw_lo
        pad = raw_span * 0.08
        
        center_price = (raw_hi + raw_lo) / 2 + self.y_scroll_offset
        visible_half = (raw_span + pad * 2) / (2 * self.y_scale_factor)
        min_p = center_price - visible_half
        max_p = center_price + visible_half
        ps = max_p - min_p
        if ps == 0: return
        
        def py(p): return draw_rect.bottom() - ((p - min_p) / ps * h)

        fp_max = 0.001
        for bands in self.trade_grid.values():
            for vols in bands.values():
                fp_max = max(fp_max, vols['bid_vol'], vols['ask_vol'])
                
        tier_whale = VOLUME_THRESHOLD * WHALE_MULTIPLIER
        tier_medium = VOLUME_THRESHOLD * MEDIUM_MULTIPLIER
        base_font_size = max(6, min(12, int(8 * self.y_scale_factor)))
        font = painter.font()
        font.setPointSize(base_font_size); font.setBold(True)

        current_state_hash = (self.y_scale_factor, self.y_scroll_offset, self.x_scroll_offset, 
                              draw_rect.width(), draw_rect.height(), nc, self.width(), self.height())
        if self.last_buffer_state != current_state_hash or not self.bg_buffer:
            self.bg_buffer = self._render_static_layer(draw_rect, cw, min_p, ps, h, fp_max, tier_medium, tier_whale, base_font_size, font, vp_w, candle_zone_w, fp_zone_w, bw)
            self.last_buffer_state = current_state_hash

        # Draw offscreen buffer
        painter.drawPixmap(0, 0, self.bg_buffer)

        # LIVE CANDLE RENDER (nc - 1)
        idx = nc - 1
        xl_cell = draw_rect.left() + (idx * cw)
        if not (xl_cell > vp_max_x or (xl_cell + cw) < vp_min_x):
            if idx in self.trade_grid:
                bands = self.trade_grid[idx]
                xl_fp = xl_cell + candle_zone_w
                candle_max_vol = 0.001
                poc_bp = None
                for bp, vols in bands.items():
                    if not (min_p <= bp <= max_p): continue
                    tot = vols['bid_vol'] + vols['ask_vol']
                    if tot > candle_max_vol:
                        candle_max_vol = tot
                        poc_bp = bp
                
                painter.setFont(font)
                for bp, vols in bands.items():
                    if not (min_p <= bp <= max_p): continue
                    y = py(bp)
                    if y > vp_max_y + 10 or y < vp_min_y - 10: continue
                    yb = py(bp - self.tick_size)
                    ch = max(4, abs(yb - y))
                    bv = vols['bid_vol']; av = vols['ask_vol']
                    total_vol = bv + av
                    delta = av - bv
                    if total_vol < 0.01: continue
                    
                    center_x = xl_fp + (fp_zone_w / 2)
                    bid_w = (bv / candle_max_vol) * (fp_zone_w / 2)
                    ask_w = (av / candle_max_vol) * (fp_zone_w / 2)
                    
                    bg_alpha = 60
                    if total_vol >= tier_whale: bg_alpha = 180
                    elif total_vol >= tier_medium: bg_alpha = 100
                    elif total_vol < VOLUME_THRESHOLD: bg_alpha = 20
                    
                    painter.setPen(Qt.NoPen)
                    if bid_w > 0:
                        painter.setBrush(QColor(255, 50, 100, bg_alpha))
                        painter.drawRect(int(center_x - bid_w), int(y) - int(ch / 2) + 1, int(bid_w), int(ch) - 2)
                    if ask_w > 0:
                        painter.setBrush(QColor(0, 245, 255, bg_alpha))
                        painter.drawRect(int(center_x), int(y) - int(ch / 2) + 1, int(ask_w), int(ch) - 2)
                    
                    if bp == poc_bp and total_vol > VOLUME_THRESHOLD:
                        painter.setPen(QPen(QColor(255, 200, 0, 150), 1))
                        painter.setBrush(Qt.NoBrush)
                        painter.drawRect(int(xl_fp), int(y) - int(ch / 2), int(fp_zone_w), int(ch))
                    
                    if total_vol >= tier_whale:
                        glow_color = QColor(0, 255, 255) if delta > 0 else QColor(255, 0, 255)
                        painter.setPen(QPen(glow_color, 1))
                        painter.setBrush(Qt.NoBrush)
                        painter.drawRect(int(xl_fp), int(y) - int(ch / 2), int(fp_zone_w), int(ch))
                    
                    if total_vol >= VOLUME_THRESHOLD and fp_zone_w > 15 and ch > (base_font_size - 2):
                        bt = f"{bv:.0f}" if bv >= 1 else f"{bv:.1f}"
                        at = f"{av:.0f}" if av >= 1 else f"{av:.1f}"
                        bid_color = QColor(255, 100, 150); ask_color = QColor(0, 255, 255)
                        if av > bv * 3 and av > tier_medium: ask_color = QColor(255, 255, 0)
                        if bv > av * 3 and bv > tier_medium: bid_color = QColor(255, 255, 0)
                        if total_vol >= tier_whale: bid_color = QColor(255, 255, 255); ask_color = QColor(255, 255, 255)
                        
                        pm_b = self._get_cached_text(bt, font, bid_color)
                        painter.drawPixmap(int(center_x - pm_b.width() - 3), int(y - pm_b.height()/2), pm_b)
                        pm_a = self._get_cached_text(at, font, ask_color)
                        painter.drawPixmap(int(center_x + 3), int(y - pm_a.height()/2), pm_a)

            # Live Candle
            k = self.klines[idx]
            o, hi, lo, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
            xc = xl_cell + (candle_zone_w / 2)
            yo = py(o); yc = py(c); yh = py(hi); yl = py(lo)
            bull = c >= o
            if bull: bc = QColor(0, 245, 255, 200); wc = QColor(0, 245, 255, 150)
            else: bc = QColor(208, 0, 255, 200); wc = QColor(208, 0, 255, 150)
            painter.setPen(QPen(wc, 1))
            painter.drawLine(int(xc), int(yh), int(xc), int(yl))
            bt = min(yo, yc); bh = max(1, abs(yo - yc))
            painter.setPen(Qt.NoPen); painter.setBrush(bc)
            painter.drawRect(int(xc - bw / 2), int(bt), int(bw), int(bh))

            # Live Delta Bar
            if idx in self.trade_grid:
                dh = 15; dy = draw_rect.bottom() - dh
                bands = self.trade_grid[idx]
                tb = sum(v['bid_vol'] for v in bands.values())
                ta = sum(v['ask_vol'] for v in bands.values())
                d = ta - tb
                dbh = min(dh, max(2, abs(d) / fp_max * dh))
                painter.setPen(Qt.NoPen)
                if d > 0:
                    painter.setBrush(QColor(0, 245, 255, 140))
                    painter.drawRect(int(xc - bw / 2), int(dy + dh - dbh), int(bw), int(dbh))
                else:
                    painter.setBrush(QColor(208, 0, 255, 140))
                    painter.drawRect(int(xc - bw / 2), int(dy), int(bw), int(dbh))

        # LIQUIDITY WALLS (Whales) - Animated & Collision-Free
        if self.order_state:
            import math
            import time
            wall_font = QFont(font)
            wall_font.setPointSize(6); painter.setFont(wall_font)
            current_time = time.time()
            
            def draw_animated_walls(walls, is_bid):
                used_y = []
                for price, vol in walls:
                    if min_p <= price <= max_p:
                        wy = py(price)
                        if wy > vp_max_y + 10 or wy < vp_min_y - 10: continue
                        float_offset = math.sin(current_time * 2.5 + price * 0.1) * 35
                        base_x = draw_rect.left() + (int(w * 0.25) if is_bid else int(w * 0.65))
                        text_x = base_x + float_offset
                        
                        text_y = wy
                        while any(abs(text_y - uy) < 10 for uy in used_y):
                            text_y += 10
                        used_y.append(text_y)
                        
                        color = QColor(0, 255, 0) if is_bid else QColor(255, 50, 50)
                        wall_color = QColor(0, 255, 0, 150) if is_bid else QColor(255, 50, 50, 150)
                        label = "BID" if is_bid else "ASK"
                        
                        painter.setPen(QPen(wall_color, 1, Qt.SolidLine))
                        painter.drawLine(draw_rect.left(), int(wy), draw_rect.right() + vp_w, int(wy))
                        
                        if abs(text_y - wy) > 1:
                            painter.setPen(QPen(wall_color, 1, Qt.DotLine))
                            painter.drawLine(int(text_x) - 15, int(wy), int(text_x), int(text_y))
                            
                        painter.setPen(color)
                        painter.drawText(int(text_x), int(text_y) - 2, f"{label} {vol:.1f} BTC")
            
            bid_walls = [b for b in self.order_state.get('bids', []) if b[1] > 2.0]
            draw_animated_walls(bid_walls, True)
            
            ask_walls = [a for a in self.order_state.get('asks', []) if a[1] > 2.0]
            draw_animated_walls(ask_walls, False)

        # VOLUME PROFILE SIDEBAR (Dynamic)
        if self.order_state:
            ob_max = max((q for _, q in self.order_state['bids'] + self.order_state['asks']), default=1)
            for p, q in self.order_state['bids']:
                if min_p <= p <= max_p:
                    y = py(p)
                    if y > vp_max_y + 10 or y < vp_min_y - 10: continue
                    bww = (q / ob_max) * vp_w
                    painter.setPen(Qt.NoPen); painter.setBrush(QColor(0, 245, 255, 100))
                    painter.drawRect(int(vp_x), int(y) - 1, int(bww), 3)
            for p, q in self.order_state['asks']:
                if min_p <= p <= max_p:
                    y = py(p)
                    if y > vp_max_y + 10 or y < vp_min_y - 10: continue
                    bww = (q / ob_max) * vp_w
                    painter.setPen(Qt.NoPen); painter.setBrush(QColor(208, 0, 255, 100))
                    painter.drawRect(int(vp_x), int(y) - 1, int(bww), 3)

        # PREDICTIVE GHOST CANDLES
        if self.predicted_candles:
            sep_x = draw_rect.left() + (nc * cw)
            painter.setPen(QPen(QColor(COLORS['accent_gold']), 1, Qt.DashLine))
            painter.drawLine(int(sep_x), draw_rect.top(), int(sep_x), draw_rect.bottom())
            
            p_font = QFont(font); p_font.setPointSize(6); p_font.setBold(True); painter.setFont(p_font)
            painter.setPen(QColor(COLORS['accent_gold']))
            painter.drawText(int(sep_x + 3), draw_rect.top() + 10, "PREDICTION")
            
            for pi, pc in enumerate(self.predicted_candles):
                idx = nc + pi
                xl_cell = draw_rect.left() + (idx * cw)
                if xl_cell > vp_max_x or (xl_cell + cw) < vp_min_x: continue
                xc = xl_cell + (candle_zone_w / 2)
                
                yo = py(pc['o']); yc_p = py(pc['c']); yh = py(pc['h']); yl = py(pc['l'])
                bull = pc['c'] >= pc['o']
                conf = pc['confidence']
                
                alpha_body = max(30, int(conf * 0.8))
                alpha_wick = max(20, int(conf * 0.5))
                
                if bull:
                    body_c = QColor(0, 245, 255, alpha_body)
                    wick_c = QColor(0, 245, 255, alpha_wick)
                    border_c = QColor(0, 245, 255, alpha_wick + 30)
                else:
                    body_c = QColor(208, 0, 255, alpha_body)
                    wick_c = QColor(208, 0, 255, alpha_wick)
                    border_c = QColor(208, 0, 255, alpha_wick + 30)
                
                painter.setPen(QPen(wick_c, 1, Qt.DashLine))
                painter.drawLine(int(xc), int(yh), int(xc), int(yl))
                
                bt = min(yo, yc_p); bh = max(1, abs(yo - yc_p))
                painter.setPen(QPen(border_c, 1, Qt.DashLine))
                painter.setBrush(body_c)
                painter.drawRect(int(xc - bw / 2), int(bt), int(bw), int(bh))
                
                if pi == 0 or pi == len(self.predicted_candles) - 1:
                    painter.setPen(QColor(COLORS['text_primary']))
                    label_y = int(min(yh, yl)) - 5
                    painter.drawText(int(xc - 10), label_y, f"{conf:.0f}%")
            
            import time
            current_time = time.time()
            dash_offset = int((current_time * 20) % 20)
            pen = QPen(QColor(COLORS['accent_gold']), 2, Qt.DashLine)
            pen.setDashOffset(dash_offset)
            painter.setPen(pen)
            
            start_idx = nc - 1
            last_x = draw_rect.left() + (start_idx * cw) + (candle_zone_w / 2)
            last_y = py(float(self.klines[-1][4])) if self.klines else draw_rect.center().y()
            
            for pi, pc in enumerate(self.predicted_candles):
                idx = nc + pi
                xc = draw_rect.left() + (idx * cw) + (candle_zone_w / 2)
                if xc > vp_max_x + 50 or xc < vp_min_x - 50: continue
                yc = py(pc['c'])
                painter.drawLine(int(last_x), int(last_y), int(xc), int(yc))
                last_x, last_y = xc, yc
                
        # PULSE ANIMATIONS (Radar Effect)
        import time
        current_time = time.time()
        active_pulses = []
        for p in self.visual_pulses:
            elapsed = current_time - p['start']
            if elapsed < 1.0:
                progress = elapsed / 1.0
                radius = progress * 50
                alpha = int(255 * (1.0 - progress))
                color = QColor(p['color'].red(), p['color'].green(), p['color'].blue(), alpha)
                painter.setPen(QPen(color, 2))
                painter.setBrush(QColor(color.red(), color.green(), color.blue(), int(alpha * 0.2)))
                xc = draw_rect.left() + (p['idx'] * cw) + (candle_zone_w / 2)
                yc = py(p['price'])
                painter.drawEllipse(QPointF(xc, yc), radius, radius)
                active_pulses.append(p)
        self.visual_pulses = active_pulses
        
        # ENTRY POINT INDICATOR
        if self.entry_state:
            ep = self.entry_state['price']
            side = self.entry_state['type']
            if min_p <= ep <= max_p:
                ey = py(ep)
                if vp_min_y <= ey <= vp_max_y:
                    color = QColor(0, 255, 255) if side == 'BUY' else QColor(255, 0, 255)
                    icon = "🐂" if side == 'BUY' else "🐻"
                    
                    painter.setPen(QPen(color, 2, Qt.SolidLine))
                    painter.drawLine(draw_rect.left(), int(ey), draw_rect.right() + vp_w, int(ey))
                    
                    box_w, box_h = 130, 20
                    box_x, box_y = draw_rect.left() + 5, int(ey) - box_h - 2
                    painter.setBrush(QColor(0, 0, 0, 200))
                    painter.setPen(QPen(color, 1))
                    painter.drawRoundedRect(box_x, box_y, box_w, box_h, 4, 4)
                    
                    p_font = QFont(font); p_font.setPointSize(8); p_font.setBold(True); painter.setFont(p_font)
                    painter.setPen(color)
                    painter.drawText(box_x + 5, box_y + 14, f"{icon} ENTRY: ${ep:,.1f}")
"""

with open('/home/RK13/RK13/BB-450/dashboard_gui.py', 'r') as f:
    lines = f.readlines()

start_idx = -1
end_idx = -1
for i, line in enumerate(lines):
    if "def paintEvent(self, event):" in line and start_idx == -1:
        start_idx = i
    elif start_idx != -1 and 'painter.drawText(box_x + 5, box_y + 14, f"{icon} ENTRY: ${ep:,.1f}")' in line:
        end_idx = i
        break

if start_idx != -1 and end_idx != -1:
    lines[start_idx:end_idx+1] = [new_code + "\n"]
    with open('/home/RK13/RK13/BB-450/dashboard_gui.py', 'w') as f:
        f.writelines(lines)
    print("SUCCESS")
else:
    print(f"FAILED TO FIND BOUNDS: {start_idx}, {end_idx}")
