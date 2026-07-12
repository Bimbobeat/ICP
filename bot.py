import calendar
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Any

import feedparser
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ============ НАСТРОЙКИ ============
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""

RUN_MODE = "loop"  # "once" или "loop"
PRICE_CHECK_INTERVAL_MIN = 1
NEWS_CHECK_INTERVAL_MIN = 5
STATE_FILE = "icp_bot_state.json"
LOG_FILE = "icp_bot.log"

COINGECKO_ID = "internet-computer"

# Алармени прагове
ALERT_PRICE_CHANGE_PCT = [5, 10, 15, 20]
RSI_OVERSOLD = 25
RSI_OVERBOUGHT = 75
# ===================================

# Настройка на логване
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def create_session() -> requests.Session:
    """Създава сесия с автоматично повторение при грешки"""
    session = requests.Session()
    retry = Retry(
        total=3,
        read=3,
        connect=3,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504]
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


session = create_session()


def load_state() -> Dict:
    """Зарежда състоянието от файл"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Грешка при зареждане на състояние: {e}")
    return {
        "sent_news_ids": [],
        "last_verdict": None,
        "last_price": None,
        "daily_report_sent": False,
        "last_report_date": None,
        "price_alerts": {},
        "sma_cross_alert": None,
        "rsi_alert": False
    }


def save_state(state: Dict):
    """Запазва състоянието във файл"""
    try:
        with open(STATE_FILE, "w", encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Грешка при запазване на състояние: {e}")


def send_telegram(text: str, parse_mode: str = "HTML") -> bool:
    """Изпраща съобщение в Telegram"""
    if not text or not text.strip():
        return False
    
    # Ограничаваме дължината на съобщението (Telegram лимит 4096 символа)
    if len(text) > 4000:
        text = text[:4000] + "..."
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    try:
        r = session.post(url, data=payload, timeout=15)
        if not r.ok:
            logger.error(f"Telegram грешка: {r.text}")
        return r.ok
    except Exception as e:
        logger.error(f"Telegram изключение: {e}")
        return False


def fetch_fear_greed_index() -> Optional[Dict]:
    """Взима Fear & Greed Index"""
    try:
        url = "https://api.alternative.me/fng/"
        r = session.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data and data.get("data"):
            item = data["data"][0]
            return {
                "value": int(item["value"]),
                "classification": item["value_classification"],
                "timestamp": int(item["timestamp"])
            }
    except Exception as e:
        logger.warning(f"Грешка при взимане на Fear & Greed: {e}")
    return None


def fetch_btc_dominance() -> Optional[float]:
    """Взима BTC доминация"""
    try:
        url = "https://api.coingecko.com/api/v3/global"
        r = session.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        return float(data.get("data", {}).get("market_cap_percentage", {}).get("btc", 0))
    except Exception as e:
        logger.warning(f"Грешка при взимане на BTC доминация: {e}")
    return None


def fetch_current_price() -> Dict:
    """Взима текуща цена и допълнителни данни"""
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {
        "ids": COINGECKO_ID,
        "vs_currencies": "usd",
        "include_24hr_change": "true",
        "include_24hr_vol": "true",
        "include_market_cap": "true",
    }
    r = session.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()[COINGECKO_ID]


def fetch_market_data() -> Dict:
    """Взима детайлни пазарни данни"""
    try:
        url = f"https://api.coingecko.com/api/v3/coins/{COINGECKO_ID}"
        params = {
            "localization": "false",
            "tickers": "false",
            "market_data": "true",
            "community_data": "false",
            "developer_data": "false",
            "sparkline": "false"
        }
        r = session.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        market_data = data.get("market_data", {})
        
        return {
            "market_cap": market_data.get("market_cap", {}).get("usd"),
            "volume_24h": market_data.get("total_volume", {}).get("usd"),
            "ath": market_data.get("ath", {}).get("usd"),
            "atl": market_data.get("atl", {}).get("usd"),
            "ath_change_pct": market_data.get("ath_change_percentage", {}).get("usd"),
            "atl_change_pct": market_data.get("atl_change_percentage", {}).get("usd"),
        }
    except Exception as e:
        logger.error(f"Грешка при взимане на пазарни данни: {e}")
        return {}


def fetch_price_series(days: int = 200) -> List[float]:
    """Взима исторически цени"""
    try:
        url = f"https://api.coingecko.com/api/v3/coins/{COINGECKO_ID}/market_chart"
        params = {"vs_currency": "usd", "days": days}
        r = session.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        return [p[1] for p in data["prices"]]
    except Exception as e:
        logger.error(f"Грешка при взимане на исторически цени: {e}")
        return []


def calculate_sma(values: List[float], period: int) -> Optional[float]:
    """Изчислява SMA"""
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def calculate_rsi(values: List[float], period: int = 14) -> Optional[float]:
    """Изчислява RSI"""
    if len(values) < period + 1:
        return None
    
    gains = 0.0
    losses = 0.0
    
    for i in range(len(values) - period, len(values)):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    
    avg_gain = gains / period
    avg_loss = losses / period
    
    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_momentum(values: List[float], period: int = 10) -> Optional[float]:
    """Изчислява Momentum"""
    if len(values) < period + 1:
        return None
    now = values[-1]
    past = values[-1 - period]
    if past == 0:
        return None
    return ((now - past) / past) * 100


def calculate_macd(values: List[float]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Изчислява MACD (опростена версия)"""
    if len(values) < 35:
        return None, None, None
    
    # Използваме проста EMA имплементация
    def ema(data: List[float], period: int) -> List[float]:
        if len(data) < period:
            return []
        result = []
        # Първа стойност = SMA
        ema_val = sum(data[:period]) / period
        result.append(ema_val)
        multiplier = 2 / (period + 1)
        for price in data[period:]:
            ema_val = (price - ema_val) * multiplier + ema_val
            result.append(ema_val)
        return result
    
    try:
        ema12 = ema(values, 12)
        ema26 = ema(values, 26)
        
        # MACD линия
        macd_line = []
        for i in range(min(len(ema12), len(ema26))):
            macd_line.append(ema12[i] - ema26[i])
        
        # Сигнална линия (EMA9 на MACD)
        signal_line = ema(macd_line, 9)
        
        if macd_line and signal_line:
            macd_val = macd_line[-1]
            signal_val = signal_line[-1]
            histogram = macd_val - signal_val
            return macd_val, signal_val, histogram
        
        return None, None, None
    except Exception:
        return None, None, None


