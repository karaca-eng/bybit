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
WHALE_VOL_3M = 80000     # Test için biraz düşürdük (80k USDT)
WHALE_CHG_3M = 1.5       # %1.5 Değişim
NORMAL_LIMIT_3M = 0.8    # %0.8 Normal sinyal
TRI_WINDOW = 180         
MAX_DISPLAY_ROWS = 100 

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
        self.connection_log = "Başlatılıyor..."

    def process_ticker(self, data):
        if "data" not in data: return
        item = data["data"]
        symbol = data.get("topic", "").split(".")[-1]
        if not symbol: return

        now = time.time()
        with self.lock:
            self.last_heartbeat = now
            price = float(item.get('lastPrice', 0))
            turnover = float(item.get('turnover24h', 0))

            if price <= 0: return

            if symbol not in self.history: 
                self.history[symbol] = deque(maxlen=600)
            
            self.history[symbol].append((now, price, turnover))
            self.total_pairs = len(self.history)
            self.check_logic(symbol, now)

    def check_logic(self, symbol, now):
        hist = list(self.history[symbol])
        if len(hist) < 5: return
        current = hist[-1]
        past_3m = next((x for x in hist if now - x[0] <= TRI_WINDOW), hist[0])
        
        chg_3m = ((current[1] - past_3m[1]) / past_3m[1]) * 100
        vol_3m = current[2] - past_3m[2]

        res_type = None
        is_whale = False

        if vol_3m >= WHALE_VOL_3M and abs(chg_3m) >= WHALE_CHG_3M:
            res_type = "PUMP" if chg_3m > 0 else "DUMP"
            is_whale = True
        elif vol_3m >= 20000 and abs(chg_3m) >= NORMAL_LIMIT_3M:
            res_type = "PUMP" if chg_3m > 0 else "DUMP"
            is_whale = False

        if res_type:
            past_1m = next((x for x in hist if now - x[0] <= 60), hist[0])
            past_5m = next((x for x in hist if now - x[0] <= 300), hist[0])
            c1 = ((current[1] - past_1m[1]) / past_1m[1]) * 100
            c5 = ((current[1] - past_5m[1]) / past_5m[1]) * 100
            self.add_signal(symbol, current[1], c1, chg_3m, c5, 0, res_type, is_whale)

    def add_signal(self, symbol, price, c1, c3, c5, c15, s_type, is_whale):
        t_str = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            for s in self.signals[:3]:
                if s['Symbol'] == symbol and s['P/D'] == s_type: return
            
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
def get_radar(): return MarketRadar()

def fetch_symbols():
    """Bybit API'sinden tüm pariteleri güvenli şekilde çeker"""
    url = "https://api.bybit.com/v5/market/instruments-info?category=linear"
    try:
        response = requests.get(url, timeout=10)
        data = response.json()
        if data.get("retCode") == 0:
            syms = [item['symbol'] for item in data['result']['list'] if item['symbol'].endswith('USDT')]
            return syms
    except Exception as e:
        print(f"Sembol çekme hatası: {e}")
    return []

async def bybit_worker(radar_obj):
    uri = "wss://stream.bybit.com/v5/public/linear"
    
    while True:
        symbols = fetch_symbols()
        if not symbols:
            radar_obj.connection_log = "⚠️ Sembol listesi alınamadı, 10sn sonra tekrar denenecek..."
            await asyncio.sleep(10)
            continue
        
        radar_obj.connection_log = f"✅ {len(symbols)} parite bulundu. Bağlanılıyor..."
        
        try:
            async with websockets.connect(uri, ping_interval=20) as ws:
                # Bybit abonelik limiti: Bir seferde 10 argüman
                for i in range(0, len(symbols), 10):
                    chunk = symbols[i:i+10]
                    sub_msg = {"op": "subscribe", "args": [f"tickers.{s}" for s in chunk]}
                    await ws.send(json.dumps(sub_msg))
                    await asyncio.sleep(0.1) # Rate limit koruması

                radar_obj.connection_log = f"🚀 {len(symbols)} parite izleniyor (Aktif)"
                
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    if "topic" in data:
                        radar_obj.process_ticker(data)
        except Exception as e:
            radar_obj.connection_log = f"❌ Bağlantı koptu: {e}. Yeniden deneniyor..."
            await asyncio.sleep(5)

# --- UI ---
st.set_page_config(layout="wide", page_title="SinyalEngineer Bybit Whale")

radar = get_radar()
if "started" not in st.session_state:
    threading.Thread(target=lambda: asyncio.run(bybit_worker(radar)), daemon=True).start()
    st.session_state.started = True

