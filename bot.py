import json
import logging
import os
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ============ НАСТРОЙКИ ============
TELEGRAM_BOT_TOKEN = ""  # Взема се от environment
TELEGRAM_CHAT_ID = ""    # Взема се от environment

RUN_MODE = "once"  # За GitHub Actions
STATE_FILE = "icp_bot_state.json"
LOG_FILE = "icp_bot.log"

COINGECKO_ID = "internet-computer"

# Алармени прагове
ALERT_PRICE_CHANGE_PCT = [5, 10, 15, 20]
RSI_OVERSOLD = 25
RSI_OVERBOUGHT = 75

# Тежести на отделните компоненти на сигнала.
# Промени тези числа, за да пренастроиш колко влияе всеки индикатор.
SIGNAL_WEIGHTS = {
    "rsi": 2.0,
    "trend": 2.5,     # SMA20/SMA50/SMA200 подравняване спрямо цената
    "macd": 1.5,
    "momentum": 1.0,
    "bollinger": 1.0,
}

# Хистерезис: колко близо до границата между две категории трябва
# нормализираният резултат да е, за да СМЕНИМ последния изпратен verdict.
# Пази от "трептене" между напр. BUY/HOLD при леки колебания.
VERDICT_HYSTERESIS = 0.5
# ===================================

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
    session = requests.Session()
    retry = Retry(
        total=3, read=3, connect=3,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504]
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


session = create_session()


def load_state() -> Dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Грешка при зареждане на състояние: {e}")
    return {
        "sent_news_ids": [],
        "last_verdict": None,
        "last_normalized_score": None,
        "last_price": None,
        "daily_report_sent": False,
        "last_report_date": None,
        "price_alerts": {},
        "sma_cross_alert": None,
        "rsi_alert": False,
    }


