import calendar
import json
import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any

import feedparser
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

# ============ ТВОИТЕ НАСТРОЙКИ ============
TARGET_ICP = 1000  # Цел: 1000 ICP
CURRENT_ICP = 85   # Текущо притежание
BUY_ZONE_LOW = 2.10   # Долна граница за покупка
BUY_ZONE_HIGH = 2.50  # Горна граница за покупка
SELL_TARGETS = [3.00, 3.50, 4.00, 5.00]  # Цели за продажба
# ===================================

# Алармени прагове
ALERT_PRICE_CHANGE_PCT = [3, 5, 8, 10]
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
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
        "last_buy_signal": None,
        "last_sell_signal": None,
        "daily_report_sent": False,
        "last_report_date": None,
        "price_alerts": {},
        "sma_cross_alert": None,
        "rsi_alert": False,
        "news_sent_today": 0,
        "last_news_date": None
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
    
    def ema(data: List[float], period: int) -> List[float]:
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
    
    try:
        ema12 = ema(values, 12)
        ema26 = ema(values, 26)
        
        macd_line = []
        for i in range(min(len(ema12), len(ema26))):
            macd_line.append(ema12[i] - ema26[i])
        
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
        "STRONG SELL": "🔴🔴",
        "BUY ZONE": "🟢🔥",
        "SELL ZONE": "🔴🔥"
    }
    return emojis.get(verdict, "⚪")


def compute_personalized_signal(price: float, rsi: Optional[float], sma20: Optional[float], sma50: Optional[float]) -> Dict:
    """Изчислява персонализиран сигнал според твоята стратегия"""
    
    # ===== СИГНАЛ ЗА ПОКУПКА =====
    buy_signals = 0
    buy_reasons = []
    
    # 1. Цената е в зоната за покупка
    if BUY_ZONE_LOW <= price <= BUY_ZONE_HIGH:
        buy_signals += 2
        buy_reasons.append(f"✅ Цената е в зоната за покупка (${price:.4f})")
    elif price < BUY_ZONE_LOW:
        buy_signals += 3
        buy_reasons.append(f"🔥 Цената е ПОД зоната за покупка - ОТЛИЧЕН МОМЕНТ! (${price:.4f})")
    elif price < BUY_ZONE_HIGH * 1.1:
        buy_signals += 1
        buy_reasons.append(f"📈 Цената е близо до зоната за покупка (${price:.4f})")
    
    # 2. RSI - свръхпродаденост
    if rsi is not None:
        if rsi < 30:
            buy_signals += 2
            buy_reasons.append(f"🔥 RSI е {rsi:.1f} - СВРЪХПРОДАДЕНО!")
        elif rsi < 40:
            buy_signals += 1
            buy_reasons.append(f"📉 RSI е {rsi:.1f} - близо до свръхпродаденост")
    
    # 3. Цената е под SMA50 (добра за покупка)
    if sma50 is not None and price < sma50:
        buy_signals += 1
        buy_reasons.append(f"📊 Цената е под SMA50 (${sma50:.4f}) - добра за покупка")
    
    # 4. Цената е под SMA20
    if sma20 is not None and price < sma20:
        buy_signals += 1
        buy_reasons.append(f"📊 Цената е под SMA20 (${sma20:.4f})")
    
    # ===== СИГНАЛ ЗА ПРОДАЖБА =====
    sell_signals = 0
    sell_reasons = []
    
    # 1. Цената е над целите за продажба
    for target in SELL_TARGETS:
        if price >= target:
            sell_signals += 2
            sell_reasons.append(f"💰 Цената достигна цел ${target:.2f}!")
            break
        elif price >= target * 0.95:
            sell_signals += 1
            sell_reasons.append(f"📈 Цената е близо до цел ${target:.2f} (на {((price/target)*100):.1f}%)")
    
    # 2. RSI - свръхкупеност
    if rsi is not None:
        if rsi > 70:
            sell_signals += 2
            sell_reasons.append(f"🔥 RSI е {rsi:.1f} - СВРЪХКУПЕНО!")
        elif rsi > 60:
            sell_signals += 1
            sell_reasons.append(f"📈 RSI е {rsi:.1f} - близо до свръхкупеност")
    
    # 3. Цената е над SMA50 (добра за продажба)
    if sma50 is not None and price > sma50:
        sell_signals += 1
        sell_reasons.append(f"📊 Цената е над SMA50 (${sma50:.4f})")
    
    # ===== ФИНАЛЕН СИГНАЛ =====
    if buy_signals >= 4:
        verdict = "STRONG BUY"
        action = "КУПИ СЕГА! 🟢🔥"
        priority = "HIGH"
    elif buy_signals >= 2:
        verdict = "BUY"
        action = "Купи 🟢"
        priority = "MEDIUM"
    elif sell_signals >= 4:
        verdict = "STRONG SELL"
        action = "ПРОДАЙ СЕГА! 🔴🔥"
        priority = "HIGH"
    elif sell_signals >= 2:
        verdict = "SELL"
        action = "Продай 🔴"
        priority = "MEDIUM"
    else:
        verdict = "HOLD"
        action = "Изчакай 🟡"
        priority = "LOW"
    
    # ===== ИЗЧИСЛЯВАНЕ НА ПОТЕНЦИАЛНА ПЕЧАЛБА =====
    profit_calculations = []
    for target in SELL_TARGETS:
        if target > price:
            profit_pct = ((target - price) / price) * 100
            profit_usd = (target - price) * TARGET_ICP
            profit_calculations.append({
                "target": target,
                "profit_pct": profit_pct,
                "profit_usd": profit_usd
            })
    
    return {
        "verdict": verdict,
        "action": action,
        "priority": priority,
        "buy_signals": buy_signals,
        "buy_reasons": buy_reasons,
        "sell_signals": sell_signals,
        "sell_reasons": sell_reasons,
        "profit_calculations": profit_calculations,
        "needed_icp": TARGET_ICP - CURRENT_ICP,
        "current_icp": CURRENT_ICP,
        "target_icp": TARGET_ICP
    }


