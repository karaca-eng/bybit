import streamlit as st
import pandas as pd
import asyncio
import json
import websockets
import time
import threading
import requests
import numpy as np
from datetime import datetime
from collections import deque

# --- CONFIGURATION ---
TG_TOKEN = "8650328750:AAGQ-3NlYmpD_Gn5ONUFc59aQYv3UmS2l18"
CHID_WHALE = "-1003751484386"

MIN_VOL_3M = 40000
MIN_CHG_3M = 1.0
FAST_STRIKE_CHG = 1.2
TOLERANCE = 0.012  # %1.2 Yakınlık payı (Analiz için tolerans)
MAX_DISPLAY_ROWS = 100


class MarketRadar:
    def __init__(self):
        self.history = {}
        self.signals = []
        self.stats_4h = {}
        self.lock = threading.RLock()
        self.last_heartbeat = 0
        self.total_pairs = 0
        self.headers = {'User-Agent': 'Mozilla/5.0'}

    def send_telegram(self, message):
        try:
            url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
            payload = {"chat_id": CHID_WHALE, "text": message, "parse_mode": "HTML"}
            requests.post(url, json=payload, timeout=5)
        except:
            pass

    def get_historical_data(self, symbol, interval, limit=150):
        try:
            url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
            res = requests.get(url, headers=self.headers, timeout=3)
            if res.status_code == 200: return res.json()
        except:
            pass
        return None

    def find_pivots_pro(self, klines):
        if not klines: return []
        df = pd.DataFrame(klines,
                          columns=['time', 'open', 'high', 'low', 'close', 'vol', 'ct', 'qv', 'tr', 'b1', 'b2', 'b3'])
        df[['high', 'low', 'close']] = df[['high', 'low', 'close']].astype(float)
        pivots = []
        window = 5
        for i in range(window, len(df) - window):
            if df['high'].iloc[i] == df['high'].iloc[i - window:i + window].max():
                pivots.append(df['high'].iloc[i])
            if df['low'].iloc[i] == df['low'].iloc[i - window:i + window].min():
                pivots.append(df['low'].iloc[i])

        pivots = sorted(pivots)
        refined = []
        if pivots:
            curr = pivots[0]
            group = [curr]
            for i in range(1, len(pivots)):
                if (pivots[i] - curr) / curr < 0.012:
                    group.append(pivots[i])
                else:
                    refined.append(sum(group) / len(group))
                    group = [pivots[i]]
                curr = pivots[i]
            refined.append(sum(group) / len(group))
        return sorted(refined, reverse=True)

    def analyze_strategy(self, symbol, current_price, side):
        k1d = self.get_historical_data(symbol, "1d", 150)
        k4h = self.get_historical_data(symbol, "4h", 150)
        k15m = self.get_historical_data(symbol, "15m", 2)

        if not k1d or not k4h or not k15m: return None

        p1d = self.find_pivots_pro(k1d)
        p4h = self.find_pivots_pro(k4h)
        last_15m_close = float(k15m[-2][4])

        s1d = max([p for p in p1d if p < current_price], default=0)
        r1d = min([p for p in p1d if p > current_price], default=0)
        s4h = max([p for p in p4h if p < current_price], default=0)
        r4h = min([p for p in p4h if p > current_price], default=0)

        tags = []
        strat_type = None
        proximity_str = ""

        if side == "PUMP":
            # Destek Yakınlıkları
            prox_1d = abs(current_price - s1d) / s1d * 100 if s1d > 0 else 99
            prox_4h = abs(current_price - s4h) / s4h * 100 if s4h > 0 else 99

            if prox_1d <= TOLERANCE * 100:
                tags.append(f"🏷️ 1G DEST (%{prox_1d:.2f})")
                strat_type = "🚀 LONG (Bounce)"
            if prox_4h <= TOLERANCE * 100:
                tags.append(f"🏷️ 4S DEST (%{prox_4h:.2f})")
                strat_type = "🚀 LONG (Bounce)"

            if r4h > 0 and current_price > r4h and last_15m_close > r4h:
                strat_type = "⚡ LONG (Breakout)"
                tags.append(f"🔥 4S DİRENÇ KIRILDI")

        elif side == "DUMP":
            # Direnç Yakınlıkları
            prox_1d = abs(r1d - current_price) / r1d * 100 if r1d > 0 else 99
            prox_4h = abs(r4h - current_price) / r4h * 100 if r4h > 0 else 99

            if prox_1d <= TOLERANCE * 100:
                tags.append(f"🏷️ 1G DİR (%{prox_1d:.2f})")
                strat_type = "📉 SHORT (Rejection)"
            if prox_4h <= TOLERANCE * 100:
                tags.append(f"🏷️ 4S DİR (%{prox_4h:.2f})")
                strat_type = "📉 SHORT (Rejection)"

            if s4h > 0 and current_price < s4h and last_15m_close < s4h:
                strat_type = "💀 SHORT (Breakdown)"
                tags.append(f"🔥 4S DESTEK KIRILDI")

        if strat_type:
            return {
                "type": strat_type, "tags": " | ".join(tags),
                "s1d": s1d, "r1d": r1d, "s4h": s4h, "r4h": r4h
            }
        return None

    def process_ticker(self, data):
        now = time.time()
        with self.lock:
            self.last_heartbeat = now
            for item in data:
                symbol = item['s']
                if not symbol.endswith('USDT'): continue
                price, vol = float(item['c']), float(item['q'])
                if symbol not in self.history: self.history[symbol] = deque(maxlen=200)
                self.history[symbol].append((now, price, vol))
                self.check_logic(symbol, now)

    def check_logic(self, symbol, now):
        hist = list(self.history[symbol])
        if len(hist) < 15: return
        curr = hist[-1]
        past_1m = next((x for x in hist if now - x[0] <= 60), hist[0])
        past_3m = next((x for x in hist if now - x[0] <= 180), hist[0])
        c1, c3 = ((curr[1] - past_1m[1]) / past_1m[1]) * 100, ((curr[1] - past_3m[1]) / past_3m[1]) * 100
        v1 = curr[2] - past_1m[2]

        side = None
        if abs(c1) >= FAST_STRIKE_CHG and v1 >= 50000:
            side = "PUMP" if c1 > 0 else "DUMP"
        elif abs(c3) >= MIN_CHG_3M and (curr[2] - past_3m[2]) >= MIN_VOL_3M:
            side = "PUMP" if c3 > 0 else "DUMP"

        if side:
            sym_clean = symbol.replace("USDT", "")
            if any(s['Symbol'] == sym_clean and (now - s['ts']) < 60 for s in self.signals[:5]): return
            strat = self.analyze_strategy(symbol, curr[1], side)
            if strat: self.add_final_signal(sym_clean, curr[1], strat, side, now)

    def add_final_signal(self, sym, price, strat, s_type, ts):
        if sym not in self.stats_4h: self.stats_4h[sym] = {"PUMP": 0, "DUMP": 0}
        self.stats_4h[sym][s_type] += 1
        t_str = datetime.now().strftime("%H:%M:%S")

        signal_obj = {
            "ts": ts, "Time": t_str, "Symbol": sym, "Price": f"{price:.4f}",
            "Strat": strat['type'], "Tags": strat['tags'],
            "S1G": strat['s1d'], "R1G": strat['r1d'], "S4S": strat['s4h'], "R4S": strat['r4h'],
            "P": self.stats_4h[sym]["PUMP"], "D": self.stats_4h[sym]["DUMP"]
        }

        with self.lock:
            self.signals.insert(0, signal_obj)
            if len(self.signals) > MAX_DISPLAY_ROWS: self.signals.pop()

        msg = (f"🎯 <b>STRATEJİ ONAMI</b>\n"
               f"💰 Coin: <b>#{sym}</b> | Fiyat: <code>{price}</code>\n"
               f"📍 <b>Bölge:</b> {strat['tags']}\n"
               f"📊 İşlem: <b>{strat['type']}</b>\n\n"
               f"🛡️ 1G: {strat['s1d']:.4f} - {strat['r1d']:.4f}\n"
               f"⏱️ 4S: {strat['s4h']:.4f} - {strat['r4h']:.4f}\n\n"
               f"🔥 Aktivite: ↑ {self.stats_4h[sym]['PUMP']} | ↓ {self.stats_4h[sym]['DUMP']}\n"
               f"🔗 <a href='https://www.tradingview.com/chart/?symbol=BINANCE:{sym}USDT.P'>Grafiği Aç</a>")
        threading.Thread(target=self.send_telegram, args=(msg,)).start()


