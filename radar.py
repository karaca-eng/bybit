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

# --- CONFIG ---
WHALE_VOL_3M = 60000     
WHALE_CHG_3M = 1.2       
TRI_WINDOW = 180         
MAX_DISPLAY_ROWS = 50 

FALLBACK_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXUSDT", "XRPUSDT", "ADAUSDT", "LINKUSDT", "LTCUSDT", "DOGEUSDT"]

class MarketRadar:
    def __init__(self):
        self.history = {}
        self.signals = []
        self.stats_hourly = {} 
        self.stats_daily = {}  
        self.lock = threading.RLock() 
        self.last_heartbeat = 0  
        self.total_pairs = 0
        self.log_messages = deque(maxlen=8)

    def add_log(self, msg):
        t = datetime.now().strftime("%H:%M:%S")
        with self.lock:
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
                if symbol not in self.history: self.history[symbol] = deque(maxlen=400)
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

        if (vol_3m >= WHALE_VOL_3M and abs(chg_3m) >= WHALE_CHG_3M) or (vol_3m >= 20000 and abs(chg_3m) >= 0.8):
            is_whale = vol_3m >= WHALE_VOL_3M
            t_type = "PUMP" if chg_3m > 0 else "DUMP"
            self.add_signal(symbol, current[1], chg_3m, t_type, is_whale)

    def add_signal(self, symbol, price, c3, s_type, is_whale):
        t_str = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            for s in self.signals[:3]:
                if s['Symbol'] == symbol and s['P/D'] == s_type: return
            
            if symbol not in self.stats_daily: self.stats_daily[symbol] = {"PUMP": 0, "DUMP": 0}
            self.stats_daily[symbol][s_type] += 1
            
            self.signals.insert(0, {
                "Time": t_str, "Symbol": symbol, "Price": f"{price:.2f}",
                "C3": c3, "P/D": s_type,
                "SnapP": self.stats_daily[symbol]["PUMP"], "SnapD": self.stats_daily[symbol]["DUMP"],
                "Whale": is_whale
            })

@st.cache_resource
def get_radar_instance(): return MarketRadar()

def fetch_symbols():
    # Engellenme ihtimaline karşı farklı API yolları
    for url in ["https://api.bytick.com/v5/market/instruments-info?category=linear", 
                "https://api.bybit.com/v5/market/instruments-info?category=linear"]:
        try:
            resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200:
                return [i['symbol'] for i in resp.json()['result']['list'] if i['symbol'].endswith('USDT')]
        except: continue
    return FALLBACK_SYMBOLS

async def bybit_worker(radar_obj):
    # Denenecek WebSocket adresleri
    uris = [
        "wss://stream.bybit.com/v5/public/linear",
        "wss://stream.bytick.com/v5/public/linear",
        "wss://stream-global.bybit.com/v5/public/linear"
    ]
    
    # Sahte tarayıcı başlıkları (Blokajı aşmak için en önemli kısım)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Origin": "https://www.bybit.com"
    }

    uri_index = 0
    while True:
        symbols = fetch_symbols()
        current_uri = uris[uri_index % len(uris)]
        radar_obj.add_log(f"Bağlanıyor: {current_uri.split('/')[2]}")
        
        try:
            # extra_headers ile tarayıcı taklidi yapıyoruz
            async with websockets.connect(current_uri, extra_headers=headers, ping_interval=20) as ws:
                radar_obj.add_log("Bağlantı ONLINE! Abone olunuyor...")
                
                # Abonelikleri 10'arlı gruplarla ve yavaşça gönder
                for i in range(0, len(symbols), 10):
                    chunk = symbols[i:i+10]
                    await ws.send(json.dumps({"op": "subscribe", "args": [f"tickers.{s}" for s in chunk]}))
                    await asyncio.sleep(0.4)

                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    if "topic" in data:
                        radar_obj.process_ticker(data)
        except Exception as e:
            radar_obj.add_log(f"Hata: {str(e)[:30]}")
            uri_index += 1 # Bir sonraki adresi dene
            await asyncio.sleep(8)

# --- UI (Aynı Tasarım) ---
st.set_page_config(layout="wide", page_title="Bybit Whale Radar")
st.markdown("""<style>
    .status-live { color: #00ff88; font-weight: bold; border: 1px solid #00ff88; padding: 2px 10px; border-radius: 15px; }
    .pump-label { background-color: #00ff88; color: black; padding: 2px 8px; border-radius: 4px; font-weight: bold; }
    .dump-label { background-color: #ff4b4b; color: white; padding: 2px 8px; border-radius: 4px; font-weight: bold; }
    .whale-row { background-color: rgba(0, 255, 136, 0.15) !important; border-left: 5px solid #00ff88 !important; }
</style>""", unsafe_allow_html=True)

radar = get_radar_instance()
if "started" not in st.session_state:
    threading.Thread(target=lambda: asyncio.run(bybit_worker(radar)), daemon=True).start()
    st.session_state.started = True

# Başlık
h1, h2, h3 = st.columns([2, 1, 1])
h1.title("🛡️ Bybit Whale Radar")
is_alive = (time.time() - radar.last_heartbeat) < 30
h2.markdown(f"<br>{'<span class=status-live>● ONLINE</span>' if is_alive else '<span style=color:red>● OFFLINE</span>'}", unsafe_allow_html=True)
h3.metric("Pairs", radar.total_pairs)

with st.expander("🛠️ Sistem Günlüğü"):
    for m in list(radar.log_messages)[::-1]: st.write(m)

placeholder = st.empty()

while True:
    with placeholder.container():
        with radar.lock:
            if radar.signals:
                html = "<table style='width:100%'><tr><th>Saat</th><th>Sembol</th><th>Fiyat</th><th>3m %</th><th>Tip</th></tr>"
                for row in radar.signals:
                    p_type = row['P/D']
                    r_style = " class='whale-row'" if row['Whale'] else ""
                    html += f"<tr{r_style}><td>{row['Time']}</td>"
                    html += f"<td>{row['Symbol']} {'🐋' if row['Whale'] else ''} <small>↑{row['SnapP']} ↓{row['SnapD']}</small></td>"
                    html += f"<td>{row['Price']}</td>"
                    html += f"<td style='color:{'#00ff88' if row['C3']>0 else '#ff4b4b'}'>{row['C3']:+.2f}%</td>"
                    html += f"<td><span class='{'pump-label' if p_type=='PUMP' else 'dump-label'}'>{p_type}</span></td></tr>"
                st.markdown(html + "</table>", unsafe_allow_html=True)
            else:
                st.info("Piyasa taranıyor... Eğer sistem sürekli 'Rejected' hatası veriyorsa sağ alttan Reboot App yapın.")
    time.sleep(1)
