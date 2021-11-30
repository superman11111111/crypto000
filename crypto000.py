from queue import Queue, LifoQueue
from threading import Thread

import datetime
import json
import time
import os

import ccxt
import numpy as np

from numba import jit
from numba import float64
from numba import int64


@jit((float64[:], int64), nopython=True, nogil=True)
def _ewma(arr_in: np.ndarray, window: int) -> np.ndarray:
    r"""Exponentialy weighted moving average specified by a decay ``window``
    to provide better adjustments for small windows via:

        y[t] = (x[t] + (1-a)*x[t-1] + (1-a)^2*x[t-2] + ... + (1-a)^n*x[t-n]) /
               (1 + (1-a) + (1-a)^2 + ... + (1-a)^n).

    Parameters
    ----------
    arr_in : np.ndarray, float64
        A single dimenisional numpy array
    window : int64
        The decay window, or 'span'

    Returns
    -------
    np.ndarray
        The EWMA vector, same length / shape as ``arr_in``

    Examples
    --------
    >>> import pandas as pd
    >>> a = np.arange(5, dtype=float)
    >>> exp = pd.DataFrame(a).ewm(span=10, adjust=True).mean()
    >>> np.array_equal(_ewma_infinite_hist(a, 10), exp.values.ravel())
    True
    """
    n = arr_in.shape[0]
    ewma = np.empty(n, dtype=float64)
    alpha = 2 / float(window + 1)
    w = 1
    ewma_old = arr_in[0]
    ewma[0] = ewma_old
    for i in range(1, n):
        w += (1-alpha)**i
        ewma_old = ewma_old*(1-alpha) + arr_in[i]
        ewma[i] = ewma_old / w
    return ewma


@jit((float64[:], int64), nopython=True, nogil=True)
def _ewma_infinite_hist(arr_in: np.ndarray, window: int) -> np.ndarray: 
    r"""Exponentialy weighted moving average specified by a decay ``window``
    assuming infinite history via the recursive form:

        (2) (i)  y[0] = x[0]; and
            (ii) y[t] = a*x[t] + (1-a)*y[t-1] for t>0.

    This method is less accurate that ``_ewma`` but
    much faster:

        In [1]: import numpy as np, bars
           ...: arreturn aaa(np.random.random(100000)
           ...: %timeit bars._ewma(arr, 10)
           ...: %timeit bars._ewma_infinite_hist(arr, 10)
        3.74 ms ± 60.2 µs per loop (mean ± std. dev. of 7 runs, 100 loops each)
        262 µs ± 1.54 µs per loop (mean ± std. dev. of 7 runs, 1000 loops each)

    Parameters
    ----------
    arr_in : np.ndarray, float64
        A single dimenisional numpy array
    window : int64
        The decay window, or 'span'

    Returns
    -------
    np.ndarray
        The EWMA vector, same length / shape as ``arr_in``

    Examples
    --------
    >>> import pandas as pd
    >>> a = np.arange(5, dtype=float)
    >>> exp = pd.DataFrame(a).ewm(span=10, adjust=False).mean()
    >>> np.array_equal(_ewma_infinite_hist(a, 10), exp.values.ravel())
    True
    """
    n = arr_in.shape[0]
    ewma = np.empty(n, dtype=float64)
    alpha = 2 / float(window + 1)
    ewma[0] = arr_in[0]
    for i in range(1, n):
        ewma[i] = arr_in[i] * alpha + ewma[i-1] * (1 - alpha)
    return ewma


