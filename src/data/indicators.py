import pandas as pd
import numpy as np
import ta
from loguru import logger


def add_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add all technical indicators to OHLCV dataframe."""
    if df.empty or len(df) < 20:
        return df

    df = df.copy()

    # Trend: EMAs and SMAs
    df["ema_9"] = ta.trend.ema_indicator(df["close"], window=9)
    df["ema_21"] = ta.trend.ema_indicator(df["close"], window=21)
    df["ema_50"] = ta.trend.ema_indicator(df["close"], window=50)
    df["sma_20"] = ta.trend.sma_indicator(df["close"], window=20)
    df["sma_200"] = ta.trend.sma_indicator(df["close"], window=200)

    # Momentum: RSI
    df["rsi"] = ta.momentum.rsi(df["close"], window=14)

    # Momentum: MACD
    macd = ta.trend.MACD(df["close"], window_slow=26, window_fast=12, window_sign=9)
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"] = macd.macd_diff()

    # Volatility: Bollinger Bands
    bb = ta.volatility.BollingerBands(df["close"], window=20, window_dev=2)
    df["bb_upper"] = bb.bollinger_hband()
    df["bb_middle"] = bb.bollinger_mavg()
    df["bb_lower"] = bb.bollinger_lband()
    df["bb_pct"] = bb.bollinger_pband()  # % position within bands

    # Volatility: ATR
    df["atr"] = ta.volatility.average_true_range(df["high"], df["low"], df["close"], window=14)

    # Momentum: Stochastic
    stoch = ta.momentum.StochasticOscillator(
        df["high"], df["low"], df["close"], window=14, smooth_window=3
    )
    df["stoch_k"] = stoch.stoch()
    df["stoch_d"] = stoch.stoch_signal()

    # Volume: ratio vs 20-period average
    df["volume_sma20"] = ta.trend.sma_indicator(df["volume"].astype(float), window=20)
    df["volume_ratio"] = df["volume"] / df["volume_sma20"].replace(0, np.nan)

    # Pivot Points (daily, using last complete candle's OHLC)
    df = _add_pivot_points(df)

    return df


def _add_pivot_points(df: pd.DataFrame) -> pd.DataFrame:
    """Classic pivot points computed from the previous candle's H/L/C."""
    high = df["high"].shift(1)
    low = df["low"].shift(1)
    close = df["close"].shift(1)

    pivot = (high + low + close) / 3
    df["pivot"] = pivot
    df["r1"] = 2 * pivot - low
    df["s1"] = 2 * pivot - high
    df["r2"] = pivot + (high - low)
    df["s2"] = pivot - (high - low)
    df["r3"] = high + 2 * (pivot - low)
    df["s3"] = low - 2 * (high - pivot)

    return df


def get_signal_summary(df: pd.DataFrame) -> dict:
    """Summarise last-bar indicator state as a dict for LLM context."""
    if df.empty:
        return {}

    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else last

    # Trend determination
    close = last["close"]
    ema9 = last.get("ema_9", np.nan)
    ema21 = last.get("ema_21", np.nan)
    ema50 = last.get("ema_50", np.nan)
    sma200 = last.get("sma_200", np.nan)

    trend_score = 0
    if close > ema9:
        trend_score += 1
    if close > ema21:
        trend_score += 1
    if close > ema50:
        trend_score += 1
    if close > sma200:
        trend_score += 2

    if trend_score >= 4:
        trend = "strong_uptrend"
    elif trend_score >= 2:
        trend = "uptrend"
    elif trend_score == 0:
        trend = "strong_downtrend"
    elif trend_score == 1:
        trend = "downtrend"
    else:
        trend = "sideways"

    # RSI zone
    rsi = last.get("rsi", 50)
    if rsi >= 70:
        rsi_zone = "overbought"
    elif rsi >= 60:
        rsi_zone = "bullish"
    elif rsi <= 30:
        rsi_zone = "oversold"
    elif rsi <= 40:
        rsi_zone = "bearish"
    else:
        rsi_zone = "neutral"

    # MACD crossover
    macd_bullish = bool(
        last.get("macd", 0) > last.get("macd_signal", 0)
        and prev.get("macd", 0) <= prev.get("macd_signal", 0)
    )
    macd_bearish = bool(
        last.get("macd", 0) < last.get("macd_signal", 0)
        and prev.get("macd", 0) >= prev.get("macd_signal", 0)
    )

    # Bollinger Band position
    bb_pct = last.get("bb_pct", 0.5)
    if bb_pct > 0.9:
        bb_position = "near_upper_band"
    elif bb_pct < 0.1:
        bb_position = "near_lower_band"
    else:
        bb_position = "middle"

    # Volume surge
    volume_ratio = last.get("volume_ratio", 1.0)
    volume_surge = bool(volume_ratio > 2.0)

    # Stochastic zone
    stoch_k = last.get("stoch_k", 50)
    if stoch_k >= 80:
        stoch_zone = "overbought"
    elif stoch_k <= 20:
        stoch_zone = "oversold"
    else:
        stoch_zone = "neutral"

    # Nearest pivot level
    pivot_levels = {
        "R3": last.get("r3"),
        "R2": last.get("r2"),
        "R1": last.get("r1"),
        "Pivot": last.get("pivot"),
        "S1": last.get("s1"),
        "S2": last.get("s2"),
        "S3": last.get("s3"),
    }
    near_level = _nearest_pivot(close, pivot_levels)

    return {
        "close": round(close, 2),
        "trend": trend,
        "ema_9": round(ema9, 2) if not np.isnan(ema9) else None,
        "ema_21": round(ema21, 2) if not np.isnan(ema21) else None,
        "ema_50": round(ema50, 2) if not np.isnan(ema50) else None,
        "sma_200": round(sma200, 2) if not np.isnan(sma200) else None,
        "rsi": round(rsi, 2),
        "rsi_zone": rsi_zone,
        "macd": round(last.get("macd", 0), 4),
        "macd_signal": round(last.get("macd_signal", 0), 4),
        "macd_bullish_crossover": macd_bullish,
        "macd_bearish_crossover": macd_bearish,
        "bb_upper": round(last.get("bb_upper", 0), 2),
        "bb_lower": round(last.get("bb_lower", 0), 2),
        "bb_position": bb_position,
        "atr": round(last.get("atr", 0), 2),
        "volume_ratio": round(volume_ratio, 2),
        "volume_surge": volume_surge,
        "stoch_k": round(stoch_k, 2),
        "stoch_d": round(last.get("stoch_d", 50), 2),
        "stoch_zone": stoch_zone,
        "pivot": round(last.get("pivot", 0), 2),
        "r1": round(last.get("r1", 0), 2),
        "r2": round(last.get("r2", 0), 2),
        "s1": round(last.get("s1", 0), 2),
        "s2": round(last.get("s2", 0), 2),
        "near_pivot_level": near_level,
    }


def _nearest_pivot(close: float, levels: dict) -> str:
    nearest = min(
        ((name, abs(close - val)) for name, val in levels.items() if val and not np.isnan(val)),
        key=lambda x: x[1],
        default=("unknown", 0),
    )
    return nearest[0]
