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

# --- CONFIGURATION (BYBIT V5) ---
WHALE_VOL_3M = 100000  # Balina için 3dk'da 100k USDT Hacim
WHALE_CHG_3M = 2.0  # Balina için 3dk'da %2.0 Değişim
NORMAL_LIMIT_3M = 1.1  # Normal sinyal barajı
TRI_WINDOW = 180  # 3 Dakika Analiz Penceresi

MAX_DISPLAY_ROWS = 100
SHORT_WINDOW = 60  # 1m
MID_WINDOW = 300  # 5m
LONG_WINDOW = 900  # 15m


class MarketRadar:
    def __init__(self):
        self.history = {}
        self.signals = []
        self.stats_hourly = {}
        self.stats_daily = {}
        self.lock = threading.RLock()
        self.last_heartbeat = 0
        self.total_pairs = 0
        self.last_reset_hour = datetime.now().hour
        self.last_reset_day = datetime.now().day

    def check_resets(self):
        now = datetime.now()
        if now.hour != self.last_reset_hour:
            with self.lock:
                self.stats_hourly.clear()
                self.last_reset_hour = now.hour
        if now.day != self.last_reset_day:
            with self.lock:
                self.stats_daily.clear()
                self.last_reset_day = now.day

    def process_ticker(self, data):
        """Bybit V5 Ticker Verisini İşler"""
        if "data" not in data: return
        item = data["data"]
        # Topic içinden sembolü çek (örn: tickers.BTCUSDT)
        symbol = data.get("topic", "").split(".")[-1]
        if not symbol: return

        now = time.time()
        with self.lock:
            self.check_resets()
            self.last_heartbeat = now

            price_raw = item.get('lastPrice')
            turnover_raw = item.get('turnover24h')  # Bybit'te 24s kümülatif USDT hacmi

            if price_raw is None: return

            price = float(price_raw)
            turnover = float(turnover_raw) if turnover_raw else 0

            if symbol not in self.history:
                self.history[symbol] = deque(maxlen=1200)

            self.history[symbol].append((now, price, turnover))
            self.total_pairs = len(self.history)
            self.check_logic(symbol, now)

    def check_logic(self, symbol, now):
        hist = list(self.history[symbol])
        if len(hist) < 10: return
        current = hist[-1]
        data_age = now - hist[0][0]

        # 3 Dakikalık Analiz
        past_3m = next((x for x in hist if now - x[0] <= TRI_WINDOW), hist[0])
        chg_3m = ((current[1] - past_3m[1]) / past_3m[1]) * 100
        vol_3m = current[2] - past_3m[2]  # 3 dakikadaki turnover farkı

        # Tablo Değişimleri
        past_1m = next((x for x in hist if now - x[0] <= SHORT_WINDOW), hist[0])
        past_5m = next((x for x in hist if now - x[0] <= MID_WINDOW), hist[0])

        c1 = ((current[1] - past_1m[1]) / past_1m[1]) * 100
        c5 = ((current[1] - past_5m[1]) / past_5m[1]) * 100 if data_age >= MID_WINDOW else 0.0
        c15 = ((current[1] - hist[0][1]) / hist[0][1]) * 100 if data_age >= LONG_WINDOW else 0.0

        res_type = None
        is_whale = False

        if vol_3m >= WHALE_VOL_3M and abs(chg_3m) >= WHALE_CHG_3M:
            res_type = "PUMP" if chg_3m > 0 else "DUMP"
            is_whale = True
        elif vol_3m >= 30000 and abs(chg_3m) >= NORMAL_LIMIT_3M:
            res_type = "PUMP" if chg_3m > 0 else "DUMP"
            is_whale = False

        if res_type:
            self.add_signal(symbol, current[1], c1, chg_3m, c5, c15, res_type, is_whale)

    def add_signal(self, symbol, price, c1, c3, c5, c15, s_type, is_whale):
        t_str = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            # Spam önleme (Son 10 saniye kontrolü)
            for s in self.signals[:5]:
                if s.get('Symbol') == symbol and s.get('Time', '')[:-1] == t_str[:-1] and s.get('P/D') == s_type:
                    return

            if symbol not in self.stats_hourly: self.stats_hourly[symbol] = {"PUMP": 0, "DUMP": 0}
            self.stats_hourly[symbol][s_type] += 1
            if symbol not in self.stats_daily: self.stats_daily[symbol] = {"PUMP": 0, "DUMP": 0}
            self.stats_daily[symbol][s_type] += 1

            self.signals.insert(0, {
                "Time": t_str, "Symbol": symbol,
                "Price": f"{price:.4f}" if price < 1 else f"{price:.2f}",
                "C1": c1, "C3": c3, "C5": c5, "C15": c15, "P/D": s_type,
                "SnapP": self.stats_daily[symbol]["PUMP"],
                "SnapD": self.stats_daily[symbol]["DUMP"],
                "Whale": is_whale
            })
            if len(self.signals) > MAX_DISPLAY_ROWS: self.signals.pop()