def format_personalized_message(current: Dict, sig: Dict, fear_greed: Optional[Dict], btc_dom: Optional[float], personalized: Dict) -> str:
    """Форматира персонализирано съобщение"""
    chg = current.get("usd_24h_change", 0) or 0
    chg_emoji = "📈" if chg >= 0 else "📉"
    price = sig["price"]
    
    # Signal emoji
    signal_emoji = get_signal_emoji(personalized["verdict"])
    
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
    
    # Построяване на съобщението
    lines = [
        "<b>🚀 ICP PERSONALIZED SIGNAL</b>",
        "",
        f"💰 <b>Price</b>: {format_price(price)}",
        f"{chg_emoji} <b>24h</b>: {chg:+.2f}%",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"🎯 <b>СИГНАЛ</b>: {signal_emoji} {personalized['action']}",
        f"📊 <b>Приоритет</b>: {personalized['priority']}",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"📊 <b>Market Cap</b>: {format_large_number(sig['market_cap'])}",
        f"💵 <b>Volume</b>: {format_large_number(sig['volume_24h'])}",
        "",
        f"📉 <b>RSI(14)</b>: {sig['rsi']:.1f}{rsi_status}" if sig["rsi"] else "📉 RSI: N/A",
        f"📈 <b>MACD</b>: {macd_status}",
        f"📊 <b>SMA20</b>: {format_price(sig['sma20'])}",
        f"📊 <b>SMA50</b>: {format_price(sig['sma50'])}",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "📊 <b>ТВОЯТА ПОЗИЦИЯ</b>",
        f"🪙 <b>ICP</b>: {personalized['current_icp']} / {personalized['target_icp']}",
        f"📦 <b>Нужни</b>: {personalized['needed_icp']} ICP",
        f"📈 <b>Прогрес</b>: {((personalized['current_icp']/personalized['target_icp'])*100):.1f}%",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    
    # Причини за покупка
    if personalized['buy_reasons']:
        lines.append("🟢 <b>ПРИЧИНИ ЗА ПОКУПКА</b>")
        for reason in personalized['buy_reasons']:
            lines.append(f"  {reason}")
        lines.append("")
    
    # Причини за продажба
    if personalized['sell_reasons']:
        lines.append("🔴 <b>ПРИЧИНИ ЗА ПРОДАЖБА</b>")
        for reason in personalized['sell_reasons']:
            lines.append(f"  {reason}")
        lines.append("")
    
    # Потенциална печалба
    if personalized['profit_calculations']:
        lines.append("💰 <b>ПОТЕНЦИАЛНА ПЕЧАЛБА</b>")
        for calc in personalized['profit_calculations'][:3]:
            lines.append(f"  🎯 ${calc['target']:.2f}: +{calc['profit_pct']:.1f}% (${calc['profit_usd']:,.0f})")
        lines.append("")
    
    lines.extend([
        f"😨 <b>Fear & Greed</b>: {fg_str}",
        f"₿ <b>BTC Dominance</b>: {btc_str}",
        "",
        f"🏆 <b>ATH</b>: {format_price(sig['ath'])}",
        f"📉 <b>Distance from ATH</b>: {sig['ath_change_pct']:+.1f}%" if sig["ath_change_pct"] else "N/A",
        "",
        "<i>📌 Базирано на твоята стратегия: купувай между $2.10-$2.50</i>",
        "<i>⚠️ Не е финансов съвет - сам вземай решенията!</i>"
    ])
    
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
            f"{'🔥 СВРЪХПРОДАДЕНО - МОМЕНТ ЗА ПОКУПКА!' if condition == 'oversold' else '⚠️ СВРЪХКУПЕНО - МОМЕНТ ЗА ПРОДАЖБА!'}"
        )
        send_telegram(msg)
        state["rsi_alert"] = rsi_alert
        save_state(state)