# --- STREAMLIT UI ---
st.set_page_config(layout="wide", page_title="PA Pro %")


@st.cache_resource
def get_radar(): return MarketRadar()


radar = get_radar()


async def binance_worker(radar_obj):
    uri = "wss://fstream.binance.com/ws/!miniTicker@arr"
    while True:
        try:
            async with websockets.connect(uri) as ws:
                while True: radar_obj.process_ticker(json.loads(await ws.recv()))
        except:
            await asyncio.sleep(5)


if "started" not in st.session_state:
    threading.Thread(target=lambda: asyncio.run(binance_worker(radar)), daemon=True).start()
    st.session_state.started = True

st.title("🎯 Price Action Pro Radar")
col_main, col_side = st.columns([4, 1])

while True:
    with col_main:
        with radar.lock:
            if radar.signals:
                html = "<table style='width:100%'><tr><th>Zaman</th><th>Sembol</th><th>Seviye Yakınlığı</th><th>Strateji</th><th>Fiyat</th><th>4S Akt.</th></tr>"
                for s in radar.signals[:30]:
                    color = "#00ff88" if "LONG" in s['Strat'] else "#ff4b4b"
                    html += f"<tr style='border-bottom: 1px solid #333;'>"
                    html += f"<td>{s['Time']}</td><td><b>{s['Symbol']}</b></td>"
                    html += f"<td><span style='color:#f1c40f; font-size:13px;'>{s['Tags']}</span></td>"
                    html += f"<td style='color:{color}; font-weight:bold;'>{s['Strat']}</td>"
                    html += f"<td>{s['Price']}</td><td>↑{s['P']} ↓{s['D']}</td></tr>"
                st.empty().markdown(html + "</table>", unsafe_allow_html=True)
    with col_side:
        st.subheader("🔥 Top Movers")
        with radar.lock:
            top = sorted(radar.stats_4h.items(), key=lambda x: x[1]['PUMP'] + x[1]['DUMP'], reverse=True)[:10]
            for sym, c in top: st.write(f"**{sym}**: ↑{c['PUMP']} ↓{c['DUMP']}")
    time.sleep(3)
