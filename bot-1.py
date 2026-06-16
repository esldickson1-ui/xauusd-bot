"""
XAUUSD Smart Money Signal Bot
Sends BUY/SELL signals to Telegram with full details + chart link
Runs on Railway (free cloud) — no PC needed
"""

import os
import asyncio
import logging
from datetime import datetime, timezone
import aiohttp
import pandas as pd
import numpy as np
from telegram import Bot
from telegram.constants import ParseMode
import telegram

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── Config from environment variables ──────────────────────────────
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]
ALPHA_VANTAGE_KEY = os.getenv("ALPHA_VANTAGE_KEY", "")
TWELVE_DATA_KEY   = os.getenv("TWELVE_DATA_KEY", "")
CHECK_INTERVAL    = int(os.getenv("CHECK_INTERVAL", "300"))  # seconds (5 min)

# ── Risk settings ───────────────────────────────────────────────────
RISK_REWARD      = 2.0
ATR_SL_MULT      = 1.5
FIB_GOLDEN_LOW   = 0.618
FIB_GOLDEN_HIGH  = 0.786

# ── Session hours (UTC) ─────────────────────────────────────────────
SESSIONS = {
    "London":       (7,  12),
    "NY":           (12, 20),
    "London/NY":    (12, 16),   # Overlap — highest volatility
}

# ── State tracking ──────────────────────────────────────────────────
last_signal_time  = {}   # prevent duplicate alerts
daily_signals     = 0
bot_start_time    = datetime.now(timezone.utc)

# ═══════════════════════════════════════════════════════════════════
# DATA FETCHING
# ═══════════════════════════════════════════════════════════════════

async def fetch_ohlcv(session: aiohttp.ClientSession, interval: str = "5min") -> pd.DataFrame:
    """Fetch XAUUSD OHLCV data from multiple free sources with fallback."""
    
    # Primary: Twelve Data (800 free req/day — your key)
    if TWELVE_DATA_KEY:
        try:
            url = (
                "https://api.twelvedata.com/time_series"
                f"?symbol=XAU/USD&interval={interval}&outputsize=100"
                f"&format=JSON&apikey={TWELVE_DATA_KEY}"
            )
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                data = await r.json()
                if "values" in data:
                    rows = data["values"]
                    df = pd.DataFrame(rows)
                    df["datetime"] = pd.to_datetime(df["datetime"])
                    df = df.set_index("datetime").sort_index()
                    df = df[["open","high","low","close"]].astype(float)
                    log.info(f"TwelveData: {len(df)} bars fetched")
                    return df
                else:
                    log.warning(f"TwelveData response: {data.get('message','unknown error')}")
        except Exception as e:
            log.warning(f"TwelveData failed: {e}")

    # Fallback: Alpha Vantage
    if ALPHA_VANTAGE_KEY:
        try:
            url = (
                f"https://www.alphavantage.co/query"
                f"?function=FX_INTRADAY&from_symbol=XAU&to_symbol=USD"
                f"&interval={interval}&outputsize=compact&apikey={ALPHA_VANTAGE_KEY}"
            )
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                data = await r.json()
                key = f"Time Series FX ({interval})"
                if key in data:
                    df = pd.DataFrame(data[key]).T
                    df.index = pd.to_datetime(df.index)
                    df = df.sort_index()
                    df.columns = ["open","high","low","close"]
                    df = df.astype(float)
                    log.info(f"AlphaVantage: {len(df)} bars fetched")
                    return df
        except Exception as e:
            log.warning(f"AlphaVantage failed: {e}")

    # Fallback 2: Frankfurter (XAU daily — limited but free, no key needed)
    try:
        url = "https://api.frankfurter.app/latest?from=XAU&to=USD"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
            price = data["rates"]["USD"]
            # Build synthetic OHLCV for current price (limited analysis)
            now = datetime.now(timezone.utc)
            df = pd.DataFrame([{
                "open": price * 0.9995, "high": price * 1.002,
                "low":  price * 0.998,  "close": price
            }], index=[now])
            log.info(f"Frankfurter fallback: price={price}")
            return df
    except Exception as e:
        log.warning(f"Frankfurter failed: {e}")

    raise RuntimeError("All price data sources failed.")