def format_daily_report(sig: Dict, current: Dict, fear_greed: Optional[Dict], btc_dom: Optional[float], personalized: Dict) -> str:
    """Форматира дневен отчет с персонализирана информация"""
    chg = current.get("usd_24h_change", 0) or 0
    chg_emoji = "📈" if chg >= 0 else "📉"
    today = datetime.now().strftime("%d %B %Y")
    price = sig["price"]
    
    lines = [
        f"<b>📅 ДНЕВЕН ОТЧЕТ - {today}</b>",
        "",
        f"💰 <b>Цена</b>: {format_price(price)} {chg_emoji} {chg:+.2f}%",
        f"📊 <b>Market Cap</b>: {format_large_number(sig['market_cap'])}",
        f"💵 <b>Volume</b>: {format_large_number(sig['volume_24h'])}",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📉 <b>RSI(14)</b>: {sig['rsi']:.1f}" if sig["rsi"] else "📉 RSI: N/A",
        f"🎯 <b>Signal</b>: {get_signal_emoji(personalized['verdict'])} {personalized['action']}",
        f"🤖 <b>AI Score</b>: {sig['ai_score']:.1f}/10",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"🪙 <b>Твоите ICP</b>: {personalized['current_icp']} / {personalized['target_icp']}",
        f"📦 <b>Нужни за целта</b>: {personalized['needed_icp']} ICP",
        f"📈 <b>Прогрес</b>: {((personalized['current_icp']/personalized['target_icp'])*100):.1f}%",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"😨 <b>Fear & Greed</b>: {fear_greed['value']} ({fear_greed['classification']})" if fear_greed else "N/A",
        f"₿ <b>BTC Dominance</b>: {btc_dom:.1f}%" if btc_dom else "N/A",
        "",
        f"🏆 <b>ATH</b>: {format_price(sig['ath'])}",
        f"📉 <b>Distance from ATH</b>: {sig['ath_change_pct']:+.1f}%" if sig["ath_change_pct"] else "N/A",
        "",
        "<i>📌 Стратегия: Купувай между $2.10-$2.50</i>",
        "<i>🎯 Цел: 1000 ICP</i>",
        "<i>⚠️ Не е финансов съвет</i>"
    ]
    return "\n".join(lines)


