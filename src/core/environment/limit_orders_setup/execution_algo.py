import math
import copy
import numpy as np
from datetime import datetime, timedelta
from decimal import Decimal
from random import randint
import random


BUCKET_SIZES_IN_SECS = {"1m": 7,
                        "2m": 15,
                        "3m": 22.5,
                        "4m": 24,
                        "5m": 30,
                        "10m": 60,
                        "30m": 180,
                        "1h": 300,
                        "2h": 600,
                        "3h": 900,
                        "4h": 1200}


def split_across_buckets(quantity, n_splits, ticks):
    base_vol, extra_vol = divmod(quantity * int(1/ticks), n_splits)
    base_vol = Decimal(str(base_vol/int(1/ticks)))
    extra_vol = Decimal(str(extra_vol/int(1/ticks)))
    ticks_dec = Decimal(str(ticks))
    split = [base_vol + ((Decimal(str(i)) * ticks_dec < extra_vol) * ticks_dec) for i in range(n_splits)]
    return split


def _get_execution_times(algo, idx):
    sample_placements = algo.bucket_placement_func(algo.no_of_slices)
    if not isinstance(sample_placements, list):
        sample_placements = [sample_placements]
    t_diff = algo.buckets.bucket_bounds[idx + 1] - algo.buckets.bucket_bounds[idx]
    bucket_trades = [algo.buckets.bucket_bounds[idx] + t_diff * plcmt for plcmt in sample_placements]
    return bucket_trades


class Bucket:
    """ Bucket class acting as a helper for splitting trades across time.

        Args:
            start_time (datetime): Start time of bucket
            end_time (datetime): End time of bucket
            rand_width (datetime): randomisation (in secs) for bucket lengths (default: None)

    """

    def __init__(self, start_time, end_time, rand_width=None):

        self.start_time = start_time
        self.end_time = end_time
        self.duration = self.end_time - self.start_time
        if self.duration.total_seconds() < 0:
            raise ValueError("start_time can't take place before end_time")
        self.bucket_width = self._bucket_size(self.duration)
        self.bucket_bounds(rand_width)

    @staticmethod
    def _bucket_size(duration):
        """ Returns the pre-defined bucket length as function of parent order duration """

        # get the parent order duration in minutes & derive lookup key
        duration_in_m = duration.total_seconds() / 60.0
        if duration_in_m > 30:
            ceil_duration_in_h = int(divmod(duration_in_m, 60.0)[0] + 1)
            if ceil_duration_in_h >= 5:
                ceil_duration_in_h = 4
            dur_key = str(ceil_duration_in_h) + "h"
        elif 10 < duration_in_m <= 30:
            dur_key = "30m"
        elif 5 < duration_in_m <= 10:
            dur_key = "10m"
        else:
            dur_key = str(math.ceil(duration_in_m)) + "m"
        bucket_width = BUCKET_SIZES_IN_SECS[dur_key]
        return bucket_width

    def bucket_bounds(self, rand_width):
        """ Constructs bounds of the buckets which can be randomised """

        self.rand_width = rand_width

        # derive bucket bounds
        b_bounds = [self.start_time]
        while b_bounds[-1] < self.end_time:
            if rand_width:
                rand_add = randint(-rand_width, rand_width) # rand_width should be a % of the bucket_width, otherwise the bounds could be non-increasing.
            else:
                rand_add = 0
            b_bounds.append(b_bounds[-1] + timedelta(days= 0, seconds= self.bucket_width * ( 1 + rand_add/100)))

        # delete if one was added too much
        if b_bounds[-1] >= self.end_time:
            del b_bounds[-1]

        # finally add the end time of the last bucket & set length
        b_bounds.append(self.end_time)
        n_buckets = len(b_bounds) - 1

        self.bucket_bounds = b_bounds
        self.n_buckets = n_buckets
        return self.bucket_bounds, self.n_buckets


