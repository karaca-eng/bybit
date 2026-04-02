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
MIN_VOL_3M = 75000      
MIN_CHG_3M = 0.8        
CONFIRM_CHG_15M = 2.0   
FAST_STRIKE_CHG = 1.2   
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
        self.msg_count = 0
        self.total_pairs = 0
        self.last_reset_hour = datetime.now().hour
        self.last_reset_4h_block = datetime.now().hour // 4

    def get_15m_price(self, symbol):
        # Alternatif domainler üzerinden deneme yapar
        urls = [f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}&interval=15&limit=2",
                f"https://api.bytick.com/v5/market/kline?category=linear&symbol={symbol}&interval=15&limit=2"]
        for url in urls:
            try:
                res = requests.get(url, timeout=2, verify=False)
                if res.status_code == 200:
                    return float(res.json()['result']['list'][1][1])
            except: continue
        return None

    def check_resets(self):
        now = datetime.now()
        if now.hour != self.last_reset_hour:
            with self.lock:
                self.stats_hourly.clear()
                self.last_reset_hour = now.hour

    def process_ticker(self, data):
        if "data" not in data: return
        self.msg_count += 1
        item = data["data"]
        symbol = data.get("topic", "").split(".")[-1]
        if not symbol: return

        now = time.time()
        with self.lock:
            self.check_resets()
            self.last_heartbeat = now
            price_raw = item.get('lastPrice')
            turnover_raw = item.get('turnover24h') 
            if price_raw is None: return
            price = float(price_raw)
            turnover = float(turnover_raw) if turnover_raw else 0
            if symbol not in self.history: self.history[symbol] = deque(maxlen=600)
            self.history[symbol].append((now, price, turnover))
            self.check_logic(symbol, now)

    def check_logic(self, symbol, now):
        hist = list(self.history[symbol])
        if len(hist) < 5: return
        current = hist[-1]
        past_1m = next((x for x in hist if now - x[0] <= 60), hist[0])
        past_3m = next((x for x in hist if now - x[0] <= TRI_WINDOW), hist[0])
        c1, c3 = ((current[1]-past_1m[1])/past_1m[1])*100, ((current[1]-past_3m[1])/past_3m[1])*100
        vol_3m, vol_1m = current[2]-past_3m[2], current[2]-past_1m[2]

        if abs(c1) >= FAST_STRIKE_CHG and vol_1m >= 40000:
            self.add_signal(symbol, current[1], c1, 0, vol_1m, "PUMP" if c1 > 0 else "DUMP", "⚡ FLASH")
            return 
        if vol_3m >= MIN_VOL_3M and abs(c3) >= MIN_CHG_3M:
            price_15m_ago = self.get_15m_price(symbol)
            if price_15m_ago:
                c15 = ((current[1]-price_15m_ago)/price_15m_ago)*100
                if (c3 > 0 and c15 > 0) or (c3 < 0 and c15 < 0):
                    if abs(c15) >= CONFIRM_CHG_15M:
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
            self.signals.insert(0, {"Time": t_str, "Symbol": sym_clean, "FullSym": symbol, "Price": f"{price:.4f}" if price < 1 else f"{price:.2f}", "Chg": chg_main, "Ref": chg_ref, "Vol": vol, "P/D": s_type, "Mode": mode, "SnapP": self.stats_4h[sym_clean]["PUMP"], "SnapD": self.stats_4h[sym_clean]["DUMP"]})
            if len(self.signals) > MAX_DISPLAY_ROWS: self.signals.pop()

@st.cache_resource
def get_radar(): return MarketRadar()