# ============ НОВИНИ (само за ICP, последните 24 часа) ============
NEWS_FEEDS = [
    {"url": "https://cointelegraph.com/rss/tag/internet-computer", "name": "Cointelegraph", "keyword_filter": False},
    {"url": "https://www.coindesk.com/arc/outboundfeeds/rss/", "name": "CoinDesk", "keyword_filter": True},
    {"url": "https://decrypt.co/feed", "name": "Decrypt", "keyword_filter": True},
    {"url": "https://cryptoslate.com/feed/", "name": "CryptoSlate", "keyword_filter": True},
]

ICP_KEYWORD_RE = re.compile(r"\b(icp|internet computer|internet-computer)\b", re.IGNORECASE)


def fetch_news() -> List[Dict]:
    """Взима новини само за ICP от последните 24 часа"""
    items = []
    
    # Само последните 24 часа
    cutoff_time = time.time() - (24 * 60 * 60)
    
    for feed in NEWS_FEEDS:
        try:
            parsed = feedparser.parse(feed["url"])
            for entry in parsed.entries[:30]:
                title = entry.get("title", "")
                summary = entry.get("summary", "")
                
                # Филтър само за ICP
                if not ICP_KEYWORD_RE.search(title + " " + summary):
                    continue
                
                link = entry.get("link", "")
                item_id = entry.get("id") or link
                
                if entry.get("published_parsed"):
                    published_ts = calendar.timegm(entry["published_parsed"])
                elif entry.get("updated_parsed"):
                    published_ts = calendar.timegm(entry["updated_parsed"])
                else:
                    published_ts = int(time.time())
                
                # Пропускаме новини по-стари от 24 часа
                if published_ts < cutoff_time:
                    continue
                
                items.append({
                    "id": item_id,
                    "title": title,
                    "url": link,
                    "source": feed["name"],
                    "published_on": published_ts,
                })
        except Exception as e:
            logger.error(f"Грешка при четене на RSS ({feed['name']}): {e}")
    
    items.sort(key=lambda x: x["published_on"], reverse=True)
    return items[:3]  # Максимум 3 новини


def format_news_message(item: Dict) -> str:
    """Форматира новинарско съобщение"""
    published = datetime.fromtimestamp(item["published_on"], tz=timezone.utc)
    when = published.strftime("%d %b %Y, %H:%M UTC")
    return (
        f"📰 <b>ICP НОВИНА</b>\n"
        f"{item['title']}\n"
        f"{item['source']} · {when}\n"
        f"{item['url']}"
    )


