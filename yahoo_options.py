"""Paced, session-aware Yahoo option access for one scheduled run.

The isolated _data.get adapter uses yfinance's cookie/crumb handling, including
its built-in authentication retry. We never construct or log cookies/crumbs.
"""
from contextvars import ContextVar
from datetime import datetime, timezone
from types import SimpleNamespace
import gzip
import json
import os
from pathlib import Path
import time

import pandas as pd
import yfinance as yf

ACTIVE = ContextVar('option_reader', default=None)


class OptionDataError(ValueError):
    def __init__(self, message, category='unavailable'):
        super().__init__(message)
        self.category = category


def ticker_for(symbol):
    reader = ACTIVE.get()
    return reader.ticker(symbol) if reader else yf.Ticker(symbol)


class YahooOptions:
    def __init__(self, state, spacing=.75, failure_limit=3, cooldown_minutes=15, cache_path=None, max_age=1800):
        self.state = state
        self.spacing = spacing
        self.failure_limit = failure_limit
        self.cooldown_minutes = cooldown_minutes
        self.tickers, self.chains, self.failures = {}, {}, {}
        self.last_request = None
        self.requests = self.cache_hits = self.consecutive_failures = 0
        self.open_until = state.get('provider_health', {}).get('cooldown_until', 0)
        self.last_error = state.get('provider_health', {}).get('last_error', '')
        self.cache_path = Path(cache_path) if cache_path else None
        self.max_age = max_age
        self.raw_chains = {}
        if self.cache_path and self.cache_path.exists():
            try:
                saved = json.loads(gzip.decompress(self.cache_path.read_bytes()))
                if not isinstance(saved, dict):
                    raise ValueError('Invalid disposable cache')
                for key, row in saved.items():
                    if 0 <= time.time()-row['observed_at'] <= max_age:
                        self.raw_chains[key] = row
                        self._store_chain(row['ticker'], row['block'], row['observed_at'], row.get('put_status'))
            except (ValueError, KeyError, TypeError, OSError, EOFError, OverflowError, AttributeError):
                # A disposable quote cache cannot prevent portfolio recovery.
                self.raw_chains, self.chains = {}, {}

    def _store_chain(self, symbol, block, observed, put_status=None):
        expiry = datetime.fromtimestamp(int(block['expirationDate']), timezone.utc).date().isoformat()
        if not all(isinstance(block.get(side), list) for side in ('calls', 'puts')):
            raise OptionDataError('Option response missing a calls or puts list', 'no_data')
        if not block['calls'] and not block['puts']:
            raise OptionDataError('Both option sides empty; expiry remains unconfirmed', 'no_data')
        status = put_status or ('available' if block['puts'] else 'empty_unconfirmed')
        frames = []
        for side in ('calls', 'puts'):
            frame = pd.DataFrame(block[side])
            if 'lastTradeDate' in frame:
                frame['lastTradeDate'] = pd.to_datetime(frame['lastTradeDate'], unit='s', utc=True, errors='coerce')
            frames.append(frame)
        self.chains[(symbol, expiry)] = SimpleNamespace(calls=frames[0], puts=frames[1],
            observed_at=observed, put_status=status)
        self.raw_chains[f'{symbol}:{expiry}'] = dict(ticker=symbol, block=block,
            observed_at=observed, put_status=status)

    def save_cache(self):
        if self.cache_path:
            rows = {k:v for k,v in self.raw_chains.items()
                    if 0 <= time.time()-v['observed_at'] <= self.max_age}
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix('.tmp')
            tmp.write_bytes(gzip.compress(json.dumps(rows, allow_nan=False).encode(), mtime=0))
            os.replace(tmp, self.cache_path)

    def __enter__(self):
        self.token = ACTIVE.set(self)
        return self

    def __exit__(self, *args):
        self.state['provider_health'] = self.health()
        ACTIVE.reset(self.token)
        try:
            self.save_cache()
        except (OSError, ValueError, TypeError):
            print('Disposable option cache could not be saved; portfolio results are unaffected')

    def ticker(self, symbol):
        if symbol not in self.tickers:
            self.tickers[symbol] = yf.Ticker(symbol)
        return self.tickers[symbol]

    @property
    def paused(self):
        return time.time() < self.open_until

    def health(self):
        status = 'cooldown' if self.paused else 'degraded' if self.failures else 'available' if self.requests else 'not_checked'
        return dict(status=status, cooldown_until=self.open_until, client_version=yf.__version__,
                    last_error=self.last_error, requests=self.requests, cache_hits=self.cache_hits,
                    consecutive_failures=self.consecutive_failures)

    def fetch(self, symbol, expiry=None):
        key = (symbol, expiry)
        if key in self.failures:
            raise self.failures[key]
        if self.paused:
            raise OptionDataError('Yahoo access paused after repeated failures; queued for a later run', 'cooldown')
        if self.last_request is not None:
            time.sleep(max(0, self.spacing-(time.monotonic()-self.last_request)))
        self.last_request = time.monotonic()
        self.requests += 1
        try:
            params = {} if expiry is None else {'date':int(pd.Timestamp(expiry, tz='UTC').timestamp())}
            response = self.ticker(symbol)._data.get(
                url=f'https://query2.finance.yahoo.com/v7/finance/options/{symbol}',
                params=params, timeout=15)
            if response.status_code != 200:
                category = 'access' if response.status_code in (401,403) else 'rate_limit' if response.status_code == 429 else 'unavailable'
                raise OptionDataError(f'Yahoo HTTP {response.status_code} after session-aware request', category)
            payload = response.json().get('optionChain', {})
            roots = payload.get('result') or []
            if not roots:
                raise OptionDataError('Yahoo returned no option data', 'no_data')
            root = roots[0]
            expirations = [datetime.fromtimestamp(int(v), timezone.utc).date().isoformat()
                           for v in root.get('expirationDates', [])]
            observed = time.time()
            if expirations:
                self.state.setdefault('option_expirations', {})[symbol] = dict(observed_at=observed, dates=expirations)
            returned = set()
            for block in root.get('options', []):
                block_expiry = datetime.fromtimestamp(int(block['expirationDate']), timezone.utc).date().isoformat()
                returned.add((symbol, block_expiry))
                try:
                    self._store_chain(symbol, block, observed)
                except OptionDataError:
                    if expiry is not None:
                        raise
                    # Valid expiry metadata may accompany an unusable default
                    # chain; the requested expiry is checked separately.
            if expiry is not None and (symbol, expiry) not in returned:
                raise OptionDataError('Requested expiry absent from Yahoo response', 'no_data')
            if expiry is None and not expirations:
                raise OptionDataError('Yahoo returned no expirations', 'no_data')
            self.consecutive_failures = 0
            return expirations
        except Exception as exc:
            if not isinstance(exc, OptionDataError):
                # Avoid exposing request URLs containing authentication parameters.
                category = 'rate_limit' if 'rate' in type(exc).__name__.lower() else 'unavailable'
                exc = OptionDataError(f'Yahoo request failed ({type(exc).__name__})', category)
            self.failures[key] = exc
            self.last_error = str(exc)
            if exc.category in ('access', 'rate_limit', 'unavailable'):
                self.consecutive_failures += 1
                if self.consecutive_failures >= self.failure_limit:
                    self.open_until = time.time()+self.cooldown_minutes*60
            else:
                self.consecutive_failures = 0
            raise exc

    def expirations(self, symbol):
        cached = self.state.setdefault('option_expirations', {}).get(symbol)
        if cached and 0 <= time.time()-cached['observed_at'] < 24*3600:
            self.cache_hits += 1
            return cached['dates']
        return self.fetch(symbol)

    def chain(self, symbol, expiry, max_age=None):
        max_age = self.max_age if max_age is None else min(max_age, self.max_age)
        cached = self.chains.get((symbol, expiry))
        if cached and 0 <= time.time()-cached.observed_at <= max_age:
            self.cache_hits += 1
        else:
            self.fetch(symbol, expiry)
        if self.chains[(symbol, expiry)].put_status == 'empty_unconfirmed':
            # One additional paced HTTP response, never a repeated cache read.
            self.fetch(symbol, expiry)
            checked = self.chains[(symbol, expiry)]
            if checked.put_status == 'empty_unconfirmed':
                checked.put_status = 'empty_confirmed'
                self.raw_chains[f'{symbol}:{expiry}']['put_status'] = 'empty_confirmed'
        return self.chains[(symbol, expiry)]