def fetch_all_bybit_symbols():
    # API Linkleri (Bybit ana ve yedek domain)
    urls = ["https://api.bytick.com/v5/market/instruments-info?category=linear", 
            "https://api.bybit.com/v5/market/instruments-info?category=linear"]
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    for url in urls:
        try:
            res = requests.get(url, headers=headers, timeout=10, verify=False)
            if res.status_code == 200:
                data = res.json()
                syms = [x['symbol'] for x in data['result']['list'] if x['symbol'].endswith('USDT') and x['status'] == 'Trading']
                if len(syms) > 50: return syms
        except: continue
    
    # EĞER API HALA ÇALIŞMAZSA: MANUEL YEDEK LİSTE (TOP 100+)
    return ["BTCUSDT","ETHUSDT","SOLUSDT","AVAXUSDT","XRPUSDT","ADAUSDT","LINKUSDT","DOTUSDT","MATICUSDT","SHIBUSDT","LTCUSDT","BCHUSDT","TRXUSDT","NEARUSDT","ICPUSDT","FILUSDT","APTUSDT","RNDRUSDT","STXUSDT","ARBUSDT","OPUSDT","TIAUSDT","SEIUSDT","INJUSDT","SUIUSDT","PEPEUSDT","WIFUSDT","BONKUSDT","FLOKIUSDT","JUPUSDT","ORDIUSDT","1000SATSUSDT","WORLDUSDT","FETUSDT","AGIXUSDT","OCEANUSDT","ANKRUSDT","GRTUSDT","LDOUSDT","EUNAUSDT","PENDLEUSDT","PYTHUSDT","STRKUSDT","DYDXUSDT","AAVEUSDT","MKRUSDT","COMPUSDT","CRVUSDT","SNXUSDT","GALAUSDT","ENJUSDT","CHZUSDT","SANDUSDT","MANAUSDT","APEUSDT","AXSUSDT","IMXUSDT","BEAMUSDT","GMTUSDT","ILVUSDT","WLDUSDT","HBARUSDT","VETUSDT","ALGOUSDT","EGLDUSDT","FLOWUSDT","THETAUSDT","ASTRUSDT","KASUSDT","MINAUSDT","RONUSDT","PIXELUSDT","MAVIAUSDT","ZETAUSDT","DYMUSDT","ALTUSDT","MANTAUSDT","XAIUSDT","AIUSDT","NFPUSDT","ACEUSDT","BEAMXUSDT","GASUSDT","STORJUSDT","BLZUSDT","TRBUSDT","LOOMUSDT","BONDUSDT","HIFIUSDT","ARKUSDT","FRONTUSDT","CYBERUSDT","WAVESUSDT","KAVAUSDT","GMXUSDT","IDUSDT"]

async def bybit_worker(radar_obj):
    uri = "wss://stream.bybit.com/v5/public/linear"
    while True:
        all_syms = fetch_all_bybit_symbols()
        radar_obj.total_pairs = len(all_syms)
        chunks = [all_syms[i:i + 10] for i in range(0, len(all_syms), 10)]
        try:
            async with websockets.connect(uri, ping_interval=20) as ws:
                for chunk in chunks:
                    await ws.send(json.dumps({"op": "subscribe", "args": [f"tickers.{s}" for s in chunk]}))
                    await asyncio.sleep(0.02)
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    if "topic" in data: radar_obj.process_ticker(data)
        except: await asyncio.sleep(5)

# --- UI ---
st.set_page_config(layout="wide", page_title="Bybit Speed Radar")
st.markdown("""<style>.main { background-color: #0e1117; } .status-live { color: #00ff88; font-weight: bold; border: 1px solid #00ff88; padding: 2px 10px; border-radius: 15px; font-size: 0.8rem; } .pump-label { background-color: #00ff88; color: black; padding: 2px 8px; border-radius: 4px; font-weight: bold; } .dump-label { background-color: #ff4b4b; color: white; padding: 2px 8px; border-radius: 4px; font-weight: bold; } .stat-card { background-color: #1e2127; padding: 10px; border-radius: 10px; margin-bottom: 10px; border-left: 5px solid #f1c40f; } table { width: 100%; border-collapse: collapse; } th, td { white-space: nowrap; padding: 12px 15px; text-align: left; border-bottom: 1px solid #222; } .sym-link { color: #f1c40f; text-decoration: none; font-weight: bold; font-size: 1.1rem; } .green-arrow { color: #00ff88; font-weight: bold; } .red-arrow { color: #ff4b4b; font-weight: bold; } .row-flash-pump { background-color: rgba(0, 255, 136, 0.18) !important; border-left: 5px solid #00ff88 !important; } .row-flash-dump { background-color: rgba(255, 75, 75, 0.18) !important; border-left: 5px solid #ff4b4b !important; } .row-conf-pump { background-color: rgba(0, 255, 136, 0.08) !important; } .row-conf-dump { background-color: rgba(255, 75, 75, 0.08) !important; }</style>""", unsafe_allow_html=True)

