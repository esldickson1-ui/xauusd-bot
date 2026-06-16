import os, asyncio, logging
from datetime import datetime, timezone
import aiohttp
from telegram import Bot
from telegram.constants import ParseMode

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TWELVE_DATA_KEY  = os.environ["TWELVE_DATA_KEY"]
CHECK_INTERVAL   = int(os.getenv("CHECK_INTERVAL", "300"))
RISK_REWARD      = 2.0
ATR_SL_MULT      = 1.5
last_signal_key  = ""

def mean(d): return sum(d)/len(d) if d else 0
def ema(p, n):
    if len(p)<n: return p[-1] if p else 0
    k=2/(n+1); r=mean(p[:n])
    for x in p[n:]: r=x*k+r*(1-k)
    return r
def atr(h,l,c,n=14):
    t=[max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1])) for i in range(1,len(c))]
    return mean(t[-n:]) if t else 0
def rsi(c,n=14):
    if len(c)<n+1: return 50
    g=[max(c[i]-c[i-1],0) for i in range(1,len(c))]
    ls=[max(c[i-1]-c[i],0) for i in range(1,len(c))]
    ag=mean(g[-n:]); al=mean(ls[-n:])
    return 100 if al==0 else 100-(100/(1+ag/al))
def swH(h,n=20): return max(h[-n:]) if len(h)>=n else max(h)
def swL(l,n=20): return min(l[-n:]) if len(l)>=n else min(l)
def in_fvg(price,fvgs):
    for f in fvgs:
        if f["lower"]<=price<=f["upper"]: return f
    return None
def active_session():
    h=datetime.now(timezone.utc).hour
    if 12<=h<16: return True,"🔥 London/NY Overlap"
    if 7<=h<12:  return True,"🇬🇧 London Session"
    if 16<=h<20: return True,"🇺🇸 New York Session"
    return False,"😴 Off-Hours"async def fetch_ohlcv(session, interval="5min", bars=100):
    url=(f"https://api.twelvedata.com/time_series"
         f"?symbol=XAU/USD&interval={interval}&outputsize={bars}"
         f"&format=JSON&apikey={TWELVE_DATA_KEY}")
    async with session.get(url,timeout=aiohttp.ClientTimeout(total=20)) as r:
        data=await r.json()
    if "values" not in data:
        raise RuntimeError(f"TwelveData [{interval}]: {data.get('message','no values')}")
    rows=sorted(data["values"],key=lambda x:x["datetime"])
    return ([float(x["open"]) for x in rows],[float(x["high"]) for x in rows],
            [float(x["low"]) for x in rows],[float(x["close"]) for x in rows])

async def fetch_all(session):
    m5=await fetch_ohlcv(session,"5min",100)
    await asyncio.sleep(1)
    m15=await fetch_ohlcv(session,"15min",100)
    await asyncio.sleep(1)
    h1=await fetch_ohlcv(session,"1h",60)
    return m5,m15,h1

def get_bias(h1):
    c=h1[3]; e=ema(c,min(50,len(c)-1)); p=c[-1]
    if p>e*1.001: return "bull"
    if p<e*0.999: return "bear"
    return "neutral"

def get_bos(ohlcv,n=20):
    _,h,l,c=ohlcv
    sh=swH(h,n); sl=swL(l,n)
    return {"bullish":c[-1]>sh and c[-2]<=sh,"bearish":c[-1]<sl and c[-2]>=sl,
            "swing_high":sh,"swing_low":sl}

def get_fvg(ohlcv,mn=0.30):
    _,h,l,c=ohlcv; bf=[]; brf=[]
    for i in range(2,min(len(c)-1,20)):
        ph=h[-(i+1)]; pl=l[-(i+1)]; nh=h[-(i-1)]; nl=l[-(i-1)]
        if nl>ph and nl-ph>=mn: bf.append({"upper":nl,"lower":ph})
        if nh<pl and pl-nh>=mn: brf.append({"upper":pl,"lower":nh})
    return bf,brf

def get_fib(sh,sl,bias):
    r=sh-sl
    if bias=="bull":
        return {"zone_lo":sh-r*0.786,"zone_hi":sh-r*0.618,
                "61.8":sh-r*0.618,"78.6":sh-r*0.786}
    return {"zone_lo":sl+r*0.618,"zone_hi":sl+r*0.786,
            "61.8":sl+r*0.618,"78.6":sl+r*0.786}

def in_fib(price,fib):
    lo=fib["zone_lo"]-abs(fib["zone_hi"]-fib["zone_lo"])*0.2
    hi=fib["zone_hi"]+abs(fib["zone_hi"]-fib["zone_lo"])*0.2
    return lo<=price<=hi