def save_state(state: Dict):
    try:
        with open(STATE_FILE, "w", encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Грешка при запазване на състояние: {e}")


def send_telegram(text: str, parse_mode: str = "HTML") -> bool:
    if not text or not text.strip():
        return False
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
    try:
        url = f"https://api.coingecko.com/api/v3/coins/{COINGECKO_ID}"
        params = {
            "localization": "false", "tickers": "false", "market_data": "true",
            "community_data": "false", "developer_data": "false", "sparkline": "false"
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
    """Дневни цени за последните `days` дни (нужни са >=200 за SMA200)."""
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


def fetch_volume_series(days: int = 30) -> List[float]:
    """Дневни обеми - за изчисляване на средния обем (потвърждение на сигнала)."""
    try:
        url = f"https://api.coingecko.com/api/v3/coins/{COINGECKO_ID}/market_chart"
        params = {"vs_currency": "usd", "days": days}
        r = session.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        return [v[1] for v in data.get("total_volumes", [])]
    except Exception as e:
        logger.warning(f"Грешка при взимане на исторически обеми: {e}")
        return []


# ============ ИНДИКАТОРИ ============

def calculate_sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def calculate_rsi(values: List[float], period: int = 14) -> Optional[float]:
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
    if len(values) < period + 1:
        return None
    now = values[-1]
    past = values[-1 - period]
    if past == 0:
        return None
    return ((now - past) / past) * 100


def calculate_ema_series(data: List[float], period: int) -> List[float]:
    """Пълна EMA серия (не само последната стойност) - нужна е за да следим
    дали MACD хистограмата се разширява или свива."""
    if len(data) < period:
        return []
    result = []
    ema_val = sum(data[:period]) / period
    result.append(ema_val)
    multiplier = 2 / (period + 1)
    for price in data[period:]:
        ema_val = (price - ema_val) * multiplier + ema_val
        result.append(ema_val)
    return result


def calculate_macd_series(values: List[float]) -> Tuple[List[float], List[float], List[float]]:
    """Връща (macd_line, signal_line, histogram) като ПЪЛНИ подравнени серии."""
    if len(values) < 35:
        return [], [], []
    ema12 = calculate_ema_series(values, 12)
    ema26 = calculate_ema_series(values, 26)
    if not ema12 or not ema26:
        return [], [], []
    offset = len(ema12) - len(ema26)  # ema12 е по-дълга поредица от ema26
    macd_line = [ema12[i + offset] - ema26[i] for i in range(len(ema26))]
    signal_line = calculate_ema_series(macd_line, 9)
    if not signal_line:
        return macd_line, [], []
    hist_offset = len(macd_line) - len(signal_line)
    histogram = [macd_line[i + hist_offset] - signal_line[i] for i in range(len(signal_line))]
    return macd_line, signal_line, histogram


def calculate_bollinger_bands(values: List[float], period: int = 20, std_dev: float = 2) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    if len(values) < period:
        return None, None, None
    recent = values[-period:]
    sma = sum(recent) / period
    variance = sum((x - sma) ** 2 for x in recent) / period
    std = variance ** 0.5
    return sma + (std_dev * std), sma, sma - (std_dev * std)


# ============ ОЦЕНКИ (score функции) ============
# Всяка връща стойност в грубия диапазон -1.5..+1.5 (положително = бичи).
# Умножават се по тежестта си от SIGNAL_WEIGHTS и се сумират.

def score_rsi(rsi: Optional[float]) -> float:
    if rsi is None:
        return 0.0
    # rsi=30 -> +1.0, rsi=50 -> 0, rsi=70 -> -1.0, но не clip-ваме рязко на 30/70
    centered = (50 - rsi) / 20
    return max(-1.5, min(1.5, centered))


def score_trend(price: float, sma20: Optional[float], sma50: Optional[float], sma200: Optional[float]) -> float:
    """Комбинира краткосрочно подравняване (SMA20/50) с дългосрочния филтър SMA200."""
    score = 0.0
    if sma20 is not None and sma50 is not None:
        if price > sma20 > sma50:
            score += 1.0
        elif price < sma20 < sma50:
            score -= 1.0
        elif sma20 > sma50:
            score += 0.3
        else:
            score -= 0.3
    if sma200 is not None:
        # дългосрочен тренд - не обръща сигнала сам, но го потвърждава/отслабва
        score += 0.6 if price > sma200 else -0.6
    return max(-1.6, min(1.6, score))


def score_macd(histogram_series: List[float]) -> float:
    if not histogram_series:
        return 0.0
    last = histogram_series[-1]
    base = 1.0 if last > 0 else -1.0
    if len(histogram_series) >= 3:
        expanding = abs(histogram_series[-1]) > abs(histogram_series[-2]) > abs(histogram_series[-3])
        if expanding:
            base *= 1.3  # импулсът се засилва - по-силна увереност
    return max(-1.3, min(1.3, base))


def score_momentum(momentum: Optional[float]) -> float:
    if momentum is None:
        return 0.0
    return max(-1.0, min(1.0, momentum / 5))  # ±5% за 10 периода = максимална оценка


def score_bollinger(price: float, bb_upper: Optional[float], bb_lower: Optional[float]) -> float:
    if bb_upper is None or bb_lower is None or bb_upper == bb_lower:
        return 0.0
    percent_b = (price - bb_lower) / (bb_upper - bb_lower)  # 0=долна лента, 1=горна лента
    if percent_b <= 0.05:
        return 1.0
    if percent_b >= 0.95:
        return -1.0
    return (0.5 - percent_b) * 1.2  # леко наклонена оценка към средата


def score_volume_confidence(volume_24h: Optional[float], avg_volume: Optional[float]) -> float:
    """НЕ дава посока - връща множител (0.8..1.2), с който се усилва/отслабва общия сигнал."""
    if not volume_24h or not avg_volume or avg_volume == 0:
        return 1.0
    ratio = volume_24h / avg_volume
    if ratio >= 1.5:
        return 1.2
    if ratio <= 0.5:
        return 0.8
    return 1.0


def format_price(price: Optional[float]) -> str:
    if price is None:
        return "N/A"
    if price >= 100:
        return f"${price:,.2f}"
    elif price >= 1:
        return f"${price:.4f}"
    else:
        return f"${price:.6f}"


def format_large_number(num: Optional[float]) -> str:
    if num is None:
        return "N/A"
    if num >= 1e9:
        return f"${num/1e9:.2f}B"
    elif num >= 1e6:
        return f"${num/1e6:.2f}M"
    return f"${num:,.0f}"


def get_signal_emoji(verdict: str) -> str:
    return {
        "STRONG BUY": "🟢🟢",
        "BUY": "🟢",
        "HOLD": "🟡",
        "SELL": "🔴",
        "STRONG SELL": "🔴🔴",
    }.get(verdict, "⚪")


def verdict_from_score(normalized: float) -> str:
    if normalized >= 5:
        return "STRONG BUY"
    elif normalized >= 1.5:
        return "BUY"
    elif normalized >= -1.5:
        return "HOLD"
    elif normalized >= -5:
        return "SELL"
    return "STRONG SELL"


def compute_signal(values: List[float], market_data: Dict, volume_series: Optional[List[float]] = None) -> Dict:
    """Претеглена, непрекъсната оценка на индикаторите вместо груби прагове.

    Логиката:
    1. Всеки индикатор дава оценка в приблизителен диапазон -1.5..+1.5.
    2. Оценките се умножават по тежестта им (SIGNAL_WEIGHTS) и се сумират -> raw_total.
    3. raw_total се умножава по множител на обема (потвърждение/отслабване).
    4. Резултатът се нормализира в диапазон -10..+10 -> normalized_score.
    5. normalized_score се мапва към verdict и ai_score (0..10).
    """
    if not values:
        return {"error": "Няма ценови данни"}

    price = values[-1]
    sma20 = calculate_sma(values, 20)
    sma50 = calculate_sma(values, 50)
    sma200 = calculate_sma(values, 200)
    rsi = calculate_rsi(values, 14)
    momentum = calculate_momentum(values, 10)
    macd_line_s, macd_signal_s, macd_hist_s = calculate_macd_series(values)
    bb_upper, bb_middle, bb_lower = calculate_bollinger_bands(values)

    volume_24h = market_data.get("volume_24h")
    avg_volume = None
    if volume_series:
        avg_volume = sum(volume_series) / len(volume_series)

    s_rsi = score_rsi(rsi) * SIGNAL_WEIGHTS["rsi"]
    s_trend = score_trend(price, sma20, sma50, sma200) * SIGNAL_WEIGHTS["trend"]
    s_macd = score_macd(macd_hist_s) * SIGNAL_WEIGHTS["macd"]
    s_mom = score_momentum(momentum) * SIGNAL_WEIGHTS["momentum"]
    s_bb = score_bollinger(price, bb_upper, bb_lower) * SIGNAL_WEIGHTS["bollinger"]
    volume_multiplier = score_volume_confidence(volume_24h, avg_volume)

    raw_total = (s_rsi + s_trend + s_macd + s_mom + s_bb) * volume_multiplier

    # Максимално теоретично възможен сбор (за нормализация в -10..10)
    max_possible = sum(SIGNAL_WEIGHTS[k] for k in ("rsi", "trend", "macd", "momentum", "bollinger")) * 1.3
    normalized = max(-10.0, min(10.0, (raw_total / max_possible) * 10))

    verdict = verdict_from_score(normalized)
    ai_score = round((normalized + 10) / 2, 1)  # -10..10 -> 0..10

    return {
        "price": price,
        "sma20": sma20,
        "sma50": sma50,
        "sma200": sma200,
        "rsi": rsi,
        "momentum": momentum,
        "macd_line": macd_line_s[-1] if macd_line_s else None,
        "macd_signal": macd_signal_s[-1] if macd_signal_s else None,
        "macd_histogram": macd_hist_s[-1] if macd_hist_s else None,
        "macd_histogram_series": macd_hist_s[-5:],  # за debug/лог, не се праща в telegram
        "bb_upper": bb_upper,
        "bb_middle": bb_middle,
        "bb_lower": bb_lower,
        "component_scores": {
            "rsi": round(s_rsi, 2), "trend": round(s_trend, 2), "macd": round(s_macd, 2),
            "momentum": round(s_mom, 2), "bollinger": round(s_bb, 2),
            "volume_multiplier": round(volume_multiplier, 2),
        },
        "total_score": round(raw_total, 2),
        "normalized_score": round(normalized, 2),
        "verdict": verdict,
        "ai_score": ai_score,
        "market_cap": market_data.get("market_cap"),
        "volume_24h": market_data.get("volume_24h"),
        "ath": market_data.get("ath"),
        "atl": market_data.get("atl"),
        "ath_change_pct": market_data.get("ath_change_pct"),
    }


def should_emit_new_verdict(state: Dict, sig: Dict) -> bool:
    """Хистерезис: сменяме изпратения verdict само ако новият е различен И
    normalized_score е излязъл достатъчно извън предишната зона, за да не
    спамим при леко трептене около границата между категории."""
    last_verdict = state.get("last_verdict")
    if last_verdict is None:
        return True
    if sig["verdict"] == last_verdict:
        return False

    last_score = state.get("last_normalized_score")
    if last_score is None:
        return True

    # Изискваме реално движение на резултата, не просто прекосяване на границата за косъм
    return abs(sig["normalized_score"] - last_score) >= VERDICT_HYSTERESIS


# ============ ФОРМАТИРАНЕ НА СЪОБЩЕНИЯ ============

def format_signal_message(current: Dict, sig: Dict, fear_greed: Optional[Dict], btc_dom: Optional[float]) -> str:
    chg = current.get("usd_24h_change", 0) or 0
    chg_emoji = "📈" if chg >= 0 else "📉"
    signal_emoji = get_signal_emoji(sig["verdict"])

    fg_str = f"{fear_greed['value']} ({fear_greed['classification']})" if fear_greed else "N/A"
    btc_str = f"{btc_dom:.1f}%" if btc_dom else "N/A"

    macd_status = "N/A"
    if sig["macd_histogram"] is not None:
        macd_status = "Bullish ✅" if sig["macd_histogram"] > 0 else "Bearish ❌"

    rsi_status = ""
    if sig["rsi"] is not None:
        if sig["rsi"] < RSI_OVERSOLD:
            rsi_status = " 🔥 Oversold"
        elif sig["rsi"] > RSI_OVERBOUGHT:
            rsi_status = " 🔥 Overbought"

    trend_str = "N/A"
    if sig["sma200"] is not None:
        trend_str = "Bullish (над SMA200) ✅" if sig["price"] > sig["sma200"] else "Bearish (под SMA200) ❌"

    lines = [
        "<b>🚀 ICP MARKET UPDATE</b>",
        "",
        f"💰 <b>Price</b>: {format_price(sig['price'])}",
        f"{chg_emoji} <b>24h</b>: {chg:+.2f}%",
        "",
        f"📊 <b>Market Cap</b>: {format_large_number(sig['market_cap'])}",
        f"💵 <b>Volume</b>: {format_large_number(sig['volume_24h'])}",
        "",
        f"📉 <b>RSI(14)</b>: {sig['rsi']:.1f}{rsi_status}" if sig["rsi"] else "📉 RSI: N/A",
        f"📈 <b>MACD</b>: {macd_status}",
        f"📊 <b>SMA20/50</b>: {format_price(sig['sma20'])} / {format_price(sig['sma50'])}",
        f"🧭 <b>Дългосрочен тренд</b>: {trend_str}",
        "",
        f"🎯 <b>Signal</b>: {signal_emoji} {sig['verdict']}",
        f"🤖 <b>AI Score</b>: {sig['ai_score']:.1f} / 10  (raw: {sig['normalized_score']:+.1f})",
        "",
        f"😨 <b>Fear & Greed</b>: {fg_str}",
        f"₿ <b>BTC Dominance</b>: {btc_str}",
        "",
        f"🏆 <b>ATH</b>: {format_price(sig['ath'])}",
        f"📉 <b>Distance from ATH</b>: {sig['ath_change_pct']:+.1f}%" if sig["ath_change_pct"] else "N/A",
        "",
        "<i>Автоматичен технически сигнал, не е финансов съвет.</i>",
    ]
    return "\n".join(line for line in lines if line and line.strip())


def format_daily_report(sig: Dict, current: Dict, fear_greed: Optional[Dict], btc_dom: Optional[float]) -> str:
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
        "<i>Автоматичен дневен отчет</i>",
    ]
    return "\n".join(lines)


# ============ АЛАРМИ ============

def check_price_alerts(state: Dict, old_price: float, new_price: float):
    if old_price is None:
        return
    change_pct = ((new_price - old_price) / old_price) * 100
    abs_change = abs(change_pct)
    for threshold in ALERT_PRICE_CHANGE_PCT:
        if abs_change >= threshold:
            key = f"{threshold}_{'up' if change_pct > 0 else 'down'}"
            last_alert = state["price_alerts"].get(key, {}).get("timestamp", 0)
            if time.time() - last_alert > 1800:
                emoji = "🚀" if change_pct > 0 else "🔻"
                msg = (
                    f"{emoji} <b>Ценова аларма!</b>\n"
                    f"Цената се промени с {change_pct:+.1f}%\n"
                    f"От {format_price(old_price)} до {format_price(new_price)}"
                )
                send_telegram(msg)
                state["price_alerts"][key] = {
                    "timestamp": time.time(), "price": new_price, "change_pct": change_pct
                }
                save_state(state)
                break


def check_sma_cross(state: Dict, sig: Dict):
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
    elif not rsi_alert and state.get("rsi_alert", False):
        state["rsi_alert"] = False
        save_state(state)


# ============ ОСНОВЕН ЦИКЪЛ ============

def run_price_check(state: Dict):
    try:
        logger.info("Проверка на цена...")
        current = fetch_current_price()
        market_data = fetch_market_data()
        prices = fetch_price_series(days=200)
        volumes = fetch_volume_series(days=30)

        if not prices:
            logger.error("Няма ценови данни")
            return

        sig = compute_signal(prices, market_data, volume_series=volumes)
        if "error" in sig:
            logger.error(sig["error"])
            return

        fear_greed = fetch_fear_greed_index()
        btc_dom = fetch_btc_dominance()

        old_price = state.get("last_price")
        if old_price is not None:
            check_price_alerts(state, old_price, sig["price"])

        check_sma_cross(state, sig)
        check_rsi_alert(state, sig)

        if should_emit_new_verdict(state, sig):
            msg = format_signal_message(current, sig, fear_greed, btc_dom)
            send_telegram(msg)
            state["last_verdict"] = sig["verdict"]
            state["last_normalized_score"] = sig["normalized_score"]
            logger.info(
                f"Изпратен нов сигнал: {sig['verdict']} "
                f"(score={sig['normalized_score']:+.2f}, components={sig['component_scores']})"
            )
        else:
            logger.info(
                f"Сигналът остава {sig['verdict']} (score={sig['normalized_score']:+.2f}), "
                f"не се праща ново съобщение."
            )

        state["last_price"] = sig["price"]

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


def main():
    global TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
    TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("❌ Липсват TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID!")
        logger.error("Добави ги като Secrets в GitHub Actions")
        return

    state = load_state()
    logger.info("🤖 ICP Ботът е стартиран (само сигнали, без новини)")

    test_msg = "🤖 ICP Bot стартира успешно!\n📊 Ще получавате само ценови сигнали (без новини)."
    if send_telegram(test_msg):
        logger.info("✅ Telegram връзката работи")
    else:
        logger.error("❌ Telegram връзката НЕ работи! Проверете токена и чат ID.")
        return

    run_price_check(state)
    logger.info("✅ Ботът завърши успешно!")


if __name__ == "__main__":
    main()
