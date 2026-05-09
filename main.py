#!/usr/bin/env python3
"""
Self-Learning Trading Agent — Entry Point

Usage:
  python main.py                    # Run in mode specified in config.yaml
  python main.py --mode paper       # Override mode to paper trading
  python main.py --mode live        # Override mode to live trading
  python main.py --mode backtest    # Run backtest
  python main.py --once             # Run a single analysis cycle and exit
  python main.py --kite-login       # Generate Kite access token (run once per day)
"""

import argparse
import os
import sys
from datetime import date
from pathlib import Path

import yaml
from dotenv import load_dotenv
from loguru import logger

load_dotenv()


def _load_keyvault_secrets():
    """Fetch secrets from Azure Key Vault using Managed Identity and inject into env."""
    vault_url = "https://tradingkvlje77ohmfrs5c.vault.azure.net"
    try:
        from azure.identity import ManagedIdentityCredential
        from azure.keyvault.secrets import SecretClient
        credential = ManagedIdentityCredential()
        client = SecretClient(vault_url=vault_url, credential=credential)
        secret = client.get_secret("ANTHROPIC-SELF-LEARNING-KEY")
        os.environ["ANTHROPIC_API_KEY"] = secret.value
        logger.info("Anthropic API key loaded from Key Vault")
    except Exception as e:
        logger.warning(f"Key Vault fetch failed, falling back to env: {e}")


_load_keyvault_secrets()


def load_config(config_path: str = "config/config.yaml") -> dict:
    with open(config_path) as f:
        content = f.read()
    # Simple env var substitution: ${VAR_NAME}
    import re
    def replace_env(match):
        var = match.group(1)
        val = os.environ.get(var, "")
        if not val:
            logger.warning(f"Environment variable {var} not set")
        return val
    content = re.sub(r"\$\{([^}]+)\}", replace_env, content)
    return yaml.safe_load(content)


def setup_logging(config: dict):
    import pytz
    from datetime import datetime

    log_config = config.get("logging", {})
    level = log_config.get("level", "INFO")
    log_file = log_config.get("file", "logs/trading_agent.log")
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    ist = pytz.timezone("Asia/Kolkata")

    def ist_patcher(record):
        record["extra"]["ist_time"] = datetime.now(ist).strftime("%Y-%m-%d %H:%M:%S")

    fmt = "{extra[ist_time]} IST | {level:<8} | {message}"

    logger.remove()
    logger.configure(patcher=ist_patcher)
    logger.add(sys.stdout, level=level,
               format="<green>{extra[ist_time]} IST</green> | <level>{level:<8}</level> | {message}")
    logger.add(log_file, level=level, format=fmt,
               rotation="1 day", retention="30 days", compression="gz")


def build_agent(config: dict, mode: str):
    from src.core.brain import TradingBrain
    from src.core.memory import TradeMemory
    from src.risk.risk_manager import RiskManager
    from src.core.agent import TradingAgent

    agent_cfg = config.get("agent", {})
    brain = TradingBrain(
        model=agent_cfg.get("model", "claude-opus-4-7"),
        max_tokens=agent_cfg.get("max_tokens", 8192),
    )
    memory = TradeMemory(agent_cfg.get("memory_file", "data/memory/trade_memory.json"))
    risk = RiskManager(config)

    if mode in ("paper", "live"):
        from src.data.market_data import MarketData
        from src.data.binance_data import BinanceData
        from src.analysis.market_analyzer import MarketAnalyzer

        kite_cfg = config.get("kite", {})
        market_data = MarketData(
            api_key=kite_cfg.get("api_key", ""),
            access_token=kite_cfg.get("access_token", ""),
        )

        binance_data = None
        binance_cfg = config.get("binance", {})
        if binance_cfg.get("api_key"):
            try:
                binance_data = BinanceData(
                    api_key=binance_cfg["api_key"],
                    api_secret=binance_cfg["api_secret"],
                    testnet=binance_cfg.get("testnet", True),
                )
            except Exception as e:
                logger.warning(f"Binance init failed (crypto disabled): {e}")

        analyzer = MarketAnalyzer(market_data, config, binance_data=binance_data)

        if mode == "paper":
            from src.execution.paper_trader import PaperTrader
            from src.execution.binance_trader import BinancePaperTrader
            risk_cfg = config.get("risk", {})
            nse_capital = risk_cfg.get("nse_capital", 50000)
            mcx_capital = risk_cfg.get("mcx_capital", 50000)
            crypto_usdt = risk_cfg.get("crypto_capital_usdt", 500)

            # NSE options executor (₹50K)
            executor = PaperTrader(initial_capital=nse_capital)
            executor.set_price_feed(
                lambda sym: market_data.get_quote([sym], "NSE")
                .get(f"NSE:{sym}", {})
                .get("last_price", 0.0)
            )

            # MCX commodities executor (₹50K) with MCX price feed
            mcx_trader = PaperTrader(initial_capital=mcx_capital)
            mcx_trader.set_price_feed(
                lambda sym: market_data.get_mcx_quote([sym]).get(sym, {}).get("last_price", 0.0)
            )
            executor.mcx_paper = mcx_trader

            # Binance paper trader for crypto ($500 USDT)
            executor.binance_paper = BinancePaperTrader(initial_usdt=crypto_usdt)
            logger.info(
                f"Paper trading mode | NSE: ₹{nse_capital:,} | MCX: ₹{mcx_capital:,} | Crypto: ${crypto_usdt} USDT"
            )

        else:  # live
            from src.execution.live_trader import LiveTrader
            from src.execution.binance_trader import BinanceLiveTrader
            kite_cfg = config["kite"]
            executor = LiveTrader(
                api_key=kite_cfg["api_key"],
                access_token=kite_cfg["access_token"],
            )
            if binance_data:
                executor.binance_live = BinanceLiveTrader(binance_data)
            logger.warning("LIVE TRADING MODE — real money will be used!")

    elif mode == "backtest":
        from src.data.market_data import MarketData
        from src.analysis.market_analyzer import MarketAnalyzer
        from src.execution.backtester import Backtester

        kite_cfg = config.get("kite", {})
        market_data = MarketData(
            api_key=kite_cfg.get("api_key", ""),
            access_token=kite_cfg.get("access_token", ""),
        )
        analyzer = MarketAnalyzer(market_data, config)

        bt_cfg = config.get("backtest", {})
        start = date.fromisoformat(bt_cfg.get("start_date", "2024-01-01"))
        end = date.fromisoformat(bt_cfg.get("end_date", "2024-12-31"))
        initial_capital = config["risk"].get("initial_capital", 100000)

        executor = Backtester(
            market_data=market_data,
            initial_capital=initial_capital,
            start_date=start,
            end_date=end,
            interval=bt_cfg.get("interval", "5minute"),
        )
        logger.info(f"Backtest mode | {start} → {end} | Capital: ₹{initial_capital:,}")
    else:
        raise ValueError(f"Unknown mode: {mode}")

    return TradingAgent(
        config=config,
        brain=brain,
        memory=memory,
        risk_manager=risk,
        market_analyzer=analyzer,
        executor=executor,
    )