class ExecutionAlgo:
    """ Parent class for the benchmark algos as well as the RL algo itself.

        Args:
            trade_direction (int): The direction of the trade to execute, 1 stands for buying, -1 for selling.
            volume (int): volume to trade (i.e. parent order volume)
            start_time (string): start of execution in '%Y-%m-%d %H:%M:%S' format
            end_time (string): end of execution in '%Y-%m-%d %H:%M:%S' format
            no_of_slices (int): number of order splits within a bucket
            bucket_placement_func (func): function returning random splits of buckets
            tick_size (Decimal): tick size of the market the RL agent is trained on
            rand_bucket_bounds_width (int): Max % of the bucket width to add/substract from each bucket

        """

    def __init__(self,
                 trade_direction,
                 volume,
                 no_of_slices,
                 bucket_placement_func,
                 broker_data_feed,
                 start_time=None,
                 end_time=None,
                 rand_bucket_bounds_width=None,
                 ):

        # Raw inputs
        self.trade_direction = trade_direction
        self.volume = Decimal(str(volume))
        self.start_time = start_time
        self.end_time = end_time
        self.no_of_slices = no_of_slices
        self.bucket_placement_func = bucket_placement_func
        self.rand_bucket_bounds_width = rand_bucket_bounds_width
        self.broker_data_feed = broker_data_feed

    def reset(self):
        if type(self).__name__ != 'RLAlgo':
            self.volumes_per_trade = copy.deepcopy(self.volumes_per_trade_default)
        else:
            volumes_per_trade = []
            for bucket in range(len(self.volumes_per_trade)):
                volumes_per_trade.append(list([Decimal(0) for order_event in self.volumes_per_trade_default[bucket]]))
            self.volumes_per_trade = volumes_per_trade
        self.vol_remaining = Decimal(str(self.volume))
        self.bucket_vol_remaining = self.bucket_volumes.copy()
        self.event_idx = 0
        self.order_idx = 0
        self.bucket_idx = 0

    def _sample_execution_times(self):
        exec_times = []

        for i in range(0, self.buckets.n_buckets):
            bucket_trades = _get_execution_times(self, i)
            # make sure trade times are different from each other and from bucket bounds
            while len(set(bucket_trades)) < self.no_of_slices or \
                    any(trade in self.buckets.bucket_bounds for trade in bucket_trades):
                bucket_trades = _get_execution_times(self, i)
            exec_times.append(bucket_trades)
        self.execution_times = exec_times
        flat_exec_times = [item for sublist in exec_times for item in sublist]
        self.algo_events = sorted(list(set(flat_exec_times + self.buckets.bucket_bounds[1:])))

    def get_next_event(self):
        """ gets the time stamp for the next event which might trigger an order """

        event_time = self.algo_events[self.event_idx]
        event_type = 'order_placement'
        if not any(event_time in sublist for sublist in self.execution_times):
            event_type = 'bucket_bound'
        event = {'type': event_type, 'time': event_time}

        # update the event_idx
        if not self.event_idx == len(self.algo_events)-1:
            self.event_idx += 1
            done = False
        else:
            self.event_idx = 0
            done = True

        return event, done

    def get_order_at_event(self, event, lob, trade_id=None):

        if trade_id is None:
            trade_id = 1
        if self.trade_direction == 1:
            side = 'bid'
        else:
            side = 'ask'

        if event['type'] == 'order_placement':
            # place a limit order at best bid/ask -/+ 1 tick
            if side == 'bid':
                p = lob.get_best_bid() - self.tick_size
                # p = lob.get_best_bid() + 10 * self.tick_size # This allows for (partial) execution at the current LOB
            else:
                p = lob.get_best_ask() + self.tick_size
                # p = lob.get_best_ask() - 10 * self.tick_size # This allows for (partial) execution at the current LOB
            order = {'type': 'limit',
                     'timestamp': datetime.strftime(event['time'], '%Y-%m-%d %H:%M:%S.%f'),
                     'side': side,
                     'quantity': self.volumes_per_trade[self.bucket_idx][self.order_idx],
                     'price': p,
                     'trade_id': trade_id}
            self.order_idx += 1
        elif event['type'] == 'bucket_bound':
            # place a market order with remaining volume left in bucket
            order = {'type': 'market',
                     'timestamp': datetime.strftime(event['time'], '%Y-%m-%d %H:%M:%S.%f'),
                     'side': side,
                     'quantity': self.bucket_vol_remaining[self.bucket_idx],
                     'trade_id': trade_id}
        else:
            raise ValueError('No such event type allowed !!!')

        if self.order_idx / self.no_of_slices >= 1:
            self.order_idx = 0

        return order

    def update_remaining_volume(self, trade_log, event_type=None):
        if trade_log is not None and trade_log['quantity'] > 0:
            self.vol_remaining -= Decimal(str(trade_log['quantity']))
            self.bucket_vol_remaining[self.bucket_idx] -= Decimal(str(trade_log['quantity']))

        if self.vol_remaining < -self.tick_size * len(self.bucket_volumes) or self.bucket_vol_remaining[self.bucket_idx] < -self.tick_size:
            raise ValueError("More volume than available placed!")

        if event_type is not None and event_type == 'bucket_bound':
            self.bucket_idx += 1

    def plot_schedule(self, trade_logs=None):
        """ Plots the expected execution schedule determined ahead of trading """

        import matplotlib.pyplot as plt
        # Get all the volumes of all limit orders
        y = [float(item) for sublist in self.volumes_per_trade for item in sublist]
        # And the bucket bounds
        i = self.no_of_slices
        while i < len(y):
            y.insert(i, 0)
            i += (self.no_of_slices+1)
        y.insert(0, 0)
        y.append(0)

        x = self.algo_events.copy()
        x.insert(0, datetime.strptime(self.start_time, '%Y-%m-%d %H:%M:%S'))

        if trade_logs is not None:
            fig, (ax1, ax2) = plt.subplots(nrows=2, sharex=True, sharey= True)
            x_trades = [datetime.strptime(log['timestamp'], '%Y-%m-%d %H:%M:%S.%f')
                            for log in trade_logs if log['message'] == 'trade']
            y_trades = [float(log['quantity']) for log in trade_logs if log['message'] == 'trade']
            ax1.bar(x, y, color = 'b', width=0.00005, alpha=0.2, edgecolor='k', linewidth=0.5)
            ax2.bar(x_trades, y_trades, color='r', width=0.00005, alpha=0.2, edgecolor='k', linewidth=0.5)
            ax1.xaxis_date()
            ax2.xaxis_date()

            i = 0
            while i < len(y):
                ax1.axvline(x=x[i], color='k', linestyle='dashed', alpha=0.2)
                ax2.axvline(x=x[i], color='k', linestyle='dashed', alpha=0.2)
                i += (self.no_of_slices+1)
            ax1.set_title('Execution Schedule across Buckets')
            ax1.set(ylabel='Volume')
            ax2.set_title('Trades Executed across Buckets')
            ax2.set(xlabel='Time', ylabel='Volume')
            plt.show()

        else:
            fig, ax = plt.subplots()
            ax.bar(x, y, width=0.00005, alpha=0.8, edgecolor='k', linewidth=0.5)
            ax.xaxis_date()
            i = 0
            while i < len(y):
                plt.axvline(x=x[i], color='k', linestyle='dashed', alpha=0.2)
                i += (self.no_of_slices+1)
            plt.title('Execution Schedule across Buckets')
            plt.xlabel('Time')
            plt.ylabel('Tick Size')
            plt.show()

    def _split_volume_across_buckets(self):
        """ splits parent order volumes across the buckets """
        raise NotImplementedError

    def _split_volume_within_buckets(self):
        """ splits the volume of each bucket across the different trades within """
        raise NotImplementedError