# CSS (İlk kodun CSS'i)
st.markdown("""
    <style>
    .main { background-color: #0e1117; }
    .status-live { color: #00ff88; font-weight: bold; border: 1px solid #00ff88; padding: 2px 10px; border-radius: 15px; font-size: 0.8rem; }
    .pump-label { background-color: #00ff88; color: black; padding: 2px 8px; border-radius: 4px; font-weight: bold; }
    .dump-label { background-color: #ff4b4b; color: white; padding: 2px 8px; border-radius: 4px; font-weight: bold; }
    .stat-card { background-color: #1e2127; padding: 10px; border-radius: 10px; margin-bottom: 10px; border-left: 5px solid #f1c40f; }
    table { width: 100%; border-collapse: collapse; }
    th, td { white-space: nowrap; padding: 10px 15px; text-align: left; border-bottom: 1px solid #222; }
    .sym-link { color: #f1c40f; text-decoration: none; font-weight: bold; }
    .green-arrow { color: #00ff88; font-weight: bold; }
    .red-arrow { color: #ff4b4b; font-weight: bold; }
    .whale-pump-row { background-color: rgba(0, 255, 136, 0.15) !important; border-left: 5px solid #00ff88 !important; }
    .whale-dump-row { background-color: rgba(255, 75, 75, 0.15) !important; border-left: 5px solid #ff4b4b !important; }
    </style>
""", unsafe_allow_html=True)

# Üst Panel
h1, h2, h3 = st.columns([2, 1, 1])
h1.title("🛡️ Bybit Whale Radar")
is_alive = (time.time() - radar.last_heartbeat) < 30
status_html = '<span class="status-live">● BYBIT LIVE</span>' if is_alive else '<span style="color:red">● OFFLINE</span>'
h2.markdown(f"<div style='margin-top:10px;'>{status_html}</div>", unsafe_allow_html=True)
h3.metric("Bybit Pairs", radar.total_pairs)

st.caption(f"Sistem Durumu: {radar.connection_log}")

st.divider()

col_side, col_main = st.columns([1, 4])

with col_main:
    header_col, search_col = st.columns([3, 1])
    header_col.subheader("📡 Canlı Balina Akışı")
    search_query = search_col.text_input("Filtrele", placeholder="Sembol...", label_visibility="collapsed").upper()

placeholder_side = col_side.empty()
placeholder_main = col_main.empty()

def get_color(val):
    if val > 0.1: return "#00ff88"
    if val < -0.1: return "#ff4b4b"
    return "white"

# Güncelleme Döngüsü
while True:
    with placeholder_side.container():
        st.subheader("🔥 Top 5 (1s)")
        with radar.lock:
            h_stats = dict(radar.stats_hourly)
            sorted_stats = sorted(h_stats.items(), key=lambda x: x[1]['PUMP'] + x[1]['DUMP'], reverse=True)[:5]
            for sym, counts in sorted_stats:
                tv_url = f"https://www.tradingview.com/chart/?symbol=BYBIT:{sym}.P"
                st.markdown(f'''<div class="stat-card"><a href="{tv_url}" target="_blank" class="sym-link">{sym}</a><br>
                <small><span class="green-arrow">↑ {counts["PUMP"]}</span> | <span class="red-arrow">↓ {counts["DUMP"]}</span></small></div>''', unsafe_allow_html=True)

    with placeholder_main.container():
        with radar.lock:
            display_data = [s for s in radar.signals if search_query in s['Symbol']] if search_query else radar.signals
            
            if display_data:
                html = "<table><tr><th>Time</th><th>Symbol (Daily ↑/↓)</th><th>Price</th><th>1m</th><th>3m(T)</th><th>5m</th><th>Type</th></tr>"
                for row in display_data:
                    sym = row['Symbol']; p_count = row['SnapP']; d_count = row['SnapD']
                    tv_url = f"https://www.tradingview.com/chart/?symbol=BYBIT:{sym}.P"
                    c1, c3, c5 = row['C1'], row['C3'], row['C5']
                    p_type = row['P/D']
                    
                    row_class = ""
                    whale_icon = ""
                    if row['Whale']:
                        whale_icon = " 🐳"
                        row_class = ' class="whale-pump-row"' if p_type == "PUMP" else ' class="whale-dump-row"'
                    
                    html += f"<tr{row_class}><td>{row['Time']}</td>"
                    html += f"<td><a href='{tv_url}' target='_blank' class='sym-link'>{sym}{whale_icon}</a> <small class='green-arrow'>↑{p_count}</small> <small class='red-arrow'>↓{d_count}</small></td>"
                    html += f"<td>{row['Price']}</td>"
                    html += f"<td style='color:{get_color(c1)};'>{c1:+.2f}%</td>"
                    html += f"<td style='color:{get_color(c3)}; font-weight:bold;'>{c3:+.2f}%</td>"
                    html += f"<td style='color:{get_color(c5)};'>{c5:+.2f}%</td>"
                    html += f"<td><span class='{'pump-label' if p_type=='PUMP' else 'dump-label'}'>{p_type}</span></td></tr>"
                st.markdown(html + "</table>", unsafe_allow_html=True)
            else:
                st.info("Sinyal bekleniyor... (Piyasa sakinse veri gelmeyebilir, limitleri koddan düşürebilirsiniz.)")
    
    time.sleep(1)