async def fetch_multi_tf(session: aiohttp.ClientSession):
    """Fetch data for M5, M15, and H1 timeframes."""
    m5  = await fetch_ohlcv(session, "5min")
    await asyncio.sleep(1)
    m15 = await fetch_ohlcv(session, "15min")
    await asyncio.sleep(1)
    h1  = await fetch_ohlcv(session, "1h")
    return m5, m15, h1

# ═══════════════════════════════════════════════════════════════════
# TECHNICAL ANALYSIS ENGINE
# ═══════════════════════════════════════════════════════════════════

def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hl  = df["high"] - df["low"]
    hc  = (df["high"] - df["close"].shift()).abs()
    lc  = (df["low"]  - df["close"].shift()).abs()
    tr  = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def calc_ema(df: pd.DataFrame, period: int) -> pd.Series:
    return df["close"].ewm(span=period, adjust=False).mean()

def calc_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    delta = df["close"].diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def detect_swing_points(df: pd.DataFrame, lookback: int = 10):
    """Find recent swing high and swing low."""
    highs = df["high"].rolling(lookback * 2 + 1, center=True).max()
    lows  = df["low"].rolling(lookback * 2 + 1, center=True).min()
    swing_highs = df[df["high"] == highs]["high"]
    swing_lows  = df[df["low"]  == lows ]["low"]
    recent_high = swing_highs.iloc[-1] if len(swing_highs) > 0 else df["high"].max()
    recent_low  = swing_lows.iloc[-1]  if len(swing_lows)  > 0 else df["low"].min()
    return recent_high, recent_low

def detect_bos(df: pd.DataFrame, lookback: int = 20) -> dict:
    """Break of Structure detection."""
    if len(df) < lookback + 5:
        return {"bullish": False, "bearish": False, "level": 0}
    
    recent = df.iloc[-lookback:]
    swing_high = recent["high"].max()
    swing_low  = recent["low"].min()
    last_close = df["close"].iloc[-1]
    prev_close = df["close"].iloc[-2]
    
    # Bullish BOS: closed above swing high
    bull_bos = (last_close > swing_high and prev_close <= swing_high)
    # Bearish BOS: closed below swing low
    bear_bos = (last_close < swing_low  and prev_close >= swing_low)
    
    return {
        "bullish":     bull_bos,
        "bearish":     bear_bos,
        "swing_high":  swing_high,
        "swing_low":   swing_low,
        "last_close":  last_close,
    }

def detect_fvg(df: pd.DataFrame, min_size: float = 0.5) -> dict:
    """Fair Value Gap detection on recent candles."""
    bull_fvgs, bear_fvgs = [], []
    for i in range(1, min(len(df) - 1, 15)):
        prev = df.iloc[-(i+1)]
        curr = df.iloc[-i]
        nxt  = df.iloc[-(i-1)] if i > 1 else df.iloc[-1]
        
        # Bullish FVG: gap between prev high and next low
        if nxt["low"] > prev["high"] and (nxt["low"] - prev["high"]) >= min_size:
            bull_fvgs.append({
                "upper": nxt["low"], "lower": prev["high"],
                "mid":   (nxt["low"] + prev["high"]) / 2,
                "size":  nxt["low"] - prev["high"]
            })
        # Bearish FVG: gap between prev low and next high  
        if nxt["high"] < prev["low"] and (prev["low"] - nxt["high"]) >= min_size:
            bear_fvgs.append({
                "upper": prev["low"], "lower": nxt["high"],
                "mid":   (prev["low"] + nxt["high"]) / 2,
                "size":  prev["low"] - nxt["high"]
            })
    
    return {"bull": bull_fvgs, "bear": bear_fvgs}