class TWAPAlgo(ExecutionAlgo):
    """ Implementation of a TWAP Execution Algo based on the base algo logic """

    def __init__(self, *args, **kwargs):
        super(TWAPAlgo, self).__init__(*args, **kwargs)
        # get the tick size implied by LOB data_feed
        self.broker_data_feed.reset(time=self.start_time)
        dt, lob = self.broker_data_feed.next_lob_snapshot()
        v = lob.bids.get_price_list(lob.get_best_bid()).volume
        tick = Decimal(str(1 / (10 ** abs(v.as_tuple().exponent))))
        self.tick_size = tick
        # Derive trading schedules
        start_time = datetime.strptime(self.start_time, '%Y-%m-%d %H:%M:%S')
        end_time = datetime.strptime(self.end_time, '%Y-%m-%d %H:%M:%S')
        self.buckets = Bucket(start_time, end_time, self.rand_bucket_bounds_width)

        # split volume across buckets and check if this worked
        self._split_volume_across_buckets()
        if abs(np.sum(self.bucket_volumes) - self.volume) > self.tick_size:
            raise ValueError("Volumes split across buckets didn't work out!")

        # get execution times and split volume across orders/check
        self._sample_execution_times()
        self._split_volume_within_buckets()
        if abs(np.sum(self.volumes_per_trade) - self.volume) > self.tick_size:
            raise ValueError("Volumes split across orders didn't work out!")
        self.bmk_vwap = np.NaN

    def _split_volume_across_buckets(self):
        """ Aims to split volume across buckets as equal as possible """

        perc_last_bucket = (self.buckets.bucket_bounds[-1] - self.buckets.bucket_bounds[-2]) / \
                           (self.buckets.bucket_bounds[-1] - self.buckets.bucket_bounds[0])
        vol_last_bucket = math.floor(float(self.volume) * perc_last_bucket
                                     * int(1/self.tick_size))/int(1/self.tick_size)

        # distribute volume across all buckets and add remaining to last
        bucket_vol = split_across_buckets(float(self.volume) - vol_last_bucket,
                                          self.buckets.n_buckets - 1, float(self.tick_size))
        bucket_vol.append(Decimal(str(vol_last_bucket)))

        self.bucket_volumes = bucket_vol

    def _split_volume_within_buckets(self):
        """ Aims to split bucket volumes across trades as equal as possible """

        split_vols = []
        for i in range(self.buckets.n_buckets):
            vols_per_trade = split_across_buckets(float(self.bucket_volumes[i]),
                                                  self.no_of_slices, float(self.tick_size))
            split_vols.append(vols_per_trade)

        self.volumes_per_trade = split_vols
        self.volumes_per_trade_default = copy.deepcopy(split_vols)


