import streamlit as st
import pandas as pd
import asyncio
import json
import websockets
import time
import threading
import requests
from datetime import datetime
from collections import deque

# --- CONFIGURATION ---
MIN_VOL_3M = 50000  # Test için hacmi biraz düşürdük (Görünürlük için)
MIN_CHG_3M = 0.5  # Test için değişimi %0.5 yaptık (Sinyal akışını görmek için)
CONFIRM_CHG_15M = 1.0
FAST_STRIKE_CHG = 1.0
TRI_WINDOW = 180
MAX_DISPLAY_ROWS = 100


class MarketRadar:
    def __init__(self):
        self.history = {}
        self.signals = []
        self.stats_hourly = {}
        self.stats_4h = {}
        self.lock = threading.RLock()
        self.last_heartbeat = 0
        self.msg_count = 0  # Veri gelip gelmediğini kontrol için
        self.total_pairs = 0
        self.last_reset_hour = datetime.now().hour
        self.last_reset_4h_block = datetime.now().hour // 4

    def get_15m_price(self, symbol):
        try:
            url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}&interval=15&limit=2"
            response = requests.get(url, timeout=3)
            if response.status_code == 200:
                return float(response.json()['result']['list'][1][1])
        except:
            pass
        return None

    def check_resets(self):
        now = datetime.now()
        if now.hour != self.last_reset_hour:
            with self.lock:
                self.stats_hourly.clear()
                self.last_reset_hour = now.hour

    def process_ticker(self, data):
        if "data" not in data: return
        self.msg_count += 1  # Sayaç artır

        item = data["data"]
        symbol = data.get("topic", "").split(".")[-1]
        if not symbol: return

        now = time.time()
        with self.lock:
            self.check_resets()
            self.last_heartbeat = now

            # Bybit V5 ticker alanları
            price_raw = item.get('lastPrice')
            turnover_raw = item.get('turnover24h')

            if price_raw is None: return

            price = float(price_raw)
            turnover = float(turnover_raw) if turnover_raw else 0

            if symbol not in self.history:
                self.history[symbol] = deque(maxlen=600)

            self.history[symbol].append((now, price, turnover))
            self.check_logic(symbol, now)

    def check_logic(self, symbol, now):
        hist = list(self.history[symbol])
        if len(hist) < 5: return

        current = hist[-1]
        past_1m = next((x for x in hist if now - x[0] <= 60), hist[0])
        past_3m = next((x for x in hist if now - x[0] <= TRI_WINDOW), hist[0])

        c1 = ((current[1] - past_1m[1]) / past_1m[1]) * 100
        c3 = ((current[1] - past_3m[1]) / past_3m[1]) * 100
        vol_3m = current[2] - past_3m[2]
        vol_1m = current[2] - past_1m[2]

        if abs(c1) >= FAST_STRIKE_CHG and vol_1m >= 30000:
            self.add_signal(symbol, current[1], c1, 0, vol_1m, "PUMP" if c1 > 0 else "DUMP", "⚡ FLASH")
            return

        if vol_3m >= MIN_VOL_3M and abs(c3) >= MIN_CHG_3M:
            price_15m_ago = self.get_15m_price(symbol)
            if price_15m_ago:
                c15 = ((current[1] - price_15m_ago) / price_15m_ago) * 100
                if (c3 > 0 and c15 > 0) or (c3 < 0 and c15 < 0):
                    self.add_signal(symbol, current[1], c3, c15, vol_3m, "PUMP" if c3 > 0 else "DUMP", "💎 CONFIRMED")

    def add_signal(self, symbol, price, chg_main, chg_ref, vol, s_type, mode):
        t_str = datetime.now().strftime("%H:%M:%S")
        sym_clean = symbol.replace("USDT", "")
        with self.lock:
            for s in self.signals[:5]:
                if s['Symbol'] == sym_clean and s['P/D'] == s_type and s['Mode'] == mode: return

            if sym_clean not in self.stats_hourly: self.stats_hourly[sym_clean] = {"PUMP": 0, "DUMP": 0}
            self.stats_hourly[sym_clean][s_type] += 1
            if sym_clean not in self.stats_4h: self.stats_4h[sym_clean] = {"PUMP": 0, "DUMP": 0}
            self.stats_4h[sym_clean][s_type] += 1

            self.signals.insert(0, {
                "Time": t_str, "Symbol": sym_clean, "FullSym": symbol,
                "Price": f"{price:.4f}" if price < 1 else f"{price:.2f}",
                "Chg": chg_main, "Ref": chg_ref, "Vol": vol, "P/D": s_type, "Mode": mode,
                "SnapP": self.stats_4h[sym_clean]["PUMP"], "SnapD": self.stats_4h[sym_clean]["DUMP"]
            })


@st.cache_resource
def get_radar(): return MarketRadar()


