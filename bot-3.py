import os
import asyncio
import logging
from datetime import datetime, timezone
import aiohttp
from telegram import Bot
from telegram.constants import ParseMode

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]
TWELVE_DATA_KEY   = os.environ["TWELVE_DATA_KEY"]
CHECK_INTERVAL    = int(os.getenv("CHECK_INTERVAL", "300"))
RISK_REWARD       = 2.0
ATR_SL_MULT       = 1.5
last_signal_key   = ""def mean(data):
    return sum(data) / len(data) if data else 0

def ema(prices, period):
    if len(prices) < period:
        return prices[-1] if prices else 0
    k = 2 / (period + 1)
    result = mean(prices[:period])
    for p in prices[period:]:
        result = p * k + result * (1 - k)
    return result

def atr(highs, lows, closes, period=14):
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i]  - closes[i-1])
        )
        trs.append(tr)
    return mean(trs[-period:]) if trs else 0

def rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = mean(gains[-period:])
    avg_loss = mean(losses[-period:])
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def swing_high(highs, lookback=20):
    return max(highs[-lookback:]) if len(highs) >= lookback else max(highs)

def swing_low(lows, lookback=20):
    return min(lows[-lookback:]) if len(lows) >= lookback else min(lows)async def fetch_ohlcv(session, interval="5min", bars=100):
    url = (
        f"https://api.twelvedata.com/time_series"
        f"?symbol=XAU/USD&interval={interval}&outputsize={bars}"
        f"&format=JSON&apikey={TWELVE_DATA_KEY}"
    )
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
        data = await r.json()
    if "values" not in data:
        raise RuntimeError(f"TwelveData error [{interval}]: {data.get('message','no values')}")
    rows   = sorted(data["values"], key=lambda x: x["datetime"])
    opens  = [float(x["open"])  for x in rows]
    hdef generate_signal(m5, m15, h1):
    _, h5,  l5,  c5  = m5
    _, h15, l15, c15 = m15

    bias         = htf_bias(h1)
    sess_ok, sess = active_session()
    if not sess_ok or bias == "neutral":
        return None

    price    = c5[-1]
    atr_val  = atr(h5, l5, c5)
    rsi_val  = rsi(c15)
    e21      = ema(c15, 21)
    e50      = ema(c15, 50)
    bos      = detect_bos(m15)
    sh       = bos["swing_high"]
    sl_      = bos["swing_low"]
    fib      = fib_levels(sh, sl_, bias)
    bull_fvg, bear_fvg = detect_fvg(m5)

    if bias == "bull":
        in_fib  = in_fib_zone(price, fib)
        fvg_hit = in_fvg(price, bull_fvg)
        ema_ok  = e21 > e50 and price > e21
        bos_ok  = bos["bullish"] or price > sh * 0.998
        rsi_ok  = 40 < rsi_val < 70
        score   = sum([in_fib, fvg_hit is not None, ema_ok, bos_ok, rsi_ok])
        if score >= 3 and in_fib and ema_ok:
            sl_price = price - atr_val * ATR_SL_MULT
            tp_price = price + (price - sl_price) * RISK_REWARD
            return {
                "direction": "BUY", "price": price,
                "sl": sl_price, "tp": tp_price,
                "atr": atr_val, "rsi": rsi_val,
                "session": sess, "bias": bias,
                "bos_level": sh, "fib": fib,
                "fvg": fvg_hit, "score": score,
                "hits": {"BOS": bos_ok, "FIB": in_fib,
                         "FVG": fvg_hit is not None,
                         "EMA": ema_ok, "RSI": rsi_ok},
            }

    if bias == "bear":
        in_fib  = in_fib_zone(price, fib)
        fvg_hit = in_fvg(price, bear_fvg)
        ema_ok  = e21 < e50 and price < e21
        bos_ok  = bos["bearish"] or price < sl_ * 1.002
        rsi_ok  = 30 < rsi_val < 60
        score   = sum([in_fib, fvg_hit is not None, ema_ok, bos_ok, rsi_ok])
        if score >= 3 and in_fib and ema_ok:
            sl_price = price + atr_val * ATR_SL_MULT
            tp_price = price - (sl_price - price) * RISK_REWARD
            return {
                "direction": "SELL", "price": price,
                "sl": sl_price, "tp": tp_price,
                "atr": atr_val, "rsi": rsi_val,
                "session": sess, "bias": bias,
                "bos_level": sl_, "fib": fib,
                "fvg": fvg_hit, "score": score,
                "hits": {"BOS": bos_ok, "FIB": in_fib,
                         "FVG": fvg_hit is not None,
                         "EMA": ema_ok, "RSI": rsi_ok},
            }
    return Nonedef build_message(sig):
    d     = sig["direction"]
    icon  = "🟢" if d == "BUY" else "🔴"
    arrow = "📈" if d == "BUY" else "📉"
    hits  = sig["hits"]
    chk   = lambda k: "✅" if hits.get(k) else "⬜"
    fib   = sig["fib"]
    sl_dist = abs(sig["price"] - sig["sl"])
    now   = datetime.now(timezone.utc).strftime("%d %b %Y  %H:%M UTC")
    chart = "https://www.tradingview.com/chart/?symbol=OANDA:XAUUSD&interval=5"

    return (
        f"{icon}{icon} *XAUUSD {d} SIGNAL* {icon}{icon}\n"
        f"{arrow} *Smart Money Confluence*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 *Time:* {now}\n"
        f"📍 *Session:* {sig['session']}\n"
        f"🧭 *HTF Bias:* {'BULLISH ▲' if sig['bias']=='bull' else 'BEARISH ▼'}\n\n"
        f"💰 *ENTRY:* `{sig['price']:.2f}`\n"
        f"🛑 *STOP LOSS:* `{sig['sl']:.2f}` _({sl_dist:.1f} pts)_\n"
        f"🎯 *TAKE PROFIT:* `{sig['tp']:.2f}`\n"
        f"⚖️ *Risk:Reward:* `1 : {RISK_REWARD:.1f}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *CONFLUENCE* ({sig['score']}/5)\n"
        f"{chk('BOS')} Break of Structure @ `{sig['bos_level']:.2f}`\n"
        f"{chk('FIB')} Fib 61.8-78.6% `{fib['zone_lo']:.2f}-{fib['zone_hi']:.2f}`\n"
        f"{chk('FVG')} Fair Value Gap\n"
        f"{chk('EMA')} EMA 21/50 Alignment\n"
        f"{chk('RSI')} RSI Momentum `{sig['rsi']:.1f}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📏 *ATR:* `{sig['atr']:.2f}`\n"
        f"📈 *[LIVE CHART]({chart})*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ _Max 1-2% risk per trade. Always use SL._"async def run_bot():
    global last_signal_key
    bot = Bot(token=TELEGRAM_TOKEN)

    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=(
            "🤖 *XAUUSD Smart Money Bot v3 — ONLINE* ✅\n\n"
            "📡 Monitoring: `XAU/USD` on M5 / M15 / H1\n"
            "⚙️ Strategy: BOS + Fibonacci + FVG + EMA + RSI\n"
            f"⏱ Scanning every {CHECK_INTERVAL//60} minutes\n"
            "📊 Active during: London + New York sessions\n\n"
            "_Watching for high-probability setups..._"
        ),
        parse_mode=ParseMode.MARKDOWN
    )
    log.info("Bot v3 online. Monitoring XAUUSD...")

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                log.info("Scanning XAUUSD...")
                m5, m15, h1 = await fetch_all(session)
                price = m5[3][-1]
                sess_ok, sess = active_session()
                bias = htf_bias(h1)
                log.info(f"Price: {price:.2f} | {sess} | Bias: {bias}")

                sig = generate_signal(m5, m15, h1)

                if sig:
                    key = f"{sig['direction']}_{int(price)}"
                    if key != last_signal_key:
                        await bot.send_message(
                            chat_id=TELEGRAM_CHAT_ID,
                            text=build_message(sig),
                            parse_mode=ParseMode.MARKDOWN,
                            disable_web_page_preview=False
                        )
                        last_signal_key = key
                        log.info(f"Signal sent: {sig['direction']} @ {price:.2f}")
                    else:
                        log.info("Duplicate signal — skipped.")
                else:
                    log.info(f"No signal | Price: {price:.2f} | Bias: {bias}")

            except Exception as e:
                log.error(f"Error: {e}", exc_info=True)
                try:
                    await bot.send_message(
                        chat_id=TELEGRAM_CHAT_ID,
                        text=f"⚠️ Bot error: `{str(e)[:120]}`\nRetrying in {CHECK_INTERVAL//60} min...",
                        parse_mode=ParseMode.MARKDOWN
                    )
                except Exception:
                    pass

            await asyncio.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    asyncio.run(run_bot())
)
