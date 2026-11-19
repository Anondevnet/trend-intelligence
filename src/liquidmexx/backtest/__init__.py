"""Stage 5: point-in-time backtesting of trend detectors."""

from liquidmexx.backtest.engine import Backtester, BacktestResult, Point, spearman

__all__ = ["BacktestResult", "Backtester", "Point", "spearman"]
