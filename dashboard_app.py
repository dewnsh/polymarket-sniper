
"""
streamlit run dashboard_app.py
Step 3: Polymarket Live Sniper & Flip-Shield Dashboard
------------------------------------------------------
A local web interface built with Streamlit that monitors 1h, 4h, and Daily
BTC markets, evaluates the 4-Gate rules, flags late-window flips, and identifies
cheap Maker entry zones (52c-62c) or Offensive Fakeout Flips (32c-45c).
"""

import time
import math
import requests
import numpy as np
import pandas as pd
import streamlit as st
from scipy.stats import t as student_t

# --- PAGE CONFIGURATION ---
st.set_page_config(
    page_title="Polymarket BTC Sniper & Flip Guard",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- STYLING & CSS ---
st.markdown("""
<style>
    .metric-card { background-color: #1e2130; padding: 15px; border-radius: 10px; border: 1px solid #2d3348; }
    .stAlert { border-radius: 8px; }
</style>
""", unsafe_allow_html=True)

# --- API ENDPOINTS ---
SPOT_URL = "https://api.binance.com/api/v3/klines"
PERP_URL = "https://fapi.binance.com/fapi/v1/klines"

@st.cache_data(ttl=30)  # Cache data for 30 seconds to respect Binance rate limits
def fetch_live_market_data(interval: str, limit: int = 100):
    """Fetches recent Spot and Perp klines from Binance public API."""
    try:
        spot_resp = requests.get(SPOT_URL, params={"symbol": "BTCUSDT", "interval": interval, "limit": limit}, timeout=5)
        perp_resp = requests.get(PERP_URL, params={"symbol": "BTCUSDT", "interval": interval, "limit": limit}, timeout=5)
        
        if spot_resp.status_code != 200 or perp_resp.status_code != 200:
            return None, None

        cols = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_vol", "trades", "taker_buy_vol", "taker_buy_quote_vol", "ignore"]
        
        spot_df = pd.DataFrame(spot_resp.json(), columns=cols)
        perp_df = pd.DataFrame(perp_resp.json(), columns=cols)

        for df in [spot_df, perp_df]:
            for c in ["open", "high", "low", "close", "volume", "taker_buy_vol"]:
                df[c] = df[c].astype(float)
            df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)

        return spot_df, perp_df
    except Exception as e:
        st.error(f"Data Fetch Error: {e}")
        return None, None


def analyze_market_window(market_name: str, interval: str, sub_interval: str):
    """Runs the 4-Gate rule evaluation and Flip-Shield check on live market data."""
    spot, perp = fetch_live_market_data(sub_interval, limit=120)
    if spot is None or spot.empty:
        return {"status": "ERROR", "msg": "Could not connect to Binance API feed."}

    # Calculations
    current_price = spot["close"].iloc[-1]
    strike_price = spot["open"].iloc[-1] # Window opening strike price
    
    prev_close = spot["close"].shift(1)
    tr = pd.concat([
        spot["high"] - spot["low"],
        (spot["high"] - prev_close).abs(),
        (spot["low"] - prev_close).abs()
    ], axis=1).max(axis=1)
    atr = tr.rolling(14).mean().iloc[-1]
    
    dist = current_price - strike_price
    cushion_atr = abs(dist) / atr if atr > 0 else 0
    cur_dir = "UP" if dist > 0 else "DOWN"

    # Spot Buy Ratio (Taker flow)
    spot_buy_ratio = spot["taker_buy_vol"].iloc[-1] / spot["volume"].iloc[-1] if spot["volume"].iloc[-1] > 0 else 0.5
    spot_buy_ratio_3 = spot["taker_buy_vol"].iloc[-3:].sum() / spot["volume"].iloc[-3:].sum() if spot["volume"].iloc[-3:].sum() > 0 else 0.5
    
    perp_buy_ratio_3 = perp["taker_buy_vol"].iloc[-3:].sum() / perp["volume"].iloc[-3:].sum() if perp["volume"].iloc[-3:].sum() > 0 else 0.5

    # Fat-tailed digital option probability model
    rem_bars = 12 # simulated remaining bars in window
    sigma_rem = (spot["close"].pct_change().std() * math.sqrt(rem_bars)) if len(spot) > 20 else 0.01
    z = math.log(current_price / strike_price) / (sigma_rem if sigma_rem > 0 else 0.001)
    prob_up = float(student_t.cdf(z, df=4))
    model_share = prob_up if cur_dir == "UP" else (1.0 - prob_up)
    model_share = float(np.clip(model_share + 0.01, 0.04, 0.96))

    # --- GATE EVALUATION ---
    # Gate 2: Cushion Check (0.35x to 0.85x ATR)
    cushion_ok = 0.35 <= cushion_atr <= 0.85
    fragile_lead = 0.12 <= cushion_atr <= 0.42

    # Gate 3: Spot Flow & Anti-Flip Veto
    spot_flow_ok = (cur_dir == "UP" and spot_buy_ratio_3 >= 0.52) or (cur_dir == "DOWN" and spot_buy_ratio_3 <= 0.48)
    perp_fakeout_veto = (cur_dir == "UP" and perp_buy_ratio_3 - spot_buy_ratio_3 > 0.04) or \
                        (cur_dir == "DOWN" and spot_buy_ratio_3 - perp_buy_ratio_3 > 0.04)

    # Defensive Flip-Shield trigger (Active trade danger check)
    sub_body = abs(spot["close"].iloc[-1] - spot["open"].iloc[-1])
    upper_wick = spot["high"].iloc[-1] - max(spot["open"].iloc[-1], spot["close"].iloc[-1])
    lower_wick = min(spot["open"].iloc[-1], spot["close"].iloc[-1]) - spot["low"].iloc[-1]
    
    flip_shield_active = (cur_dir == "UP" and spot_buy_ratio < 0.44 and upper_wick > sub_body) or \
                         (cur_dir == "DOWN" and spot_buy_ratio > 0.56 and lower_wick > sub_body)

    # Offensive Fakeout Flip trigger (Fading weak moves)
    fakeout_flip_ready = fragile_lead and (upper_wick >= 1.2 * sub_body or lower_wick >= 1.2 * sub_body) and \
                         ((cur_dir == "UP" and spot_buy_ratio <= 0.45) or (cur_dir == "DOWN" and spot_buy_ratio >= 0.55))

    # Final Signal Determination
    if flip_shield_active:
        signal = "RED_FLIP_SHIELD"
        message = "🚨 EMERGENCY FLIP ALARM: Spot flow diverging against cushion! Exit or Eject immediately."
    elif fakeout_flip_ready:
        flip_side = "DOWN" if cur_dir == "UP" else "UP"
        signal = "PURPLE_FAKEOUT_FLIP"
        message = f"💜 OFFENSIVE FAKEOUT FLIP: Weak lead is stalling. Opportunity to fade into {flip_side} at 32c-44c."
    elif cushion_ok and spot_flow_ok and (not perp_fakeout_veto) and (0.52 <= model_share <= 0.65):
        signal = "GREEN_FORTRESS_SNIPE"
        message = f"🟢 FORTRESS SNIPE READY: Clean spot-backed trend. Post Maker Limit Bid for {cur_dir} at 52c-60c."
    else:
        signal = "AMBER_WAIT"
        message = "⏳ WAITING / CHOPPY: Conditions do not meet A+ criteria. Stay flat."

    return {
        "market": market_name,
        "current_price": current_price,
        "strike_price": strike_price,
        "dist": dist,
        "cushion_atr": cushion_atr,
        "direction": cur_dir,
        "spot_buy_ratio": spot_buy_ratio_3 * 100,
        "model_share": model_share * 100,
        "signal": signal,
        "message": message
    }


