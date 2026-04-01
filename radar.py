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
WHALE_VOL_3M = 75000     # Balina Hacmi (USDT)
WHALE_CHG_3M = 1.4       # % Değişim
NORMAL_LIMIT_3M = 0.8
TRI_WINDOW = 180         
MAX_DISPLAY_ROWS = 50 

class MarketRadar:
    def __init__(self):
        self.history = {}
        self.signals = []
        self.stats_hourly = {} 
        self.stats_daily = {}  
        self.lock = threading.RLock() 
        self.last_heartbeat = 0  
        self.total_pairs = 0
        self.log_messages = deque(maxlen=5) # Son 5 hatayı tutar

    def add_log(self, msg):
        t = datetime.now().strftime("%H:%M:%S")
        self.log_messages.append(f"[{t}] {msg}")

    def process_ticker(self, data):
        if "data" not in data: return
        item = data["data"]
        symbol = data.get("topic", "").split(".")[-1]
        
        now = time.time()
        with self.lock:
            self.last_heartbeat = now
            try:
                price = float(item.get('lastPrice', 0))
                turnover = float(item.get('turnover24h', 0))
                if price <= 0: return

                if symbol not in self.history: 
                    self.history[symbol] = deque(maxlen=400)
                
                self.history[symbol].append((now, price, turnover))
                self.total_pairs = len(self.history)
                self.check_logic(symbol, now)
            except: pass

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
        elif vol_3m >= 25000 and abs(chg_3m) >= NORMAL_LIMIT_3M:
            res_type = "PUMP" if chg_3m > 0 else "DUMP"
            is_whale = False

        if res_type:
            past_1m = next((x for x in hist if now - x[0] <= 60), hist[0])
            c1 = ((current[1] - past_1m[1]) / past_1m[1]) * 100
            self.add_signal(symbol, current[1], c1, chg_3m, res_type, is_whale)

    def add_signal(self, symbol, price, c1, c3, s_type, is_whale):
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
                "C1": c1, "C3": c3, "P/D": s_type,
                "SnapP": self.stats_daily[symbol]["PUMP"], 
                "SnapD": self.stats_daily[symbol]["DUMP"],
                "Whale": is_whale
            })
            if len(self.signals) > MAX_DISPLAY_ROWS: self.signals.pop()

@st.cache_resource
def get_radar(): return MarketRadar()

def fetch_symbols_secure():
    """Bypass denemeli sembol çekme"""
    url = "https://api.bybit.com/v5/market/instruments-info?category=linear"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
    }
    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code == 200:
            data = response.json()
            return [item['symbol'] for item in data['result']['list'] if item['symbol'].endswith('USDT')]
        else:
            return [f"ERROR_{response.status_code}"]
    except Exception as e:
        return [f"CONN_ERROR_{str(e)[:20]}"]

async def bybit_worker(radar_obj):
    uri = "wss://stream.bybit.com/v5/public/linear"
    
    while True:
        symbols = fetch_symbols_secure()
        
        # Hata kontrolü
        if not symbols or "ERROR" in symbols[0]:
            radar_obj.add_log(f"API Hatası: {symbols}")
            await asyncio.sleep(10)
            continue
        
        radar_obj.add_log(f"{len(symbols)} sembol bulundu. Bağlanılıyor...")
        
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                # Bloklanmamak için çok yavaş abone oluyoruz
                for i in range(0, len(symbols), 10):
                    chunk = symbols[i:i+10]
                    sub_msg = {"op": "subscribe", "args": [f"tickers.{s}" for s in chunk]}
                    await ws.send(json.dumps(sub_msg))
                    await asyncio.sleep(0.5) # Streamlit IP koruması için bekleme

                radar_obj.add_log("Bağlantı başarılı, veriler akıyor...")
                
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    if "topic" in data:
                        radar_obj.process_ticker(data)
        except Exception as e:
            radar_obj.add_log(f"WebSocket Hatası: {str(e)[:30]}")
            await asyncio.sleep(5)

# --- UI ---
st.set_page_config(layout="wide", page_title="Bybit Whale Radar")