def run_backtest(config: dict):
    """Run a full historical backtest over the configured date range."""
    from datetime import timedelta
    from src.data.market_data import MarketData
    from src.data.indicators import add_all_indicators
    from src.execution.backtester import Backtester
    from src.core.brain import TradingBrain
    from src.core.memory import TradeMemory
    from src.risk.risk_manager import RiskManager
    from src.analysis.market_analyzer import MarketAnalyzer
    from src.core.agent import TradingAgent

    logger.info("Starting backtest run...")
    agent = build_agent(config, "backtest")
    executor: Backtester = agent.executor
    market_data = agent.analyzer.md

    bt_cfg = config["backtest"]
    start = date.fromisoformat(bt_cfg["start_date"])
    end = date.fromisoformat(bt_cfg["end_date"])
    watchlist = config["instruments"]["watchlist"]
    exchange = config["instruments"].get("exchange", "NSE")
    interval = bt_cfg.get("interval", "5minute")

    # Pre-fetch historical data for all symbols
    logger.info(f"Fetching historical data for {len(watchlist)} symbols...")
    all_data: dict[str, object] = {}
    for symbol in watchlist:
        df = market_data.get_historical_data(symbol, start, end, interval, exchange)
        if not df.empty:
            all_data[symbol] = add_all_indicators(df)
            logger.info(f"  {symbol}: {len(df)} bars")

    if not all_data:
        logger.error("No historical data fetched. Check Kite credentials and date range.")
        return

    # Replay bars
    all_timestamps = sorted(set(
        ts for df in all_data.values() for ts in df.index
    ))

    logger.info(f"Replaying {len(all_timestamps)} bars across all symbols...")
    for ts in all_timestamps:
        bar_prices = {}
        for symbol, df in all_data.items():
            if ts in df.index:
                bar_prices[symbol] = float(df.loc[ts, "close"])
        executor.set_bar(bar_prices)
        executor.check_sl_and_targets()

        # Run agent decision at every 15th 5-min bar (~75 min interval)
        if ts.minute % 15 == 0:
            try:
                agent.run_once()
            except Exception as e:
                logger.debug(f"Backtest cycle error at {ts}: {e}")

    report = executor.get_backtest_report()
    logger.info("\n" + "=" * 50)
    logger.info("BACKTEST RESULTS")
    logger.info("=" * 50)
    for k, v in report.items():
        logger.info(f"  {k}: {v}")
    logger.info("=" * 50)
    return report


def kite_login(config: dict):
    """Interactive Kite login to get access token."""
    from kiteconnect import KiteConnect
    kite_cfg = config.get("kite", {})
    api_key = kite_cfg.get("api_key")
    api_secret = kite_cfg.get("api_secret")

    if not api_key or not api_secret:
        logger.error("KITE_API_KEY and KITE_API_SECRET must be set in .env")
        return

    kite = KiteConnect(api_key=api_key)
    login_url = kite.login_url()
    print(f"\nOpen this URL in your browser:\n{login_url}\n")
    request_token = input("Paste the request_token from the redirect URL: ").strip()

    data = kite.generate_session(request_token, api_secret=api_secret)
    access_token = data["access_token"]
    print(f"\nAccess token: {access_token}")
    print("\nAdd to your .env file:")
    print(f"KITE_ACCESS_TOKEN={access_token}")


def main():
    parser = argparse.ArgumentParser(description="Self-Learning Trading Agent")
    parser.add_argument("--config", default="config/config.yaml", help="Path to config file")
    parser.add_argument("--mode", choices=["paper", "live", "backtest"], help="Override trading mode")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--kite-login", action="store_true", help="Generate Kite access token")
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(config)

    if args.kite_login:
        kite_login(config)
        return

    mode = args.mode or config.get("trading_mode", "paper")
    logger.info(f"Trading mode: {mode.upper()}")

    if mode == "backtest":
        run_backtest(config)
        return

    agent = build_agent(config, mode)
    agent_cfg = config.get("agent", {})

    if args.once:
        logger.info("Running single analysis cycle...")
        agent.run_once()
        logger.info("Done.")
        return

    interval = agent_cfg.get("analysis_interval_minutes", 15)
    agent.run_scheduler(interval_minutes=interval)


if __name__ == "__main__":
    main()