def fetch_bybit_symbols():
    """Bybit'ten tüm aktif USDT paritelerini çeker"""
    try:
        url = "https://api.bybit.com/v5/market/instruments-info?category=linear"
        res = requests.get(url, timeout=5).json()
        symbols = [x['symbol'] for x in res['result']['list'] if x['symbol'].endswith('USDT')]
        return symbols
    except Exception as e:
        st.error(f"Sembol listesi alınamadı: {e}")
        return ["BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXUSDT", "XRPUSDT"]


async def bybit_worker(radar_obj):
    uri = "wss://stream.bybit.com/v5/public/linear"
    all_syms = fetch_bybit_symbols()
    radar_obj.total_pairs = len(all_syms)

    # Bybit abonelik limiti: 1 mesajda max 10 tane, toplam sınırsız.
    chunks = [all_syms[i:i + 10] for i in range(0, len(all_syms), 10)]

    while True:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                for chunk in chunks:
                    sub_msg = {"op": "subscribe", "args": [f"tickers.{s}" for s in chunk]}
                    await ws.send(json.dumps(sub_msg))
                    await asyncio.sleep(0.1)  # Sunucuyu yormamak için

                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    if "topic" in data:
                        radar_obj.process_ticker(data)
        except:
            await asyncio.sleep(5)


# --- UI ---
st.set_page_config(layout="wide", page_title="Bybit Speed Radar")

radar = get_radar()
if "init" not in st.session_state:
    threading.Thread(target=lambda: asyncio.run(bybit_worker(radar)), daemon=True).start()
    st.session_state.init = True

# Tasarım
st.markdown("""
<style>
    .main { background-color: #0e1117; color: white; }
    .status-box { padding: 10px; border-radius: 10px; background: #1e2127; border: 1px solid #333; }
    .pump-label { background: #00ff88; color: black; padding: 3px 8px; border-radius: 4px; font-weight: bold; }
    .dump-label { background: #ff4b4b; color: white; padding: 3px 8px; border-radius: 4px; font-weight: bold; }
    .row-flash { background: rgba(0, 255, 136, 0.15); border-left: 5px solid #00ff88; }
    .row-dump { background: rgba(255, 75, 75, 0.15); border-left: 5px solid #ff4b4b; }
    th, td { padding: 12px; text-align: left; border-bottom: 1px solid #222; }
    .sym-link { color: #f1c40f; text-decoration: none; font-weight: bold; }
</style>
""", unsafe_allow_html=True)

# Üst Bar
c1, c2, c3, c4 = st.columns(4)
c1.metric("Tracked Pairs", radar.total_pairs)
c2.metric("Data Flow (msgs)", radar.msg_count)
is_on = (time.time() - radar.last_heartbeat) < 20
c3.markdown(f"**Status:** {'🟢 ONLINE' if is_on else '🔴 OFFLINE'}")
c4.write(f"**Last Data:** {datetime.now().strftime('%H:%M:%S')}")

st.divider()

col_side, col_main = st.columns([1, 4])

with col_side:
    st.subheader("🔥 Top Activity")
    with radar.lock:
        sorted_stats = sorted(radar.stats_hourly.items(), key=lambda x: x[1]['PUMP'] + x[1]['DUMP'], reverse=True)[:5]
        for sym, counts in sorted_stats:
            st.markdown(f"**{sym}** : green[↑{counts['PUMP']}] | red[↓{counts['DUMP']}]")

with col_main:
    search = st.text_input("Search Symbol...", "").upper()
    placeholder = st.empty()

while True:
    with placeholder.container():
        with radar.lock:
            signals = [s for s in radar.signals if search in s['Symbol']]
            if signals:
                html = "<table><tr><th>Time</th><th>Symbol</th><th>Price</th><th>Momentum</th><th>15m Ref</th><th>Status</th><th>Type</th></tr>"
                for row in signals:
                    tv_url = f"https://www.tradingview.com/chart/?symbol=BYBIT:{row['FullSym']}.P"
                    row_style = "row-flash" if row['P/D'] == "PUMP" else "row-dump"

                    html += f"<tr class='{row_style}'>"
                    html += f"<td>{row['Time']}</td>"
                    html += f"<td><a href='{tv_url}' target='_blank' class='sym-link'>{row['Symbol']}</a> <small>↑{row['SnapP']} ↓{row['SnapD']}</small></td>"
                    html += f"<td>{row['Price']}</td>"
                    html += f"<td>{row['Chg']:+.2f}%</td>"
                    html += f"<td>{row['Ref']:+.2f}%</td>"
                    html += f"<td><b>{row['Mode']}</b></td>"
                    html += f"<td><span class='{'pump-label' if row['P/D'] == 'PUMP' else 'dump-label'}'>{row['P/D']}</span></td>"
                    html += "</tr>"
                st.markdown(html + "</table>", unsafe_allow_html=True)
            else:
                st.info("Veri akışı aktif. Sinyal bekleniyor... (Piyasa sakin olabilir)")

    time.sleep(1)