def get_signal(m5,m15,h1):
    _,h5,l5,c5=m5; _,h15,l15,c15=m15
    bias=get_bias(h1); sok,sess=active_session()
    if not sok or bias=="neutral": return None
    price=c5[-1]; av=atr(h5,l5,c5); rv=rsi(c15)
    e21=ema(c15,21); e50=ema(c15,50)
    bos=get_bos(m15); sh=bos["swing_high"]; sl_=bos["swing_low"]
    fib=get_fib(sh,sl_,bias); bf,brf=get_fvg(m5)
    if bias=="bull":
        ifib=in_fib(price,fib); fvg=in_fvg(price,bf)
        eok=e21>e50 and price>e21; bok=bos["bullish"] or price>sh*0.998
        rok=40<rv<70; sc=sum([ifib,fvg is not None,eok,bok,rok])
        if sc>=3 and ifib and eok:
            sl=price-av*ATR_SL_MULT; tp=price+(price-sl)*RISK_REWARD
            return {"direction":"BUY","price":price,"sl":sl,"tp":tp,
                    "atr":av,"rsi":rv,"session":sess,"bias":bias,
                    "bos_level":sh,"fib":fib,"fvg":fvg,"score":sc,
                    "hits":{"BOS":bok,"FIB":ifib,"FVG":fvg is not None,"EMA":eok,"RSI":rok}}
    if bias=="bear":
        ifib=in_fib(price,fib); fvg=in_fvg(price,brf)
        eok=e21<e50 and price<e21; bok=bos["bearish"] or price<sl_*1.002
        rok=30<rv<60; sc=sum([ifib,fvg is not None,eok,bok,rok])
        if sc>=3 and ifib and eok:
            sl=price+av*ATR_SL_MULT; tp=price-(sl-price)*RISK_REWARD
            return {"direction":"SELL","price":price,"sl":sl,"tp":tp,
                    "atr":av,"rsi":rv,"session":sess,"bias":bias,
                    "bos_level":sl_,"fib":fib,"fvg":fvg,"score":sc,
                    "hits":{"BOS":bok,"FIB":ifib,"FVG":fvg is not None,"EMA":eok,"RSI":rok}}
    return Nonedef build_msg(sig):
    d=sig["direction"]; icon="🟢" if d=="BUY" else "🔴"
    arrow="📈" if d=="BUY" else "📉"
    h=sig["hits"]; chk=lambda k:"✅" if h.get(k) else "⬜"
    fib=sig["fib"]; dist=abs(sig["price"]-sig["sl"])
    now=datetime.now(timezone.utc).strftime("%d %b %Y  %H:%M UTC")
    chart="https://www.tradingview.com/chart/?symbol=OANDA:XAUUSD&interval=5"
    return (
        f"{icon}{icon} *XAUUSD {d} SIGNAL* {icon}{icon}\n"
        f"{arrow} *Smart Money Confluence*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 *Time:* {now}\n"
        f"📍 *Session:* {sig['session']}\n"
        f"🧭 *Bias:* {'BULLISH' if sig['bias']=='bull' else 'BEARISH'}\n\n"
        f"💰 *ENTRY:* `{sig['price']:.2f}`\n"
        f"🛑 *SL:* `{sig['sl']:.2f}` _({dist:.1f} pts)_\n"
        f"🎯 *TP:* `{sig['tp']:.2f}`\n"
        f"⚖️ *RR:* `1:{RISK_REWARD:.1f}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *CONFLUENCE* ({sig['score']}/5)\n"
        f"{chk('BOS')} BOS @ `{sig['bos_level']:.2f}`\n"
        f"{chk('FIB')} Fib Zone `{fib['zone_lo']:.2f}-{fib['zone_hi']:.2f}`\n"
        f"{chk('FVG')} Fair Value Gap\n"
        f"{chk('EMA')} EMA 21/50\n"
        f"{chk('RSI')} RSI `{sig['rsi']:.1f}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 *[LIVE CHART]({chart})*\n"
        f"⚠️ _Max 1-2% risk. Always use SL._"
    )

async def run_bot():
    global last_signal_key
    bot=Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=(
            "🤖 *XAUUSD Smart Money Bot — ONLINE* ✅\n\n"
            "📡 Monitoring `XAU/USD` M5 / M15 / H1\n"
            "⚙️ BOS + Fibonacci + FVG + EMA + RSI\n"
            f"⏱ Scanning every {CHECK_INTERVAL//60} min\n"
            "📊 London + New York sessions only\n\n"
            "_Watching for setups..._"
        ),
        parse_mode=ParseMode.MARKDOWN
    )
    log.info("Bot online.")
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                log.info("Scanning...")
                m5,m15,h1=await fetch_all(session)
                price=m5[3][-1]; bias=get_bias(h1)
                _,sess=active_session()
                log.info(f"Price:{price:.2f} Bias:{bias} Session:{sess}")
                sig=get_signal(m5,m15,h1)
                if sig:
                    key=f"{sig['direction']}_{int(price)}"
                    if key!=last_signal_key:
                        await bot.send_message(
                            chat_id=TELEGRAM_CHAT_ID,
                            text=build_msg(sig),
                            parse_mode=ParseMode.MARKDOWN,
                            disable_web_page_preview=False
                        )
                        last_signal_key=key
                        log.info(f"Signal sent: {sig['direction']} @ {price:.2f}")
                    else:
                        log.info("Duplicate skipped.")
                else:
                    log.info(f"No signal. Price:{price:.2f} Bias:{bias}")
            except Exception as e:
                log.error(f"Error:{e}",exc_info=True)
                try:
                    await bot.send_message(
                        chat_id=TELEGRAM_CHAT_ID,
                        text=f"⚠️ Error: `{str(e)[:100]}`\nRetrying in {CHECK_INTERVAL//60} min...",
                        parse_mode=ParseMode.MARKDOWN
                    )
                except Exception:
                    pass
            await asyncio.sleep(CHECK_INTERVAL)

if __name__=="__main__":
    asyncio.run(run_bot())