def run_news_check(state: Dict):
    """Проверява за новини (макс 3 на ден)"""
    try:
        logger.info("Проверка за новини...")
        
        today = datetime.now().strftime("%Y-%m-%d")
        
        # Проверяваме дали днес сме пращали новини
        if state.get("last_news_date") != today:
            state["news_sent_today"] = 0
            state["last_news_date"] = today
        
        # Ако вече сме пратили 3 новини днес, спираме
        if state.get("news_sent_today", 0) >= 3:
            logger.info("Днес вече са изпратени 3 новини.")
            return
        
        # Взимаме новини
        items = fetch_news()
        sent_ids = set(state.get("sent_news_ids", []))
        
        # Филтрираме само новите
        new_items = [i for i in items if str(i["id"]) not in sent_ids]
        
        # Колко новини можем да изпратим днес
        remaining = 3 - state.get("news_sent_today", 0)
        new_items = new_items[:remaining]
        
        for item in new_items:
            send_telegram(format_news_message(item))
            sent_ids.add(str(item["id"]))
            state["news_sent_today"] = state.get("news_sent_today", 0) + 1
            time.sleep(1)
        
        if new_items:
            state["sent_news_ids"] = list(sent_ids)[-200:]
            save_state(state)
            logger.info(f"Изпратени {len(new_items)} нови новини.")
        else:
            logger.info("Няма нови новини.")
            
    except Exception as e:
        logger.error(f"Грешка при проверка на новини: {e}")


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
        
        # Персонализиран сигнал
        personalized = compute_personalized_signal(
            sig["price"],
            sig["rsi"],
            sig["sma20"],
            sig["sma50"]
        )
        
        # Проверки за аларми
        old_price = state.get("last_price")
        if old_price is not None:
            check_price_alerts(state, old_price, sig["price"])
        
        check_sma_cross(state, sig)
        check_rsi_alert(state, sig)
        
        # Проверяваме дали сигналът се е променил
        current_verdict = personalized["verdict"]
        last_verdict = state.get("last_verdict")
        
        # Изпращаме само при промяна или при силен сигнал
        if current_verdict != last_verdict or current_verdict in ["STRONG BUY", "STRONG SELL"]:
            msg = format_personalized_message(current, sig, fear_greed, btc_dom, personalized)
            send_telegram(msg)
            state["last_verdict"] = current_verdict
            logger.info(f"Изпратен сигнал: {current_verdict}")
        else:
            logger.info(f"Сигналът е същият ({current_verdict}), не се праща ново съобщение.")
        
        # Запазваме текущата цена
        state["last_price"] = sig["price"]
        
        # Дневен отчет (веднъж на ден)
        today = datetime.now().strftime("%Y-%m-%d")
        if state.get("last_report_date") != today:
            daily_msg = format_daily_report(sig, current, fear_greed, btc_dom, personalized)
            send_telegram(daily_msg)
            state["last_report_date"] = today
            logger.info("Изпратен дневен отчет")
        
        save_state(state)
        
    except Exception as e:
        logger.error(f"Грешка при проверка на цена/сигнал: {e}")
        import traceback
        logger.error(traceback.format_exc())


def main():
    """Главна функция - СИГНАЛИ + 3 НОВИНИ НА ДЕН"""
    global TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    
    # Вземи токените от environment variables (GitHub Secrets)
    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
    TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)
    
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("❌ Липсват TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID!")
        logger.error("Добави ги като Secrets в GitHub Actions")
        return
    
    # Зареди състоянието
    state = load_state()
    logger.info("🤖 ICP Ботът е стартиран")
    logger.info(f"📊 Твоята цел: {TARGET_ICP} ICP")
    logger.info(f"🪙 Текущо притежание: {CURRENT_ICP} ICP")
    logger.info(f"📈 Трябват ти още: {TARGET_ICP - CURRENT_ICP} ICP")
    
    # Тестова проверка дали Telegram работи
    test_msg = (
        f"🤖 <b>ICP Bot стартира успешно!</b>\n\n"
        f"📊 <b>Твоята стратегия:</b>\n"
        f"🪙 Цел: {TARGET_ICP} ICP\n"
        f"💰 Текущо: {CURRENT_ICP} ICP\n"
        f"📈 Остават: {TARGET_ICP - CURRENT_ICP} ICP\n"
        f"🎯 Зона за покупка: ${BUY_ZONE_LOW:.2f} - ${BUY_ZONE_HIGH:.2f}\n"
        f"📈 Цели за продажба: ${', $'.join([str(x) for x in SELL_TARGETS])}\n\n"
        f"📰 Ще получаваш до 3 новини на ден за ICP\n"
        f"📊 Ще получаваш сигнали за покупка/продажба"
    )
    if send_telegram(test_msg):
        logger.info("✅ Telegram връзката работи")
    else:
        logger.error("❌ Telegram връзката НЕ работи!")
        return
    
    # Изпълни проверка на цена
    run_price_check(state)
    
    # Изпълни проверка за новини
    run_news_check(state)
    
    logger.info("✅ Ботът завърши успешно!")


if __name__ == "__main__":
    main()