radar = get_radar()
if "started" not in st.session_state:
    threading.Thread(target=lambda: asyncio.run(bybit_worker(radar)), daemon=True).start()
    st.session_state.started = True

# CSS
st.markdown("""
    <style>
    .status-live { color: #00ff88; font-weight: bold; border: 1px solid #00ff88; padding: 2px 10px; border-radius: 15px; }
    .pump-label { background-color: #00ff88; color: black; padding: 2px 8px; border-radius: 4px; font-weight: bold; }
    .dump-label { background-color: #ff4b4b; color: white; padding: 2px 8px; border-radius: 4px; font-weight: bold; }
    .sym-link { color: #f1c40f; text-decoration: none; font-weight: bold; }
    .whale-pump-row { background-color: rgba(0, 255, 136, 0.15) !important; border-left: 5px solid #00ff88 !important; }
    .whale-dump-row { background-color: rgba(255, 75, 75, 0.15) !important; border-left: 5px solid #ff4b4b !important; }
    </style>
""", unsafe_allow_html=True)

# Üst Panel
h1, h2, h3 = st.columns([2, 1, 1])
h1.title("🛡️ Bybit Whale Radar")
is_alive = (time.time() - radar.last_heartbeat) < 30
status_html = '<span class="status-live">● ONLINE</span>' if is_alive else '<span style="color:#ff4b4b; font-weight:bold;">● OFFLINE</span>'
h2.markdown(f"<div style='margin-top:10px;'>{status_html}</div>", unsafe_allow_html=True)
h3.metric("Aktif Pariteler", radar.total_pairs)

# Sistem Günlüğü (Offline sorununu anlamak için)
with st.expander("🛠️ Sistem Günlüğü / Hata Ayıklama"):
    for msg in list(radar.log_messages)[::-1]:
        st.write(msg)

st.divider()

col_side, col_main = st.columns([1, 4])
with col_main:
    search_query = st.text_input("Sembol Filtrele", placeholder="BTC...").upper()
    placeholder_main = st.empty()

with col_side:
    st.subheader("🔥 Top 5")
    placeholder_side = st.empty()

def get_color(val):
    if val > 0.1: return "#00ff88"
    if val < -0.1: return "#ff4b4b"
    return "white"

while True:
    with placeholder_side.container():
        with radar.lock:
            sorted_stats = sorted(radar.stats_hourly.items(), key=lambda x: x[1]['PUMP'] + x[1]['DUMP'], reverse=True)[:5]
            for sym, counts in sorted_stats:
                st.markdown(f"**{sym}** <span style='color:#00ff88;'>↑{counts['PUMP']}</span> <span style='color:#ff4b4b;'>↓{counts['DUMP']}</span>", unsafe_allow_html=True)

    with placeholder_main.container():
        with radar.lock:
            display_data = [s for s in radar.signals if search_query in s['Symbol']] if search_query else radar.signals
            if display_data:
                html = "<table style='width:100%;'><tr><th>Time</th><th>Symbol</th><th>Price</th><th>1m%</th><th>3m%</th><th>Type</th></tr>"
                for row in display_data:
                    p_type = row['P/D']
                    r_class = f" class='{'whale-pump-row' if row['Whale'] else ''}{'whale-dump-row' if row['Whale'] and p_type=='DUMP' else ''}'"
                    html += f"<tr{r_class}><td>{row['Time']}</td>"
                    html += f"<td>{row['Symbol']} {'🐳' if row['Whale'] else ''}</td>"
                    html += f"<td>{row['Price']}</td>"
                    html += f"<td style='color:{get_color(row['C1'])};'>{row['C1']:+.2f}%</td>"
                    html += f"<td style='color:{get_color(row['C3'])}; font-weight:bold;'>{row['C3']:+.2f}%</td>"
                    html += f"<td><span class='{'pump-label' if p_type=='PUMP' else 'dump-label'}'>{p_type}</span></td></tr>"
                st.markdown(html + "</table>", unsafe_allow_html=True)
            else:
                st.info("Sinyal bekleniyor... Sistem günlüğünü kontrol edin.")
    
    time.sleep(1)