@st.cache_resource
def get_radar_instance(): return MarketRadar()


def get_all_symbols():
    """Bybit üzerindeki tüm aktif USDT paritelerini çeker"""
    try:
        resp = requests.get("https://api.bybit.com/v5/market/instruments-info?category=linear", timeout=5)
        data = resp.json()
        return [item['symbol'] for item in data['result']['list'] if item['symbol'].endswith('USDT')]
    except:
        return ["BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXUSDT"]


async def bybit_worker(radar_obj):
    uri = "wss://stream.bybit.com/v5/public/linear"
    symbols = get_all_symbols()
    # Bybit 10'arlı gruplar halinde abonelik ister
    chunks = [symbols[i:i + 10] for i in range(0, len(symbols), 10)]

    while True:
        try:
            async with websockets.connect(uri) as ws:
                for chunk in chunks:
                    sub_msg = {"op": "subscribe", "args": [f"tickers.{s}" for s in chunk]}
                    await ws.send(json.dumps(sub_msg))

                async def send_ping():
                    while True:
                        try:
                            await ws.send(json.dumps({"op": "ping"}))
                            await asyncio.sleep(20)
                        except:
                            break

                asyncio.create_task(send_ping())

                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    if "topic" in data and "tickers" in data["topic"]:
                        radar_obj.process_ticker(data)
        except:
            await asyncio.sleep(5)


# --- UI TASARIMI (BİREBİR İLK KOD) ---
st.set_page_config(layout="wide", page_title="SinyalEngineer Bybit Whale")

st.markdown("""
    <style>
    .main { background-color: #0e1117; }
    .status-live { color: #00ff88; font-weight: bold; border: 1px solid #00ff88; padding: 2px 10px; border-radius: 15px; font-size: 0.8rem; }
    .status-offline { color: #ff4b4b; font-weight: bold; border: 1px solid #ff4b4b; padding: 2px 10px; border-radius: 15px; font-size: 0.8rem; }
    .pump-label { background-color: #00ff88; color: black; padding: 2px 8px; border-radius: 4px; font-weight: bold; }
    .dump-label { background-color: #ff4b4b; color: white; padding: 2px 8px; border-radius: 4px; font-weight: bold; }
    .stat-card { background-color: #1e2127; padding: 10px; border-radius: 10px; margin-bottom: 10px; border-left: 5px solid #f1c40f; }
    table { width: 100%; border-collapse: collapse; }
    th, td { white-space: nowrap; padding: 10px 15px; text-align: left; border-bottom: 1px solid #222; }
    .sym-link { color: #f1c40f; text-decoration: none; font-weight: bold; }
    .green-arrow { color: #00ff88; font-weight: bold; }
    .red-arrow { color: #ff4b4b; font-weight: bold; }
    .whale-pump-row { background-color: rgba(0, 255, 136, 0.18) !important; border-left: 5px solid #00ff88 !important; }
    .whale-dump-row { background-color: rgba(255, 75, 75, 0.18) !important; border-left: 5px solid #ff4b4b !important; }
    div[data-testid="stTextInput"] > div { min-height: 0px; padding: 0px; }
    div[data-testid="stTextInput"] input { padding: 5px 10px; font-size: 0.85rem; }
    </style>
""", unsafe_allow_html=True)

radar = get_radar_instance()
if "thread_started" not in st.session_state:
    threading.Thread(target=lambda: asyncio.run(bybit_worker(radar)), daemon=True).start()
    st.session_state.thread_started = True

