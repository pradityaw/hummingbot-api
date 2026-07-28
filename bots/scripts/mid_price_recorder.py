"""
Mid-price recorder — bot-side market truth for markout analysis.

Hyperliquid testnet only retains ~3-5 days of 1m candles via candleSnapshot, which
made Gate A markout unmeasurable (402/403 fills had no candle coverage). Recording
the connector mid price every tick gives the analyzer a price series that always
covers the fill window: markout = fill price vs mid at fill+1m/5m/15m from our own
recording, spread capture = fill price vs mid at fill time.

Files land in `<HB_CONNECTIVITY_STATE_DIR>/mids/mids_YYYYMMDD.jsonl` (UTC), one JSON
object per line: {"ts", "connector", "pair", "mid"}. The connectivity guard already
persists to the same state dir, which ops exports. Recording must never break the
strategy tick: all I/O errors are swallowed by the caller.
"""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

DEFAULT_STATE_DIR = "/home/hummingbot/data/connectivity"
INTERVAL_ENV = "HB_MID_SNAPSHOT_INTERVAL_SECONDS"
DEFAULT_INTERVAL_SECONDS = 1.0


class MidPriceRecorder:
    def __init__(
        self,
        state_dir: Optional[str] = None,
        interval_seconds: Optional[float] = None,
        time_fn: Callable[[], float] = time.time,
    ):
        base_dir = Path(state_dir or os.environ.get("HB_CONNECTIVITY_STATE_DIR", DEFAULT_STATE_DIR))
        self.mids_dir = base_dir / "mids"
        if interval_seconds is None:
            interval_seconds = float(os.environ.get(INTERVAL_ENV, str(DEFAULT_INTERVAL_SECONDS)) or "0")
        self.interval_seconds = float(interval_seconds)
        self.time_fn = time_fn
        self._last_write_at = 0.0

    @property
    def enabled(self) -> bool:
        # HB_MID_SNAPSHOT_INTERVAL_SECONDS=0 is the ops escape hatch.
        return self.interval_seconds > 0

    def maybe_record(self, now: Optional[float], samples: Iterable[Mapping[str, str]]) -> bool:
        """
        Append one line per (connector, pair) sample if the interval elapsed.
        `samples` items must carry connector/pair/mid string values.
        Returns True when a batch was written.
        """
        if not self.enabled:
            return False
        timestamp = float(now if now is not None else self.time_fn())
        if timestamp - self._last_write_at < self.interval_seconds:
            return False
        samples = list(samples)
        if not samples:
            return False
        self._last_write_at = timestamp
        day = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y%m%d")
        self.mids_dir.mkdir(parents=True, exist_ok=True)
        path = self.mids_dir / f"mids_{day}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            for sample in samples:
                line = {
                    "ts": timestamp,
                    "connector": sample.get("connector"),
                    "pair": sample.get("pair"),
                    "mid": sample.get("mid"),
                }
                handle.write(json.dumps(line, sort_keys=True) + "\n")
        return True