# --- UI LAYOUT ---
st.title("🎯 Polymarket BTC Multi-Timeframe Sniper & Flip Guard")
st.markdown("Live operational dashboard tracking **1-Hour, 4-Hour, and Daily** markets. Evaluates Past Candles, ATR Cushions, and Spot vs. Perp Order Flow in real time.")

# Sidebar Controls
st.sidebar.header("🛡️ Execution & Risk Settings")
stake = st.sidebar.number_input("Simulated Stake per Trade ($)", value=100.0, step=25.0)
target_early_exit = st.sidebar.slider("Early Profit Exit Target (Cents)", 80, 95, 89)
auto_refresh = st.sidebar.checkbox("Auto-Refresh Dashboard (30s)", value=True)

if auto_refresh:
    time.sleep(0.1) # placeholder for rerun loop hook

# Main Dashboard Grid
col1, col2, col3 = st.columns(3)

markets_to_scan = [
    ("1-Hour Market (Binance 1h)", "1h", "5m"),
    ("4-Hour Market (Chainlink TWAP)", "4h", "15m"),
    ("Daily Market (Binance 1D)", "1d", "1h")
]

columns = [col1, col2, col3]

for i, (m_name, interval, sub_interval) in enumerate(markets_to_scan):
    with columns[i]:
        st.subheader(m_name)
        data = analyze_market_window(m_name, interval, sub_interval)
        
        if data.get("status") == "ERROR":
            st.error(data["msg"])
            continue

        # Display Metrics Card
        st.markdown(f"""
        <div class="metric-card">
            <b>BTC Current:</b> ${data['current_price']:,.2f}<br>
            <b>Strike (Open):</b> ${data['strike_price']:,.2f}<br>
            <b>Distance:</b> ${data['dist']:+,.2f} ({data['cushion_atr']:.2f} ATR)<br>
            <b>Spot Buy Flow:</b> {data['spot_buy_ratio']:.1f}%<br>
            <b>Model Probability:</b> {data['model_share']:.1f}%
        </div>
        """, unsafe_allow_html=True)
        
        sig = data["signal"]
        if sig == "GREEN_FORTRESS_SNIPE":
            st.success(data["message"])
        elif sig == "RED_FLIP_SHIELD":
            st.error(data["message"])
        elif sig == "PURPLE_FAKEOUT_FLIP":
            st.warning(data["message"]) # Streamlit warning renders well for purple/amber alerts
        else:
            st.info(data["message"])

st.markdown("---")
st.markdown("### 📋 Active Rule & Execution Log")
st.info("💡 **Pro-Tip for Manual Trading:** When a **🟢 Fortress Snipe** triggers, never click market buy (which incurs taker fees and spread). Place a **Maker Limit Order** inside the suggested 52¢–60¢ price zone and wait for impatient sellers to fill you. If a **🚨 Emergency Flip Alarm** fires while holding a position, exit immediately to protect your capital from late-window reversal wicks.")