# Header
h1, h2, h3 = st.columns([2, 1, 1])
h1.title("🛡️ Bybit Whale Radar")
is_alive = (time.time() - radar.last_heartbeat) < 30 if radar.last_heartbeat > 0 else False
status_html = '<span class="status-live">● BYBIT LIVE</span>' if is_alive else '<span class="status-offline">● OFFLINE</span>'
h2.markdown(f"<div style='margin-top:10px;'>{status_html}</div>", unsafe_allow_html=True)
h2.markdown(
    f'<a href="https://x.com/SinyalEngineer" target="_blank" style="color:white; text-decoration:none;">𝕏 @SinyalEngineer</a>',
    unsafe_allow_html=True)
h3.metric("Bybit Pairs", radar.total_pairs)

st.divider()

col_side, col_main = st.columns([1, 4])
with col_main:
    header_col, search_col = st.columns([3, 1])
    header_col.subheader("📡 Bybit Live Signals (Whale 🐳 Filter)")
    search_query = search_col.text_input("Filter", placeholder="🔍 Sym...", label_visibility="collapsed",
                                         key="gs").upper()

placeholder_side = col_side.empty()
placeholder_main = col_main.empty()


def get_color(val):
    if val > 0.1: return "#00ff88"
    if val < -0.1: return "#ff4b4b"
    return "white"


while True:
    with placeholder_side.container():
        st.subheader("🔥 Top 5 Activity")
        with radar.lock:
            h_stats = dict(radar.stats_hourly)
            sorted_stats = sorted(h_stats.items(), key=lambda x: x[1]['PUMP'] + x[1]['DUMP'], reverse=True)[:5]
            for sym, counts in sorted_stats:
                tv_url = f"https://www.tradingview.com/chart/?symbol=BYBIT:{sym}.P"
                st.markdown(f'''<div class="stat-card"><a href="{tv_url}" target="_blank" class="sym-link">{sym}</a><br>
                <small><span class="green-arrow">↑ {counts["PUMP"]}</span> | <span class="red-arrow">↓ {counts["DUMP"]}</span></small></div>''',
                            unsafe_allow_html=True)

    with placeholder_main.container():
        with radar.lock:
            signals_copy = list(radar.signals)
            display_data = [s for s in signals_copy if
                            search_query in s.get('Symbol', '')] if search_query else signals_copy

            if display_data:
                # Tablo yapısı ve başlıklar
                html = "<table><tr><th>Time</th><th>Symbol (Daily ↑/↓)</th><th>Price</th><th>1m</th><th>3m(T)</th><th>5m</th><th>15m</th><th>Type</th></tr>"
                for row in display_data:
                    sym = row.get('Symbol', 'UNK')
                    p_count = row.get('SnapP', 0)
                    d_count = row.get('SnapD', 0)
                    tv_url = f"https://www.tradingview.com/chart/?symbol=BYBIT:{sym}.P"
                    c1, c3, c5, c15 = row.get('C1', 0), row.get('C3', 0), row.get('C5', 0), row.get('C15', 0)
                    p_type = row.get('P/D')

                    row_class = ""
                    whale_icon = ""
                    if row.get('Whale'):
                        whale_icon = " 🐳"
                        row_class = ' class="whale-pump-row"' if p_type == "PUMP" else ' class="whale-dump-row"'

                    # Satır İnşası
                    html += f"<tr{row_class}><td>{row.get('Time')}</td>"
                    html += f"<td><a href='{tv_url}' target='_blank' class='sym-link'>{sym}{whale_icon}</a> <small class='green-arrow'>↑{p_count}</small> <small class='red-arrow'>↓{d_count}</small></td>"
                    html += f"<td>{row.get('Price')}</td>"
                    html += f"<td style='color:{get_color(c1)};'>{c1:+.2f}%</td>"
                    html += f"<td style='color:{get_color(c3)}; font-weight:bold;'>{c3:+.2f}%</td>"
                    html += f"<td style='color:{get_color(c5)};'>{c5:+.2f}%</td>"
                    html += f"<td style='color:{get_color(c15)};'>{c15:+.2f}%</td>"
                    html += f"<td><span class='{'pump-label' if p_type == 'PUMP' else 'dump-label'}'>{p_type}</span></td></tr>"
                st.markdown(html + "</table>", unsafe_allow_html=True)
            else:
                st.info("Waiting for Bybit signals... 🐳")

    time.sleep(1)