radar = get_radar()
if "started" not in st.session_state:
    threading.Thread(target=lambda: asyncio.run(bybit_worker(radar)), daemon=True).start()
    st.session_state.started = True

h1, h2, h3 = st.columns([2, 1, 1])
h1.title("🛡️ Speed & Conviction Radar")
is_on = (time.time() - radar.last_heartbeat) < 20
h2.markdown(f"<div style='margin-top:10px;'>{'<span class="status-live">● BYBIT ONLINE</span>' if is_on else '🔴 RECONNECTING...'}</div>", unsafe_allow_html=True)
h3.metric("Tracked Pairs", radar.total_pairs)

st.divider()
col_side, col_main = st.columns([1, 4])
with col_side:
    st.subheader("🔥 Top 5 Activity")
    with radar.lock:
        sorted_stats = sorted(radar.stats_hourly.items(), key=lambda x: x[1]['PUMP'] + x[1]['DUMP'], reverse=True)[:5]
        for sym, counts in sorted_stats:
            tv_url = f"https://www.tradingview.com/chart/?symbol=BYBIT:{sym}USDT.P"
            st.markdown(f'''<div class="stat-card"><a href="{tv_url}" target="_blank" class="sym-link">{sym}</a><br><small><span class="green-arrow">↑ {counts["PUMP"]}</span> | <span class="red-arrow">↓ {counts["DUMP"]}</span></small></div>''', unsafe_allow_html=True)
with col_main:
    search_query = st.text_input("Filter...", placeholder="BTC, SOL...", label_visibility="collapsed").upper()
    placeholder = st.empty()

while True:
    with placeholder.container():
        with radar.lock:
            display = [s for s in radar.signals if search_query in s['Symbol']] if search_query else radar.signals
            if display:
                html = "<table><tr><th>Time</th><th>Symbol (4H ↑/↓)</th><th>Price</th><th>Momentum</th><th>15m Ref</th><th>Vol</th><th>Status</th><th>Type</th></tr>"
                for row in display:
                    tv_url = f"https://www.tradingview.com/chart/?symbol=BYBIT:{row['FullSym']}.P"
                    row_class = (' class="row-flash-pump"' if row['P/D']=="PUMP" else ' class="row-flash-dump"') if "FLASH" in row['Mode'] else (' class="row-conf-pump"' if row['P/D']=="PUMP" else ' class="row-conf-dump"')
                    html += f"<tr{row_class}><td>{row['Time']}</td><td><a href='{tv_url}' target='_blank' class='sym-link'>{row['Symbol']}</a> <small class='green-arrow'>↑{row['SnapP']}</small> <small class='red-arrow'>↓{row['SnapD']}</small></td><td>{row['Price']}</td><td style='font-weight:bold;'>{row['Chg']:+.2f}%</td><td>{row['Ref']:+.2f}%</td><td>{row['Vol']/1000:.0f}k</td><td><b style='color:#f1c40f;'>{row['Mode']}</b></td><td><span class='{'pump-label' if row['P/D'] == 'PUMP' else 'dump-label'}'>{row['P/D']}</span></td></tr>"
                st.markdown(html + "</table>", unsafe_allow_html=True)
            else: st.info(f"Scanning {radar.total_pairs} pairs... Waiting for signals. 🔍")
    time.sleep(1)