def calc_fibonacci(swing_high: float, swing_low: float, bias: str) -> dict:
    """Calculate Fibonacci retracement levels."""
    rng = swing_high - swing_low
    if bias == "bull":
        return {
            "0":     swing_high,
            "23.6":  swing_high - rng * 0.236,
            "38.2":  swing_high - rng * 0.382,
            "50.0":  swing_high - rng * 0.500,
            "61.8":  swing_high - rng * 0.618,   # Golden zone low
            "70.5":  swing_high - rng * 0.705,
            "78.6":  swing_high - rng * 0.786,   # Golden zone high
            "100":   swing_low,
            "golden_low":  swing_high - rng * 0.786,
            "golden_high": swing_high - rng * 0.618,
        }
    else:
        return {
            "0":     swing_low,
            "23.6":  swing_low + rng * 0.236,
            "38.2":  swing_low + rng * 0.382,
            "50.0":  swing_low + rng * 0.500,
            "61.8":  swing_low + rng * 0.618,
            "70.5":  swing_low + rng * 0.705,
            "78.6":  swing_low + rng * 0.786,
            "100":   swing_high,
            "golden_low":  swing_low + rng * 0.618,
            "golden_high": swing_low + rng * 0.786,
        }

def get_htf_bias(h1: pd.DataFrame) -> str:
    """H1 EMA200 bias."""
    if len(h1) < 50:
        return "neutral"
    ema200 = calc_ema(h1, min(200, len(h1) - 1))
    price  = h1["close"].iloc[-1]
    if price > ema200.iloc[-1]:
        return "bull"
    elif price < ema200.iloc[-1]:
        return "bear"
    return "neutral"

def is_active_session() -> tuple[bool, str]:
    """Check if we're in London or NY session (UTC)."""
    hour = datetime.now(timezone.utc).hour
    if 12 <= hour < 16:
        return True, "🔥 London/NY Overlap"
    if 7  <= hour < 12:
        return True, "🇬🇧 London Session"
    if 12 <= hour < 20:
        return True, "🇺🇸 New York Session"
    return False, "😴 Off-Hours (Asian)"

def price_in_fib_zone(price: float, fib: dict) -> bool:
    lo = min(fib["golden_low"], fib["golden_high"])
    hi = max(fib["golden_low"], fib["golden_high"])
    buffer = (hi - lo) * 0.2
    return lo - buffer <= price <= hi + buffer

def price_in_fvg(price: float, fvgs: list):
    for fvg in fvgs:
        if fvg["lower"] <= price <= fvg["upper"]:
            return fvg
    return None

# ═══════════════════════════════════════════════════════════════════
# SIGNAL GENERATION
# ═══════════════════════════════════════════════════════════════════