class Trader:         
    configurable_keys = ['port', 'ex', 'ticker_interval', 'number_of_pairs', 'saving_batch_size', 'ohlc_limit', 'latency_logging', 'serve_api']


    def __init__(self, exchange, keyfile:str, config_file='', timeout=50000, enableRateLimit=True) -> None:
        self.ssid = int(time.time() * 1000)
        self.port = 3333
        self.serve_api = False
        self.ticker_interval = 5
        self.number_of_pairs = 1
        self.saving_batch_size = 32
        self.ohlc_limit = 50
        self.latency_logging = True
        self.ex = exchange

        if config_file: 
            config = json.loads(open(config_file, 'r').read())
            for key in Trader.configurable_keys:
                if key in config:
                    v = config[key]
                    if key == 'ex': 
                        v = getattr(ccxt, v)
                    setattr(self, key, v)

        key = json.loads(open(keyfile, 'r').read())
        args = {
            'apiKey': key['apiKey'],
            'secret': key['secret'],
            'passphrase': key['passphrase'],
            'password': key['passphrase'],
            'timeout': timeout,
            'enableRateLimit': enableRateLimit,
        }
        self.ex = self.ex(config=args)

        self.profit = 0
        self.roi = 0
        self.past_trades = []
        self.log = []
        self.signals = []
        self.calculation_times = []
        self.latencies = []
        self.ticker_queues = {}
        self.data = {}
        self.stakes = {}

        if self.latency_logging:
            self.latency_queue = LifoQueue()

        self.dirs = {}
        base = os.path.dirname(os.path.realpath(__file__))
        self.dirs['log'] = os.path.join(base, 'log')
        self.dirs['ohlc'] = os.path.join(base, 'ohlc_json')
        for v in self.dirs.values():
            if not os.path.isdir(v):
                os.mkdir(v)
        self.dirs['base'] = base

        
    def set_ticker_interval(self, interval:float) -> None:
        self.ticker_interval = interval


    def set_number_of_pairs(self, number_of_pairs:int) -> None:
        self.number_of_pairs = number_of_pairs
    

    def set_saving_batch_size(self, saving_batch_size:int) -> None:
        self.saving_batch_size = saving_batch_size


    def set_latency_logging(self, latency_logging:bool) -> None:
        self.latency_logging = latency_logging


    def set_serve_api(self, serve_api:bool) -> None:
        self.serve_api = serve_api


    def set_port(self, port:int) -> None:
        self.port = port


    def set_ohlc_limit(self, ohlc_limit:int) -> None:
        self.ohlc_limit = ohlc_limit


    def populate_signal_queue(self, b:int, e:int, pair: str, signal_queue: Queue, ydata=list(), offset=0) -> None:
        lastts = 0
        if not pair in self.ticker_queues:
            raise Exception(f'No ticker queue for pair {pair}') 
        q = self.ticker_queues[pair]
        latencies = list()
        while True:
            if not q.empty():
                n = q.get()
                ts = n[0]
                if ts == lastts:
                    continue
                _starttime = time.time()
                lastts = ts
                now = time.time() * 1000
                latencies.append(int(now - ts))
                ydata.append([n[0], 0, n[1], n[2], (n[4] + n[5]) / 2, n[6]]) 
                y = np.array([n[4] for n in ydata])
                emabase = _ewma_infinite_hist(y, b)
                emaY = _ewma_infinite_hist(y, e)
                emadiff = emaY - emabase
                self.data[pair] = [y.tolist(), emabase.tolist(), emaY.tolist(), emadiff.tolist()]
                emasigndiff = np.diff(np.sign(emadiff))
                sell = ((emasigndiff < 0) * 1).astype('float')
                buy = ((emasigndiff > 0) * 1).astype('float')
                sell[sell == 0] = np.nan
                buy[buy == 0] = np.nan
                self.log.append(f'[{pair}]{latencies[-1]}ms || {y[-1]} {emadiff[-1]}')
                self.latencies.append([ts, int(sum(latencies)/len(latencies))])
                if self.latency_logging: 
                    self.latency_queue.put([ts, int(sum(latencies)/len(latencies))])
                if buy[-1] >= .9:
                    self.signals.append([ts, 'BUY', pair, y[-1]])
                    signal_queue.put([ts, 'BUY', pair, y[-1]])
                if sell[-1] >= .9: 
                    self.signals.append([ts, 'SELL', pair, y[-1]])
                    signal_queue.put([ts, 'SELL', pair, y[-1]])
                self.calculation_times.append(time.time() - _starttime)


    def do_buy(self, timestamp: int, pair: str, price: int) -> None: 
        msg = f'{pair} buy at {price}'
        self.past_trades.append(f'[{datetime.datetime.fromtimestamp(timestamp / 1000)}] {msg}\n')


    def do_sell(self, timestamp: int, pair: str, price: int, profit: int, roi: int) -> None: 
        msg = f'{pair} sell at {price} ({roi})'
        self.past_trades.append(f'[{datetime.datetime.fromtimestamp(timestamp / 1000)}] {msg}\n')

    
    def do_buy_live(self) -> None:
        pass


    def do_sell_live(self) -> None:
        pass


    def execute_trades_on_queue(self, q:Queue, stake=0, profit=0) -> None:
        while True: 
            if not q.empty():
                sgnl = q.get()
                now = time.time() * 1000
                sgnl_age = now - sgnl[0]
                # print(f'[{sgnl[2]}]', sgnl[1], 'signal is', int(sgnl_age), 'ms in the past')
                if sgnl_age < 5000:
                    if sgnl[2] not in self.stakes:
                        self.stakes[sgnl[2]] = 0
                    stake = self.stakes[sgnl[2]]
                    if sgnl[1] == 'BUY':
                        if stake == 0:
                            self.do_buy(sgnl[0], sgnl[2], sgnl[3])        
                            self.stakes[sgnl[2]] = sgnl[3]
                    if sgnl[1] == 'SELL':
                        if stake > 0: 
                            pp = sgnl[3] - stake
                            if pp > 0: 
                                roi = pp / stake
                                self.roi += roi
                                self.stakes[sgnl[2]] = 0
                                self.do_sell(sgnl[0], sgnl[2], sgnl[3], pp, roi)
                                self.profit += pp
                            else:
                                print('no sell because profit negative!!!')


    def populate_ticker_queue(self, pair: str) -> None:
        if not pair in self.ticker_queues: 
            self.ticker_queues[pair] = Queue()
        q = self.ticker_queues[pair]
        while True:
            # o = self.ex.fetch_order_book(pair)
            tkk = self.ex.fetch_ticker(pair)
            tk = tkk['info']
            n = [tk['time'], float(tk['high']), float(tk['low']), float(tk['averagePrice']), float(tk['buy']), float(tk['sell']), float(tk['vol']), float(tk['takerFeeRate']), float(tk['makerFeeRate'])]
            q.put(n)
            time.sleep(self.ticker_interval)


    def latency_bookkeeper(self) -> None: 
        X, Y = [], []
        while True:
            if not self.latency_queue.empty():
                if len(X) < self.saving_batch_size: 
                    avg = self.latency_queue.get()
                    X.append(avg[0])
                    Y.append(avg[1])
                else:
                    latencies_path = os.path.join(self.dirs['log'], f'latencies-{self.ssid}.npz')
                    if os.path.isfile(latencies_path):
                        loaded = np.load(latencies_path)
                        oldX = loaded['a'].tolist()
                        oldY = loaded['b'].tolist()
                        X = oldX + X
                        Y = oldY + Y
                    np.savez_compressed(latencies_path, a=np.array(X), b=np.array(Y))
                    X, Y = [], []
        

    def get_pairs(self, curr='usdt') -> list:
        return [x for x in list(self.ex.load_markets().keys()) if curr.lower() in x.split('/')[1].lower()] 


    def get_ohlc(self, pair, try_local=True) -> list: 
        pair_filesafe = pair.replace('/', '-')
        pp = os.path.join(self.dirs['ohlc'], f'{pair_filesafe}.json')
        if try_local: 
            if os.path.isfile(pp):
                return json.loads(open(pp, 'r').read())
        ohlc = self.ex.fetchOHLCV(pair, limit=self.ohlc_limit)
        open(pp, 'w').write(json.dumps(ohlc))
        return ohlc 


    def get_profit_per_second(self) -> int:
        t = int(time.time() * 1000) - self.ssid
        return self.profit / t


    def api(self): 
        from flask import Flask, jsonify
        app = Flask(__name__)
        import logging
        logging.getLogger('werkzeug').disabled = True
        os.environ['WERKZEUG_RUN_MAIN'] = 'true'
        def aaa(r):
            r.headers.add('Access-Control-Allow-Origin', '*')
            return r
        @app.route("/")
        def hello_world():
            return """
            <h1>
            <a href="/pps">pps</a><br><br>
            <a href="/log">log</a><br><br>
            <a href="/trades">trades</a><br><br>
            <a href="/profit">profit</a><br><br>
            <a href="/signals">signals</a><br><br>
            <a href="/calctimes">calctimes</a><br><br>
            <a href="/data">data</a><br><br>
            <a href="/roi">roi</a><br><br>
            <a href="/latencies">latencies</a><br><br>
            </h1>"""
        @app.route("/pps")
        def pps():
            return aaa(jsonify(self.get_profit_per_second()))
        @app.route("/log")
        def log():
            return aaa(jsonify(self.log))
        @app.route("/trades")
        def trades():
            return aaa(jsonify(self.past_trades))
        @app.route("/profit")
        def profit():
            return aaa(jsonify(self.profit))
        @app.route("/signals")
        def signals():
            return aaa(jsonify(self.signals))
        @app.route("/calctimes")
        def calctimes():
            return aaa(jsonify(self.calculation_times))
        @app.route("/latencies")
        def latencies():
            return aaa(jsonify(self.latencies))
        @app.route("/data")
        def data():
            return aaa(jsonify(self.data))
        @app.route("/roi")
        def roi():
            return aaa(jsonify(self.roi))
        print(f'Serving API on http://127.0.0.1:{self.port}')
        app.run(host='0.0.0.0', port=self.port)


    def export_config(self, file_path: str) -> None: 
        save_dict = {}
        for key in Trader.configurable_keys:
            v = self.__dict__[key]
            if isinstance(v, ccxt.Exchange):
                v = v.__class__.__name__
            save_dict[key] = v
        open(file_path, 'w').write(json.dumps(save_dict, indent=4, sort_keys=True))


    def __call__(self) -> None:
        print(self.__dict__)
        if self.serve_api: 
            serverThread = Thread(target=self.api)
            serverThread.daemon = True
            serverThread.start()
        signal_queue = Queue()
        pairs = self.get_pairs()
        threads = list()
        try:
            for pair in pairs[:self.number_of_pairs]: 
                t1 = Thread(target=self.populate_ticker_queue, args=(pair, ))
                t1.daemon = True
                t1.start()
                threads.append(t1)
            time.sleep(1)
            for pair in pairs[:self.number_of_pairs]:
                ohlc = self.get_ohlc(pair, try_local=False)
                t2 = Thread(target=self.populate_signal_queue, 
                    args=(10, 45, pair, signal_queue, ohlc, ))
                t2.daemon = True
                t2.start()
                threads.append(t2)
            t3 = Thread(target=self.execute_trades_on_queue, args=(signal_queue, ))
            t3.daemon = True
            t3.start()
            if self.latency_logging:
                t4 = Thread(target=self.latency_bookkeeper)
                t4.daemon = True
                t4.start()
        except KeyboardInterrupt:
            pass
        for th in threads:
            th.join()
        t3.join()
        t4.join()
        if self.serve_api:
            serverThread.join()


if __name__ == '__main__': 
    trader = Trader(ccxt.kucoin, 'key.json', 'config.json')
    trader()
    # trader.set_ticker_interval(60.0)
    # trader.set_number_of_pairs(20)
    # trader.set_saving_batch_size(64)
    # trader.set_latency_logging(False)
    # trader.set_serve_api(True)
    # trader.set_port(3333)
    # trader.set_ohlc_limit(100)
    # trader.export_config('config.json')
    # trader()