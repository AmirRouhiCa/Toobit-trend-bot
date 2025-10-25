# toobit_trend_bot.py
import os, time, math, asyncio, logging, requests, pytz, random
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from telegram import Bot

"""
نسخه ساده و پایدار:
- داده کندل از Bybit (دسته linear) برای پایداری
- 3 معیار: اسپایک حجم، شکست مقاومت کوتاه‌مدت، قیمت بالای VWAP روزانه
- هشدار کانال + گزارش هر 30 دقیقه
- مناسب اجرای 24/7 روی Render

نکته: اگر نیاز داشتید مستقیماً API توبیت را جایگزین کنیم، انجام می‌شود.
"""

# --------- تنظیمات عمومی از ENV یا مقادیر پیش‌فرض ---------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")  # @channel یا -100...
SYMBOLS = os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,TONUSDT,PEPEUSDT,LINKUSDT,APTUSDT,OPUSDT").split(",")

SCAN_INTERVAL_SEC   = int(os.getenv("SCAN_INTERVAL_SEC", "20"))   # فاصله اسکن
HISTORY_LIMIT       = int(os.getenv("HISTORY_LIMIT", "200"))      # تعداد کندل
VOL_SMA_LEN         = int(os.getenv("VOL_SMA_LEN", "30"))         # SMA حجم
RES_BREAK_LOOKBACK  = int(os.getenv("RES_BREAK_LOOKBACK", "20"))  # مقاومت
CONFIRM_TF          = os.getenv("CONFIRM_TF", "5")                # 5m
VOL_SPIKE_MIN       = float(os.getenv("VOL_SPIKE_MIN", "2.0"))    # حداقل شدت حجم
MIN_VWAP_GAP        = float(os.getenv("MIN_VWAP_GAP", "0.001"))   # 0.1%
COOLDOWN_MINUTES    = int(os.getenv("COOLDOWN_MINUTES", "20"))
REPORT_EVERY_MIN    = int(os.getenv("REPORT_EVERY_MIN", "30"))

# --------- لاگ ---------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("toobit-trend")