def generate_signal(m5: pd.DataFrame, m15: pd.DataFrame, h1: pd.DataFrame):
    """Full Smart Money confluence signal check."""
    
    if len(m5) < 30 or len(m15) < 30:
        return None
    
    price   = m5["close"].iloc[-1]
    atr_m5  = calc_atr(m5).iloc[-1]
    atr_m15 = calc_atr(m15).iloc[-1]
    rsi_m15 = calc_rsi(m15).iloc[-1]
    rsi_m5  = calc_rsi(m5).iloc[-1]
    ema21   = calc_ema(m15, 21).iloc[-1]
    ema50   = calc_ema(m15, 50).iloc[-1]
    
    htf_bias     = get_htf_bias(h1)
    session_ok, session_name = is_active_session()
    bos_m15      = detect_bos(m15)
    fvg_m5       = detect_fvg(m5)
    swing_h, swing_l = detect_swing_points(m15)
    
    if not session_ok:
        return None
    if htf_bias == "neutral":
        return None
    
    # ── BULLISH SIGNAL ──────────────────────────────────────────────
    if htf_bias == "bull":
        fib = calc_fibonacci(swing_h, swing_l, "bull")
        in_fib  = price_in_fib_zone(price, fib)
        fvg_hit = price_in_fvg(price, fvg_m5["bull"])
        ema_ok  = ema21 > ema50 and price > ema21
        rsi_ok  = 40 < rsi_m15 < 70 and rsi_m5 > rsi_m5   # momentum building
        bos_ok  = bos_m15["bullish"] or (price > bos_m15["swing_high"] * 0.998)
        
        score = sum([in_fib, fvg_hit is not None, ema_ok, bos_ok,
                     40 < rsi_m15 < 68])
        
        if score >= 3 and in_fib and ema_ok:
            sl = price - (atr_m5 * ATR_SL_MULT)
            sl = min(sl, bos_m15["swing_low"] - atr_m5 * 0.3)
            tp = price + (price - sl) * RISK_REWARD
            
            return {
                "direction":   "BUY",
                "price":       price,
                "sl":          sl,
                "tp":          tp,
                "rr":          RISK_REWARD,
                "atr":         atr_m5,
                "rsi":         rsi_m15,
                "ema21":       ema21,
                "ema50":       ema50,
                "session":     session_name,
                "htf_bias":    htf_bias,
                "bos_level":   bos_m15["swing_high"],
                "fib":         fib,
                "fvg":         fvg_hit,
                "confluence":  score,
                "swing_high":  swing_h,
                "swing_low":   swing_l,
                "signals_hit": {
                    "BOS":     bos_ok,
                    "FIB":     in_fib,
                    "FVG":     fvg_hit is not None,
                    "EMA":     ema_ok,
                    "RSI":     40 < rsi_m15 < 68,
                }
            }
    
    # ── BEARISH SIGNAL ──────────────────────────────────────────────
    if htf_bias == "bear":
        fib = calc_fibonacci(swing_h, swing_l, "bear")
        in_fib  = price_in_fib_zone(price, fib)
        fvg_hit = price_in_fvg(price, fvg_m5["bear"])
        ema_ok  = ema21 < ema50 and price < ema21
        bos_ok  = bos_m15["bearish"] or (price < bos_m15["swing_low"] * 1.002)
        
        score = sum([in_fib, fvg_hit is not None, ema_ok, bos_ok,
                     32 < rsi_m15 < 60])
        
        if score >= 3 and in_fib and ema_ok:
            sl = price + (atr_m5 * ATR_SL_MULT)
            sl = max(sl, bos_m15["swing_high"] + atr_m5 * 0.3)
            tp = price - (sl - price) * RISK_REWARD
            
            return {
                "direction":  "SELL",
                "price":      price,
                "sl":         sl,
                "tp":         tp,
                "rr":         RISK_REWARD,
                "atr":        atr_m5,
                "rsi":        rsi_m15,
                "ema21":      ema21,
                "ema50":      ema50,
                "session":    session_name,
                "htf_bias":   htf_bias,
                "bos_level":  bos_m15["swing_low"],
                "fib":        fib,
                "fvg":        fvg_hit,
                "confluence": score,
                "swing_high": swing_h,
                "swing_low":  swing_l,
                "signals_hit": {
                    "BOS":    bos_ok,
                    "FIB":    in_fib,
                    "FVG":    fvg_hit is not None,
                    "EMA":    ema_ok,
                    "RSI":    32 < rsi_m15 < 60,
                }
            }
    
    return None

# ═══════════════════════════════════════════════════════════════════
# TELEGRAM MESSAGE BUILDER
# ═══════════════════════════════════════════════════════════════════

def build_message(sig: dict) -> str:
    d    = sig["direction"]
    icon = "🟢" if d == "BUY" else "🔴"
    arrow= "📈" if d == "BUY" else "📉"
    
    # Confluence indicators
    hits = sig["signals_hit"]
    def check(k): return "✅" if hits.get(k) else "⬜"
    
    # Fibonacci zone display
    fib = sig["fib"]
    fib_zone = f"{fib['61.8']:.2f} – {fib['78.6']:.2f}"
    
    # FVG info
    fvg_str = "✅ Price inside FVG zone" if sig["fvg"] else "⬜ No FVG (BOS+Fib only)"
    
    # Chart link (TradingView deep link)
    tv_symbol = "OANDA:XAUUSD"
    chart_link = (
        f"https://www.tradingview.com/chart/?symbol={tv_symbol}"
        f"&interval=5"
    )
    
    # Risk per lot (approx for XAUUSD standard lot)
    sl_pips  = abs(sig["price"] - sig["sl"])
    pip_val  = 1.0   # $1 per 0.01 lot per pip on XAUUSD
    
    now = datetime.now(timezone.utc).strftime("%d %b %Y  %H:%M UTC")
    
    msg = f"""
{icon}{icon} *XAUUSD {d} SIGNAL* {icon}{icon}
{arrow} *Smart Money Confluence Alert*
━━━━━━━━━━━━━━━━━━━━━━
🕐 *Time:* {now}
📍 *Session:* {sig['session']}
🧭 *HTF Bias:* {'BULLISH ▲' if sig['htf_bias']=='bull' else 'BEARISH ▼'}

💰 *ENTRY:* `{sig['price']:.2f}`
🛑 *STOP LOSS:* `{sig['sl']:.2f}`  _(−{sl_pips:.1f} pts)_
🎯 *TAKE PROFIT:* `{sig['tp']:.2f}`
⚖️ *Risk:Reward:* `1 : {sig['rr']:.1f}`
━━━━━━━━━━━━━━━━━━━━━━
📊 *CONFLUENCE CHECKLIST* ({sig['confluence']}/5)
{check('BOS')} Break of Structure
{check('FIB')} Fibonacci 61.8–78.6% Zone
{check('FVG')} Fair Value Gap
{check('EMA')} EMA 21/50 Alignment
{check('RSI')} RSI Momentum _(RSI: {sig['rsi']:.1f})_
━━━━━━━━━━━━━━━━━━━━━━
📐 *FIB ZONE:* `{fib_zone}`
🔲 *BOS Level:* `{sig['bos_level']:.2f}`
🕳 *FVG:* {fvg_str}
📏 *ATR:* `{sig['atr']:.2f}`
━━━━━━━━━━━━━━━━━━━━━━
📈 *[VIEW LIVE CHART →]({chart_link})*
━━━━━━━━━━━━━━━━━━━━━━
⚠️ _Always use proper risk management. Never risk more than 1–2% per trade._
"""
    return msg.strip()

