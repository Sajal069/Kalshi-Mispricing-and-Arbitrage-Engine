"""Live capture from Kalshi.

Kalshi serves **no historical order-book data** -- the historical tier covers
markets, candlesticks, trades, orders and positions only, and candlesticks are
one-minute at best. No public Level 2 dataset for Kalshi exists either. There is
therefore no way to backtest a microstructure strategy on Kalshi from archives:
you have to record your own tape.

That makes this subpackage the moat rather than the boilerplate. Its job is to
produce a tape that the replay engine can reconstruct **exactly**, which means
getting three unglamorous things right: sequence-gap detection with resync, dual
timestamping, and never losing bytes on restart.
"""

from .auth import KalshiAuth  # noqa: F401
from .rest import KalshiREST, RestConfig  # noqa: F401
from .ws import OrderbookRecorder, RecorderConfig  # noqa: F401

__all__ = [
    "KalshiAuth",
    "KalshiREST",
    "RestConfig",
    "OrderbookRecorder",
    "RecorderConfig",
]