# --------- ابزار داده ---------
def bybit_kline(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    url = "https://api.bybit.com/v5/market/kline"
    params = {"category":"linear","symbol":symbol,"interval":interval,"limit":str(limit)}
    r = requests.get(url, params=params, timeout=12)
    r.raise_for_status()
    data = r.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit error: {data.get('retMsg')}")
    rows = data["result"]["list"]
    df = pd.DataFrame(rows, columns=["start","open","high","low","close","volume","turnover"])
    # تبدیل
    for col in ["open","high","low","close","volume","turnover"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["start"] = pd.to_datetime(df["start"].astype(np.int64), unit="ms", utc=True)
    df.sort_values("start", inplace=True); df.reset_index(drop=True, inplace=True)
    return df

def daily_vwap(df: pd.DataFrame) -> float:
    if df.empty: return np.nan
    now = datetime.now(timezone.utc)
    day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    dft = df[df["start"] >= day_start]
    if dft.empty: dft = df
    tp = (dft["high"] + dft["low"] + dft["close"]) / 3.0
    denom = max(dft["volume"].sum(), 1e-9)
    return float((tp * dft["volume"]).sum() / denom)

def last_resistance_break(df: pd.DataFrame, lookback: int) -> bool:
    if len(df) < lookback + 1: return False
    last_close = df["close"].iloc[-1]
    prev_high  = df["high"].iloc[-(lookback+1):-1].max()
    return last_close > prev_high

def volume_spike_ratio(df: pd.DataFrame, sma_len: int) -> float:
    if len(df) < sma_len + 1: return 0.0
    vol_sma = df["volume"].rolling(sma_len).mean().iloc[-2]  # مرجع تا کندل قبل
    last_vol = df["volume"].iloc[-1]
    if vol_sma <= 0: return 0.0
    return float(last_vol / vol_sma)

def fmt_x(x: float) -> str:
    return f"x{round(x,1)}"

# --------- پیام رسان ---------
async def send(bot: Bot, text: str):
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)

# ضداسپم
class Cooldown:
    def __init__(self, minutes: int): self.m, self.t = minutes, {}
    def allow(self, key: str) -> bool:
        now = time.time(); last = self.t.get(key, 0.0)
        if now - last >= self.m * 60: self.t[key] = now; return True
        return False

cool = Cooldown(COOLDOWN_MINUTES)

async def alert(bot: Bot, symbol: str, vol_ratio: float, above_vwap: bool, broke_res: bool,
                tf="1m", confirm_tf="5m", price=None, vwap_val=None):
    lines = [
        "Trend Detected ✅",
        f"Symbol: {symbol}",
        f"Volume: {fmt_x(vol_ratio)} 🔥",
        f"Price > VWAP: {'✅' if above_vwap else '❌'}  |  R-break: {'✅' if broke_res else '❌'}",
        f"TF: {tf} (confirm: {confirm_tf})"
    ]
    if price is not None and vwap_val is not None and vwap_val > 0:
        gap = (price - vwap_val)/vwap_val*100
        lines.append(f"Price: {price:.4f} | VWAP(d): {vwap_val:.4f} | ΔVWAP: {gap:.2f}%")
    await send(bot, "\n".join(lines))

async def scan_symbol(bot: Bot, symbol: str):
    try:
        df1 = bybit_kline(symbol, "1", HISTORY_LIMIT)
        if df1.empty or len(df1) < max(VOL_SMA_LEN+1, RES_BREAK_LOOKBACK+1): return

        last_close = df1["close"].iloc[-1]
        vwap_d     = daily_vwap(df1)
        above_vwap = (not math.isnan(vwap_d)) and (last_close > vwap_d*(1+MIN_VWAP_GAP))
        broke_res  = last_resistance_break(df1, RES_BREAK_LOOKBACK)
        vol_ratio  = volume_spike_ratio(df1, VOL_SMA_LEN)

        if vol_ratio < VOL_SPIKE_MIN: return

        # تایید 5m
        df5 = bybit_kline(symbol, CONFIRM_TF, max(50, HISTORY_LIMIT//5))
        vwap5 = daily_vwap(df5) if not df5.empty else np.nan
        confirm_ok = True if math.isnan(vwap5) else (df5["close"].iloc[-1] > vwap5)

        if vol_ratio >= VOL_SPIKE_MIN and above_vwap and broke_res and confirm_ok:
            if cool.allow(symbol):
                await alert(bot, symbol, vol_ratio, above_vwap, broke_res,
                            tf="1m", confirm_tf=f"{CONFIRM_TF}m",
                            price=last_close, vwap_val=vwap_d)
    except Exception as e:
        log.warning(f"[{symbol}] scan error: {e}")

async def periodic_report(bot: Bot):
    ranks = []
    for s in SYMBOLS:
        try:
            df1 = bybit_kline(s, "1", VOL_SMA_LEN+5)
            r = volume_spike_ratio(df1, VOL_SMA_LEN)
            ranks.append((s, r))
        except Exception:
            pass
        await asyncio.sleep(0.15 + random.random()*0.15)
    ranks.sort(key=lambda x: x[1], reverse=True)
    top = ranks[:5]
    if not top: return
    lines = ["📊 Top Volume Intensity (1m)"]
    for s, r in top:
        badge = "🚀" if r >= 5 else "🔥" if r >= 3 else "⚡" if r >= 2 else "•"
        lines.append(f"{badge} {s}: {fmt_x(r)}")
    await send(bot, "\n".join(lines))

async def main_loop():
    assert TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, "ENV vars missing"
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await send(bot, "✅ Trend bot started on Render.")

    next_report_at = datetime.now(timezone.utc) + timedelta(minutes=REPORT_EVERY_MIN)
    while True:
        t0 = time.time()
        await asyncio.gather(*[scan_symbol(bot, s) for s in SYMBOLS])
        if datetime.now(timezone.utc) >= next_report_at:
            try: await periodic_report(bot)
            except Exception as e: log.warning(f"report error: {e}")
            next_report_at = datetime.now(timezone.utc) + timedelta(minutes=REPORT_EVERY_MIN)
        dt = time.time() - t0
        await asyncio.sleep(max(1.0, SCAN_INTERVAL_SEC - dt))

if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        log.info("Stopped by user.")