def calculate_bollinger_bands(values: List[float], period: int = 20, std_dev: float = 2) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Изчислява Bollinger Bands"""
    if len(values) < period:
        return None, None, None
    
    recent = values[-period:]
    sma = sum(recent) / period
    
    # Изчисляваме стандартно отклонение
    variance = sum((x - sma) ** 2 for x in recent) / period
    std = variance ** 0.5
    
    upper = sma + (std_dev * std)
    lower = sma - (std_dev * std)
    
    return upper, sma, lower


def format_price(price: float) -> str:
    """Форматира цена"""
    if price is None:
        return "N/A"
    if price >= 1000:
        return f"${price:,.2f}"
    elif price >= 100:
        return f"${price:,.2f}"
    elif price >= 1:
        return f"${price:.4f}"
    else:
        return f"${price:.6f}"


def format_large_number(num: float) -> str:
    """Форматира големи числа"""
    if num is None:
        return "N/A"
    if num >= 1e9:
        return f"${num/1e9:.2f}B"
    elif num >= 1e6:
        return f"${num/1e6:.2f}M"
    else:
        return f"${num:,.0f}"


def get_signal_emoji(verdict: str) -> str:
    """Връща емоджи за сигнала"""
    emojis = {
        "STRONG BUY": "🟢🟢",
        "BUY": "🟢",
        "HOLD": "🟡",
        "SELL": "🔴",
        "STRONG SELL": "🔴🔴"
    }
    return emojis.get(verdict, "⚪")


def compute_signal(values: List[float], market_data: Dict) -> Dict:
    """Изчислява всички индикатори и сигнали"""
    if not values:
        return {"error": "Няма ценови данни"}
    
    price = values[-1]
    
    # Основни индикатори
    sma20 = calculate_sma(values, 20)
    sma50 = calculate_sma(values, 50)
    sma200 = calculate_sma(values, 200)
    rsi = calculate_rsi(values, 14)
    momentum = calculate_momentum(values, 10)
    
    # MACD
    macd_line, macd_signal, macd_hist = calculate_macd(values)
    
    # Bollinger Bands
    bb_upper, bb_middle, bb_lower = calculate_bollinger_bands(values)
    
    # Изчисляваме сигнал
    rsi_score = 0
    if rsi is not None:
        if rsi < 30:
            rsi_score = 2
        elif rsi < 40:
            rsi_score = 1
        elif rsi > 70:
            rsi_score = -2
        elif rsi > 60:
            rsi_score = -1
    
    ma_score = 0
    if sma20 is not None and sma50 is not None:
        if price > sma20 > sma50:
            ma_score = 2
        elif price < sma20 < sma50:
            ma_score = -2
    
    mom_score = 0
    if momentum is not None:
        if momentum > 1:
            mom_score = 1
        elif momentum < -1:
            mom_score = -1
    
    macd_score = 0
    if macd_hist is not None:
        if macd_hist > 0:
            macd_score = 1
        else:
            macd_score = -1
    
    total = rsi_score + ma_score + mom_score + macd_score
    
    if total >= 3:
        verdict = "STRONG BUY"
    elif total >= 1:
        verdict = "BUY"
    elif total >= -1:
        verdict = "HOLD"
    elif total >= -3:
        verdict = "SELL"
    else:
        verdict = "STRONG SELL"
    
    # AI Score (опростен)
    ai_score = 5.0
    if rsi is not None:
        if rsi < 30:
            ai_score += 2.0
        elif rsi > 70:
            ai_score -= 2.0
    if sma20 is not None and sma50 is not None:
        if price > sma20 > sma50:
            ai_score += 1.5
        elif price < sma20 < sma50:
            ai_score -= 1.5
    if macd_hist is not None:
        if macd_hist > 0:
            ai_score += 1.0
        else:
            ai_score -= 1.0
    ai_score = max(0, min(10, ai_score))
    
    return {
        "price": price,
        "sma20": sma20,
        "sma50": sma50,
        "sma200": sma200,
        "rsi": rsi,
        "momentum": momentum,
        "macd_line": macd_line,
        "macd_signal": macd_signal,
        "macd_histogram": macd_hist,
        "bb_upper": bb_upper,
        "bb_middle": bb_middle,
        "bb_lower": bb_lower,
        "total_score": total,
        "verdict": verdict,
        "ai_score": ai_score,
        "market_cap": market_data.get("market_cap"),
        "volume_24h": market_data.get("volume_24h"),
        "ath": market_data.get("ath"),
        "atl": market_data.get("atl"),
        "ath_change_pct": market_data.get("ath_change_pct"),
    }


def format_signal_message(current: Dict, sig: Dict, fear_greed: Optional[Dict], btc_dom: Optional[float]) -> str:
    """Форматира съобщението за сигнал"""
    chg = current.get("usd_24h_change", 0) or 0
    chg_emoji = "📈" if chg >= 0 else "📉"
    
    # Signal emoji
    signal_emoji = get_signal_emoji(sig["verdict"])
    
    # Fear & Greed
    fg_str = "N/A"
    if fear_greed:
        fg_str = f"{fear_greed['value']} ({fear_greed['classification']})"
    
    # BTC Dominance
    btc_str = f"{btc_dom:.1f}%" if btc_dom else "N/A"
    
    # MACD статус
    macd_status = "N/A"
    if sig["macd_histogram"] is not None:
        macd_status = "Bullish ✅" if sig["macd_histogram"] > 0 else "Bearish ❌"
    
    # RSI статус
    rsi_status = ""
    if sig["rsi"] is not None:
        if sig["rsi"] < RSI_OVERSOLD:
            rsi_status = " 🔥 Oversold"
        elif sig["rsi"] > RSI_OVERBOUGHT:
            rsi_status = " 🔥 Overbought"
    
    lines = [
        "<b>🚀 ICP MARKET UPDATE</b>",
        "",
        f"💰 <b>Price</b>: {format_price(sig['price'])}",
        f"{chg_emoji} <b>24h</b>: {chg:+.2f}%",
        "",
        f"📊 <b>Market Cap</b>",
        f"{format_large_number(sig['market_cap'])}",
        "",
        f"💵 <b>Volume</b>",
        f"{format_large_number(sig['volume_24h'])}",
        "",
        f"📉 <b>RSI(14)</b>",
        f"{sig['rsi']:.1f}{rsi_status}" if sig["rsi"] else "N/A",
        "",
        f"📈 <b>MACD</b>",
        macd_status,
        "",
        f"📊 <b>SMA20</b>",
        format_price(sig['sma20']),
        "",
        f"📊 <b>SMA50</b>",
        format_price(sig['sma50']),
        "",
        f"📊 <b>Bollinger Bands</b>",
        f"Upper: {format_price(sig['bb_upper'])}",
        f"Middle: {format_price(sig['bb_middle'])}",
        f"Lower: {format_price(sig['bb_lower'])}",
        "",
        f"🎯 <b>Signal</b>",
        f"{signal_emoji} {sig['verdict']}",
        "",
        f"🤖 <b>AI Score</b>",
        f"{sig['ai_score']:.1f} / 10",
        "",
        f"😨 <b>Fear & Greed</b>",
        fg_str,
        "",
        f"₿ <b>BTC Dominance</b>",
        btc_str,
        "",
        f"🏆 <b>ATH</b>",
        format_price(sig['ath']),
        "",
        f"📉 <b>Distance from ATH</b>",
        f"{sig['ath_change_pct']:+.1f}%" if sig["ath_change_pct"] else "N/A",
        "",
        "<i>Автоматичен технически сигнал, не е финансов съвет.</i>"
    ]
    
    # Филтрираме празни редове
    lines = [line for line in lines if line and line.strip()]
    return "\n".join(lines)


def check_price_alerts(state: Dict, old_price: float, new_price: float):
    """Проверява за ценови аларми"""
    if old_price is None:
        return
    
    change_pct = ((new_price - old_price) / old_price) * 100
    abs_change = abs(change_pct)
    
    for threshold in ALERT_PRICE_CHANGE_PCT:
        if abs_change >= threshold:
            key = f"{threshold}_{'up' if change_pct > 0 else 'down'}"
            last_alert = state["price_alerts"].get(key, {}).get("timestamp", 0)
            
            # Изпращаме аларма не по-често от веднъж на 30 минути
            if time.time() - last_alert > 1800:
                emoji = "🚀" if change_pct > 0 else "🔻"
                msg = (
                    f"{emoji} <b>Ценова аларма!</b>\n"
                    f"Цената се промени с {change_pct:+.1f}%\n"
                    f"От {format_price(old_price)} до {format_price(new_price)}"
                )
                send_telegram(msg)
                state["price_alerts"][key] = {
                    "timestamp": time.time(),
                    "price": new_price,
                    "change_pct": change_pct
                }
                save_state(state)
                break


def check_sma_cross(state: Dict, sig: Dict):
    """Проверява за пресичане на SMA"""
    if sig["sma20"] is None or sig["sma50"] is None:
        return
    
    prev_state = state.get("sma_cross_alert")
    current_state = sig["sma20"] > sig["sma50"]
    
    if prev_state is not None and current_state != prev_state:
        direction = "above" if current_state else "below"
        emoji = "📈" if current_state else "📉"
        msg = (
            f"{emoji} <b>SMA пресичане!</b>\n"
            f"SMA20 {direction} SMA50\n"
            f"Текуща цена: {format_price(sig['price'])}"
        )
        send_telegram(msg)
        state["sma_cross_alert"] = current_state
        save_state(state)


def check_rsi_alert(state: Dict, sig: Dict):
    """Проверява за RSI аларми"""
    if sig["rsi"] is None:
        return
    
    rsi_alert = sig["rsi"] < RSI_OVERSOLD or sig["rsi"] > RSI_OVERBOUGHT
    
    if rsi_alert and not state.get("rsi_alert", False):
        condition = "oversold" if sig["rsi"] < RSI_OVERSOLD else "overbought"
        emoji = "🔥" if condition == "oversold" else "⚠️"
        msg = (
            f"{emoji} <b>RSI аларма!</b>\n"
            f"RSI е {sig['rsi']:.1f}\n"
            f"{'Препоръчително купуване' if condition == 'oversold' else 'Препоръчително продаване'}"
        )
        send_telegram(msg)
        state["rsi_alert"] = rsi_alert
        save_state(state)


def format_daily_report(sig: Dict, current: Dict, fear_greed: Optional[Dict], btc_dom: Optional[float]) -> str:
    """Форматира дневен отчет"""
    chg = current.get("usd_24h_change", 0) or 0
    chg_emoji = "📈" if chg >= 0 else "📉"
    today = datetime.now().strftime("%d %B %Y")
    
    lines = [
        f"<b>📅 ДНЕВЕН ОТЧЕТ - {today}</b>",
        "",
        f"💰 <b>Цена</b>: {format_price(sig['price'])} {chg_emoji} {chg:+.2f}%",
        f"📊 <b>Market Cap</b>: {format_large_number(sig['market_cap'])}",
        f"💵 <b>Volume</b>: {format_large_number(sig['volume_24h'])}",
        "",
        f"📉 <b>RSI(14)</b>: {sig['rsi']:.1f}" if sig["rsi"] else "📉 RSI: N/A",
        f"🎯 <b>Signal</b>: {get_signal_emoji(sig['verdict'])} {sig['verdict']}",
        f"🤖 <b>AI Score</b>: {sig['ai_score']:.1f}/10",
        "",
        f"😨 <b>Fear & Greed</b>: {fear_greed['value']} ({fear_greed['classification']})" if fear_greed else "N/A",
        f"₿ <b>BTC Dominance</b>: {btc_dom:.1f}%" if btc_dom else "N/A",
        "",
        f"🏆 <b>ATH</b>: {format_price(sig['ath'])}",
        f"📉 <b>Distance from ATH</b>: {sig['ath_change_pct']:+.1f}%" if sig["ath_change_pct"] else "N/A",
        "",
        "<i>Автоматичен дневен отчет</i>"
    ]
    return "\n".join(lines)


# RSS фийдове
NEWS_FEEDS = [
    {"url": "https://cointelegraph.com/rss/tag/internet-computer", "name": "Cointelegraph", "keyword_filter": False},
    {"url": "https://www.coindesk.com/arc/outboundfeeds/rss/", "name": "CoinDesk", "keyword_filter": True},
    {"url": "https://decrypt.co/feed", "name": "Decrypt", "keyword_filter": True},
    {"url": "https://cryptoslate.com/feed/", "name": "CryptoSlate", "keyword_filter": True},
]

ICP_KEYWORD_RE = re.compile(r"\b(icp|internet computer)\b", re.IGNORECASE)


def fetch_news() -> List[Dict]:
    """Взима новини от RSS фийдове"""
    items = []
    for feed in NEWS_FEEDS:
        try:
            parsed = feedparser.parse(feed["url"])
            for entry in parsed.entries[:20]:
                title = entry.get("title", "")
                summary = entry.get("summary", "")
                if feed["keyword_filter"] and not ICP_KEYWORD_RE.search(title + " " + summary):
                    continue
                link = entry.get("link", "")
                item_id = entry.get("id") or link
                if entry.get("published_parsed"):
                    published_ts = calendar.timegm(entry["published_parsed"])
                elif entry.get("updated_parsed"):
                    published_ts = calendar.timegm(entry["updated_parsed"])
                else:
                    published_ts = int(time.time())
                items.append({
                    "id": item_id,
                    "title": title,
                    "url": link,
                    "source": feed["name"],
                    "published_on": published_ts,
                })
        except Exception as e:
            logger.error(f"Грешка при четене на RSS ({feed['name']}): {e}")
    items.sort(key=lambda x: x["published_on"])
    return items


def format_news_message(item: Dict) -> str:
    """Форматира новинарско съобщение"""
    published = datetime.fromtimestamp(item["published_on"], tz=timezone.utc)
    when = published.strftime("%d %b %Y, %H:%M UTC")
    return (
        f"📰 <b>{item['title']}</b>\n"
        f"{item['source']} · {when}\n"
        f"{item['url']}"
    )


def run_price_check(state: Dict):
    """Изпълнява проверка на цена и сигнал"""
    try:
        logger.info("Проверка на цена...")
        
        # Взимаме всички данни
        current = fetch_current_price()
        market_data = fetch_market_data()
        prices = fetch_price_series(days=200)
        
        if not prices:
            logger.error("Няма ценови данни")
            return
        
        sig = compute_signal(prices, market_data)
        
        if "error" in sig:
            logger.error(sig["error"])
            return
        
        # Взимаме Fear & Greed и BTC Dominance
        fear_greed = fetch_fear_greed_index()
        btc_dom = fetch_btc_dominance()
        
        # Проверки за аларми
        old_price = state.get("last_price")
        if old_price is not None:
            check_price_alerts(state, old_price, sig["price"])
        
        check_sma_cross(state, sig)
        check_rsi_alert(state, sig)
        
        # Изпращаме сигнал само ако се е променил
        if sig["verdict"] != state.get("last_verdict"):
            msg = format_signal_message(current, sig, fear_greed, btc_dom)
            send_telegram(msg)
            state["last_verdict"] = sig["verdict"]
            logger.info(f"Изпратен нов сигнал: {sig['verdict']}")
        else:
            logger.info(f"Сигналът е същият ({sig['verdict']}), не се праща ново съобщение.")
        
        # Запазваме текущата цена
        state["last_price"] = sig["price"]
        
        # Дневен отчет (веднъж на ден)
        today = datetime.now().strftime("%Y-%m-%d")
        if state.get("last_report_date") != today:
            daily_msg = format_daily_report(sig, current, fear_greed, btc_dom)
            send_telegram(daily_msg)
            state["last_report_date"] = today
            logger.info("Изпратен дневен отчет")
        
        save_state(state)
        
    except Exception as e:
        logger.error(f"Грешка при проверка на цена/сигнал: {e}")
        import traceback
        logger.error(traceback.format_exc())


def run_news_check(state: Dict):
    """Изпълнява проверка за нови новини"""
    try:
        logger.info("Проверка за новини...")
        items = fetch_news()
        sent_ids = set(state.get("sent_news_ids", []))
        new_items = [i for i in items if str(i["id"]) not in sent_ids]
        new_items = new_items[-5:]  # Максимум 5 новини
        
        for item in new_items:
            send_telegram(format_news_message(item))
            sent_ids.add(str(item["id"]))
            time.sleep(1)
        
        if new_items:
            state["sent_news_ids"] = list(sent_ids)[-200:]
            save_state(state)
            logger.info(f"Изпратени {len(new_items)} нови новини.")
        else:
            logger.info("Няма нови новини.")
            
    except Exception as e:
        logger.error(f"Грешка при проверка на новини: {e}")


def main():
    """Главна функция"""
    if "PUT_YOUR" in TELEGRAM_BOT_TOKEN or "PUT_YOUR" in TELEGRAM_CHAT_ID:
        logger.error("Първо попълни TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в скрипта.")
        return
    
    state = load_state()
    logger.info("Ботът е стартиран")

def main():
    """Главна функция"""
    global TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    
    # Вземи токените от environment variables (GitHub Secrets)
    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
    TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)
    
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Липсват TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID!")
        logger.error("Добави ги като Secrets в GitHub Actions")
        return
    
    # Зареди състоянието
    state = load_state()
    logger.info("Ботът е стартиран")
    
    # Тестова проверка дали Telegram работи
    test_msg = "🤖 ICP Bot стартира успешно в GitHub Actions!"
    if send_telegram(test_msg):
        logger.info("Telegram връзката работи")
    else:
        logger.error("Telegram връзката НЕ работи! Проверете токена и чат ID.")
        return
    
    # Изпълни проверката веднъж (за GitHub Actions)
    run_price_check(state)
    run_news_check(state)
    
    logger.info(f"Ботът е стартиран в режим 'loop'")
    logger.info(f"Проверка на цена на всеки {PRICE_CHECK_INTERVAL_MIN} минути")
    logger.info(f"Проверка на новини на всеки {NEWS_CHECK_INTERVAL_MIN} минути")
    logger.info("Натисни Ctrl+C за спиране")
    
    last_price_check = 0
    last_news_check = 0
    
    # Изпращаме веднага при старт
    run_price_check(state)
    run_news_check(state)
    last_price_check = time.time()
    last_news_check = time.time()
    
    while True:
        try:
            time.sleep(30)
            now = time.time()
            
            if now - last_price_check >= PRICE_CHECK_INTERVAL_MIN * 60:
                run_price_check(state)
                last_price_check = now
                
            if now - last_news_check >= NEWS_CHECK_INTERVAL_MIN * 60:
                run_news_check(state)
                last_news_check = now
                
        except KeyboardInterrupt:
            logger.info("Ботът е спрян от потребителя")
            break
        except Exception as e:
            logger.error(f"Неочаквана грешка в main loop: {e}")
            time.sleep(10)


if __name__ == "__main__":
    main()