class RLAlgo(ExecutionAlgo):
    """ Implementation of a RL Execution Algo class to use with the Broker """

    def __init__(self, benchmark_algo, *args, **kwargs):
        super(RLAlgo, self).__init__(*args, **kwargs)
        self.algo_events = benchmark_algo.algo_events
        self.start_time = benchmark_algo.start_time
        self.end_time = benchmark_algo.end_time
        self.execution_times = benchmark_algo.execution_times
        self.tick_size = benchmark_algo.tick_size
        self.buckets = benchmark_algo.buckets
        self.bucket_volumes = benchmark_algo.bucket_volumes.copy()
        self.bucket_vol_remaining = benchmark_algo.bucket_volumes.copy()
        self.volumes_per_trade = []
        for bucket in range(len(benchmark_algo.volumes_per_trade)):
            self.volumes_per_trade.append(list([Decimal(0) for order_event in benchmark_algo.volumes_per_trade_default[bucket]]))
        self.volumes_per_trade_default = copy.deepcopy(benchmark_algo.volumes_per_trade_default)

        self.vol_remaining = Decimal(str(self.volume))
        self.bucket_vol_remaining = self.bucket_volumes.copy()
        self.event_idx = 0
        self.order_idx = 0
        self.bucket_idx = 0
        self.rl_vwap = np.NaN