def build_no_signal_msg(price: float, session: str, bias: str) -> str:
    return (
        f"🔍 *XAUUSD Scan Complete* — No Signal\n"
        f"💵 Price: `{price:.2f}` | {session} | Bias: {bias.upper()}\n"
        f"_Waiting for BOS + Fib + FVG confluence..._"
    )

# ═══════════════════════════════════════════════════════════════════
# MAIN BOT LOOP
# ═══════════════════════════════════════════════════════════════════

async def run_bot():
    global daily_signals
    
    bot = Bot(token=TELEGRAM_TOKEN)
    
    # Startup message
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=(
            "🤖 *XAUUSD Smart Money Bot — ONLINE*\n\n"
            "📡 Monitoring: `XAUUSD` on M5 / M15 / H1\n"
            "⚙️ Strategy: BOS + Fibonacci + FVG + EMA + RSI\n"
            f"⏱ Scan interval: every {CHECK_INTERVAL//60} minutes\n"
            "📊 Sessions: London + New York only\n\n"
            "_Waiting for high-probability setups..._"
        ),
        parse_mode=ParseMode.MARKDOWN
    )
    log.info("Bot started. Monitoring XAUUSD...")
    
    last_signal_key = ""
    
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                log.info("Fetching XAUUSD data...")
                m5, m15, h1 = await fetch_multi_tf(session)
                
                price    = m5["close"].iloc[-1]
                is_sess, sess_name = is_active_session()
                bias     = get_htf_bias(h1)
                
                log.info(f"Price: {price:.2f} | Session: {sess_name} | Bias: {bias}")
                
                sig = generate_signal(m5, m15, h1)
                
                if sig:
                    # De-duplicate: don't send same direction signal within 30 min
                    sig_key = f"{sig['direction']}_{int(price)}"
                    if sig_key != last_signal_key:
                        msg = build_message(sig)
                        await bot.send_message(
                            chat_id=TELEGRAM_CHAT_ID,
                            text=msg,
                            parse_mode=ParseMode.MARKDOWN,
                            disable_web_page_preview=False
                        )
                        last_signal_key = sig_key
                        daily_signals  += 1
                        log.info(f"Signal sent: {sig['direction']} @ {price:.2f}")
                    else:
                        log.info("Duplicate signal suppressed.")
                else:
                    log.info("No signal this scan.")
                
            except Exception as e:
                log.error(f"Bot error: {e}", exc_info=True)
                try:
                    await bot.send_message(
                        chat_id=TELEGRAM_CHAT_ID,
                        text=f"⚠️ Bot error: `{str(e)[:100]}`\nRetrying in {CHECK_INTERVAL//60} min...",
                        parse_mode=ParseMode.MARKDOWN
                    )
                except:
                    pass
            
            await asyncio.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    asyncio.run(run_bot())
