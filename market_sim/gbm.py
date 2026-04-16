"""
Geometric Brownian Motion price engine.
Generates realistic synthetic OHLCV data for paper trading.
"""
import numpy as np
from datetime import datetime, timedelta
from typing import List


class GBMEngine:
    """
    Simulates a single asset price path using Geometric Brownian Motion.

    dS = mu * S * dt + sigma * S * dW
    where dW ~ N(0, dt)

    Parameters
    ----------
    symbol : str
    S0     : float  — starting price
    mu     : float  — annualised drift  (e.g. 0.08 = 8% p.a.)
    sigma  : float  — annualised vol    (e.g. 0.20 = 20% p.a.)
    seed   : int | None — optional random seed for reproducibility
    """

    DT_1MIN = 1 / (252 * 390)   # 1-minute bar (252 trading days, 390 mins/day)
    TICKS_PER_BAR = 10           # simulate intrabar micro-movement

    def __init__(
        self,
        symbol: str,
        S0: float = 100.0,
        mu: float = 0.08,
        sigma: float = 0.20,
        seed: int | None = None,
    ):
        self.symbol = symbol
        self.mu = mu
        self.sigma = sigma
        self.rng = np.random.default_rng(seed)

        # Price and timestamp history (close prices)
        self._closes: List[float] = [S0]
        self._times: List[datetime] = [datetime(2025, 1, 2, 9, 30, 0)]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def current_price(self) -> float:
        return self._closes[-1]

    @property
    def current_time(self) -> datetime:
        return self._times[-1]

    def advance(self, n: int = 1) -> None:
        """Generate n new 1-minute bars and append to history."""
        for _ in range(n):
            self._gen_bar()

    def get_bars(self, limit: int = 30) -> List[dict]:
        """
        Return the last `limit` OHLCV bars in Alpaca-compatible format.
        Automatically generates bars if history is shorter than limit.
        """
        needed = limit + 1   # +1 so we can compute open from prior close
        while len(self._closes) < needed:
            self._gen_bar()

        bars = []
        start = max(0, len(self._closes) - limit - 1)
        for i in range(start, len(self._closes) - 1):
            o = self._closes[i]
            c = self._closes[i + 1]
            # Simulate high/low from intrabar noise
            noise = abs(self.rng.normal(0, self.sigma * self.DT_1MIN ** 0.5 * 3))
            h = max(o, c) * (1 + noise)
            l = min(o, c) * (1 - noise)
            v = int(self.rng.lognormal(mean=8.0, sigma=0.6))
            bars.append({
                "t": self._times[i + 1].isoformat() + "Z",
                "o": round(o, 4),
                "h": round(h, 4),
                "l": round(l, 4),
                "c": round(c, 4),
                "v": v,
            })

        return bars[-limit:]

    def pct_change(self, lookback: int = 60) -> float:
        """Return price change over the last `lookback` bars."""
        if len(self._closes) < 2:
            return 0.0
        idx = max(0, len(self._closes) - 1 - lookback)
        prev = self._closes[idx]
        return (self._closes[-1] - prev) / prev if prev > 0 else 0.0

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _gen_bar(self) -> None:
        """Append one new 1-minute bar via GBM."""
        S = self._closes[-1]
        z = self.rng.standard_normal(self.TICKS_PER_BAR)
        dt = self.DT_1MIN / self.TICKS_PER_BAR
        log_returns = (self.mu - 0.5 * self.sigma ** 2) * dt + self.sigma * dt ** 0.5 * z
        path = S * np.exp(np.cumsum(log_returns))
        self._closes.append(float(path[-1]))
        self._times.append(self._times[-1] + timedelta(minutes=1))
