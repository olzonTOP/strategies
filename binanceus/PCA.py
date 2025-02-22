import operator

import numpy as np
from enum import Enum

import pywt
import talib.abstract as ta
from scipy.ndimage import gaussian_filter1d
from xgboost import XGBClassifier

import freqtrade.vendor.qtpylib.indicators as qtpylib
import arrow

from freqtrade.exchange import timeframe_to_minutes
from freqtrade.strategy import (IStrategy, merge_informative_pair, stoploss_from_open,
                                IntParameter, DecimalParameter, CategoricalParameter)

from typing import Dict, List, Optional, Tuple, Union
from pandas import DataFrame, Series
from functools import reduce
from datetime import datetime, timedelta, timezone
from freqtrade.persistence import Trade

# Get rid of pandas warnings during backtesting
import pandas as pd
import pandas_ta as pta

pd.options.mode.chained_assignment = None  # default='warn'

# Strategy specific imports, files must reside in same folder as strategy
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent))

import logging
import warnings

log = logging.getLogger(__name__)
# log.setLevel(logging.DEBUG)
warnings.simplefilter(action='ignore', category=pd.errors.PerformanceWarning)

import custom_indicators as cta
from finta import TA as fta

from sklearn.model_selection import RandomizedSearchCV, train_test_split
from sklearn.metrics import classification_report
from sklearn.metrics import ConfusionMatrixDisplay
from sklearn.preprocessing import StandardScaler, RobustScaler, MinMaxScaler
import sklearn.decomposition as skd
from sklearn.svm import SVC, SVR
from sklearn.utils.fixes import loguniform
from sklearn import preprocessing
from sklearn.tree import DecisionTreeClassifier
from sklearn.model_selection import GridSearchCV
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier, AdaBoostClassifier, VotingClassifier
from sklearn.naive_bayes import GaussianNB, MultinomialNB
from sklearn.neural_network import MLPClassifier, BernoulliRBM
from sklearn.neighbors import KNeighborsClassifier
from sklearn.linear_model import LogisticRegression, SGDClassifier
from sklearn.svm import LinearSVC
from sklearn.tree import DecisionTreeClassifier
from sklearn.ensemble import RandomForestClassifier
from sklearn.naive_bayes import GaussianNB
from sklearn.preprocessing import LabelEncoder, StandardScaler
from sklearn.discriminant_analysis import QuadraticDiscriminantAnalysis, LinearDiscriminantAnalysis
from sklearn.linear_model import LogisticRegression
from sklearn.manifold import LocallyLinearEmbedding

from sklearn.metrics import make_scorer
from sklearn.metrics import accuracy_score
from sklearn.metrics import precision_score
from sklearn.metrics import recall_score
from sklearn.metrics import f1_score
from sklearn.model_selection import cross_validate

import random

from prettytable import PrettyTable

from AutoEncoder import AutoEncoder
from CompressionAutoEncoder import CompressionAutoEncoder
from RBMEncoder import RBMEncoder

from LSTMAutoEncoder import LSTMAutoEncoder
from LSTM2AutoEncoder import LSTM2AutoEncoder

"""
####################################################################################
PCA - uses Principal Component Analysis to try and reduce the total set of indicators
      to more manageable dimensions, and predict the next gain step.
      
      This works by creating a PCA model of the available technical indicators. This produces a 
      mapping of the indicators and how they affect the outcome (entry/exit/hold). We choose only the
      mappings that have a significant effect and ignore the others. This significantly reduces the size
      of the problem.
      We then train a classifier model to predict entry or exit signals based on the known outcome in the
      informative data, and use it to predict entry/exit signals based on the real-time dataframe.
      
      Note that this is very slow to start up. This is mostly because we have to build the data on a rolling
      basis to avoid lookahead bias.
      
      In addition to the normal freqtrade packages, these strategies also require the installation of:
        random
        prettytable
        finta

####################################################################################
"""


class PCA(IStrategy):
    # Do *not* hyperopt for the roi and stoploss spaces (unless you turn off custom stoploss)

    # ROI table:
    minimal_roi = {
        "0": 0.05
    }

    # Stoploss:
    stoploss = -0.05

    # Trailing stop:
    trailing_stop = False
    trailing_stop_positive = None
    trailing_stop_positive_offset = 0.0
    trailing_only_offset_is_reached = False

    timeframe = '5m'

    inf_timeframe = '5m'

    use_custom_stoploss = True

    # Recommended
    use_entry_signal = True
    entry_profit_only = False
    ignore_roi_if_entry_signal = True

    # Required
    startup_candle_count: int = 128  # must be power of 2
    process_only_new_candles = False

    # Strategy-specific global vars

    dwt_window = startup_candle_count

    inf_mins = timeframe_to_minutes(inf_timeframe)
    data_mins = timeframe_to_minutes(timeframe)
    inf_ratio = int(inf_mins / data_mins)

    # These parameters control much of the behaviour because they control the generation of the training data
    # Unfortunately, these cannot be hyperopt params because they are used in populate_indicators, which is only run
    # once during hyperopt
    lookahead_hours = 1.0
    n_profit_stddevs = 0.0
    n_loss_stddevs = 0.0
    min_f1_score = 0.70

    curr_lookahead = int(12 * lookahead_hours)

    curr_pair = ""
    custom_trade_info = {}

    # profit/loss thresholds used for assessing entry/exit signals. Keep these realistic!
    # Note: if self.dynamic_gain_thresholds is True, these will be adjusted for each pair, based on historical mean
    default_profit_threshold = 0.3
    default_loss_threshold = -0.3
    profit_threshold = default_profit_threshold
    loss_threshold = default_loss_threshold
    dynamic_gain_thresholds = True  # dynamically adjust gain thresholds based on actual mean (beware, training data could be bad)

    num_pairs = 0
    pair_model_info = {}  # holds model-related info for each pair
    classifier_stats = {}  # holds statistics for each type of classifier (useful to rank classifiers

    # debug flags
    first_time = True  # mostly for debug
    first_run = True  # used to identify first time through entry/exit populate funcs

    dbg_scan_classifiers = False  # if True, scan all viable classifiers and choose the best. Very slow!
    dbg_test_classifier = True  # test clasifiers after fitting
    dbg_analyse_pca = False  # analyze PCA weights
    dbg_verbose = False  # controls debug output
    dbg_curr_df: DataFrame = None  # for debugging of current dataframe

    # variables to track state
    class State(Enum):
        INIT = 1
        POPULATE = 2
        STOPLOSS = 3
        RUNNING = 4

    ###################################

    # Strategy Specific Variable Storage

    ## Hyperopt Variables

    # PCA hyperparams
    # entry_pca_gain = IntParameter(1, 50, default=4, space='entry', load=True, optimize=True)
    #
    # exit_pca_gain = IntParameter(-1, -15, default=-4, space='exit', load=True, optimize=True)

    # Custom exit Profit (formerly Dynamic ROI)
    cexit_roi_type = CategoricalParameter(['static', 'decay', 'step'], default='step', space='exit', load=True,
                                          optimize=True)
    cexit_roi_time = IntParameter(720, 1440, default=720, space='exit', load=True, optimize=True)
    cexit_roi_start = DecimalParameter(0.01, 0.05, default=0.01, space='exit', load=True, optimize=True)
    cexit_roi_end = DecimalParameter(0.0, 0.01, default=0, space='exit', load=True, optimize=True)
    cexit_trend_type = CategoricalParameter(['rmi', 'ssl', 'candle', 'any', 'none'], default='any', space='exit',
                                            load=True, optimize=True)
    cexit_pullback = CategoricalParameter([True, False], default=True, space='exit', load=True, optimize=True)
    cexit_pullback_amount = DecimalParameter(0.005, 0.03, default=0.01, space='exit', load=True, optimize=True)
    cexit_pullback_respect_roi = CategoricalParameter([True, False], default=False, space='exit', load=True,
                                                      optimize=True)
    cexit_endtrend_respect_roi = CategoricalParameter([True, False], default=False, space='exit', load=True,
                                                      optimize=True)

    # Custom Stoploss
    cstop_loss_threshold = DecimalParameter(-0.05, -0.01, default=-0.03, space='exit', load=True, optimize=True)
    cstop_bail_how = CategoricalParameter(['roc', 'time', 'any', 'none'], default='none', space='exit', load=True,
                                          optimize=True)
    cstop_bail_roc = DecimalParameter(-5.0, -1.0, default=-3.0, space='exit', load=True, optimize=True)
    cstop_bail_time = IntParameter(60, 1440, default=720, space='exit', load=True, optimize=True)
    cstop_bail_time_trend = CategoricalParameter([True, False], default=True, space='exit', load=True, optimize=True)
    cstop_max_stoploss = DecimalParameter(-0.30, -0.01, default=-0.10, space='exit', load=True, optimize=True)

    ################################

    # subclasses should oiverride the following 2 functions - this is here as an example

    # Note: try to combine current/historical data (from populate_indicators) with future data
    #       If you only use future data, the ML training is just guessing
    #       Also, try to identify entry/exit ranges, rather than transitions - it gives the algorithms more chances
    #       to find a correlation. The framework will select the first one anyway.
    #       In other words, avoid using qtpylib.crossed_above() and qtpylib.crossed_below()
    #       Proably OK not to check volume, because we are just looking for patterns

    def get_train_entry_signals(self, future_df: DataFrame):

        print("!!! WARNING: using base class (entry) training implementation !!!")

        series = np.where(
            (
                    (future_df['mfi'] >= 80) &  # classic oversold threshold
                    (future_df['dwt'] <= future_df['future_min'])  # at min of future window
            ), 1.0, 0.0)

        return series

    def get_train_exit_signals(self, future_df: DataFrame):

        print("!!! WARNING: using base class (exit) training implementation !!!")

        series = np.where(
            (
                    (future_df['mfi'] <= 20) &  # classic overbought threshold
                    (future_df['dwt'] >= future_df['future_max'])  # at max of future window
            ), 1.0, 0.0)

        return series


    # override the following to add strategy-specific criteria to the (main) buy/sell conditions

    def get_strategy_buy_conditions(self, dataframe: DataFrame):
        return None

    def get_strategy_sell_conditions(self, dataframe: DataFrame):
        return None

    ################################

    """
    inf Pair Definitions
    """

    def inf_pairs(self):
        # # all pairs in the whitelist are also in the informative list
        # pairs = self.dp.current_whitelist()
        # inf_pairs = [(pair, self.inf_timeframe) for pair in pairs]
        # return inf_pairs
        return []

    ###################################

    """
    Indicator Definitions
    """

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:

        # TODO: add in classifiers from statsmodels
        # TODO; do grid search for MLP hyperparams
        # TODO: keeps stats on classifier performance and selection

        # Base pair inf timeframe indicators
        curr_pair = metadata['pair']
        self.curr_pair = curr_pair

        self.set_state(curr_pair, self.State.POPULATE)
        self.curr_lookahead = int(12 * self.lookahead_hours)
        self.dbg_curr_df = dataframe

        # reset profit/loss thresholds
        self.profit_threshold = self.default_profit_threshold
        self.loss_threshold = self.default_loss_threshold

        if PCA.first_time:
            PCA.first_time = False
            print("")
            print("***************************************")
            print("** Warning: startup can be very slow **")
            print("***************************************")

            print("    Lookahead: ", self.curr_lookahead, " candles (", self.lookahead_hours, " hours)")
            print("    Thresholds - Profit:{:.2f}% Loss:{:.2f}%".format(self.profit_threshold,
                                                                        self.loss_threshold))

        print("")
        print(curr_pair)

        # if first time through for this pair, add entry to pair_model_info
        if not (curr_pair in self.pair_model_info):
            self.pair_model_info[curr_pair] = {
                'interval': 0,
                'pca_size': 0,
                'pca': None,
                'clf_entry_name': "",
                'clf_entry': None,
                'clf_exit_name': "",
                'clf_exit': None
            }
        else:
            # decrement interval. When this reaches 0 it will trigger re-fitting of the data
            self.pair_model_info[curr_pair]['interval'] = self.pair_model_info[curr_pair]['interval'] - 1

        # populate the normal dataframe
        dataframe = self.add_indicators(dataframe)

        entries, exits = self.create_training_data(dataframe)

        # drop last group (because there cannot be a prediction)
        df = dataframe.iloc[:-self.curr_lookahead]
        entries = entries.iloc[:-self.curr_lookahead]
        exits = exits.iloc[:-self.curr_lookahead]

        # Principal Component Analysis of inf data

        # train the models on the informative data
        if self.dbg_verbose:
            print("    training models...")
        self.train_models(curr_pair, df, entries, exits)
        # add predictions

        if self.dbg_verbose:
            print("    running predictions...")

        # get predictions (Note: do not modify dataframe between calls)
        pred_entries = self.predict_entry(dataframe, curr_pair)
        pred_exits = self.predict_exit(dataframe, curr_pair)
        dataframe['predict_entry'] = pred_entries
        dataframe['predict_exit'] = pred_exits

        # Custom Stoploss
        if self.dbg_verbose:
            print("    updating stoploss data...")
        self.add_stoploss_indicators(dataframe, curr_pair)

        return dataframe

    ###################################

    # add indicators used by stoploss/custom sell logic
    def add_stoploss_indicators(self, dataframe, pair) -> DataFrame:
        if not pair in self.custom_trade_info:
            self.custom_trade_info[pair] = {}
            if not 'had_trend' in self.custom_trade_info[pair]:
                self.custom_trade_info[pair]['had_trend'] = False

        # RMI: https://www.tradingview.com/script/kwIt9OgQ-Relative-Momentum-Index/
        dataframe['rmi'] = cta.RMI(dataframe, length=24, mom=5)

        # MA Streak: https://www.tradingview.com/script/Yq1z7cIv-MA-Streak-Can-Show-When-a-Run-Is-Getting-Long-in-the-Tooth/
        dataframe['mastreak'] = cta.mastreak(dataframe, period=4)

        # Trends
        dataframe['candle_up'] = np.where(dataframe['close'] >= dataframe['close'].shift(), 1.0, -1.0)
        dataframe['candle_up_trend'] = np.where(dataframe['candle_up'].rolling(5).sum() > 0.0, 1.0, -1.0)
        dataframe['candle_up_seq'] = dataframe['candle_up'].rolling(5).sum()

        dataframe['candle_dn'] = np.where(dataframe['close'] < dataframe['close'].shift(), 1.0, -1.0)
        dataframe['candle_dn_trend'] = np.where(dataframe['candle_up'].rolling(5).sum() > 0.0, 1.0, -1.0)
        dataframe['candle_dn_seq'] = dataframe['candle_up'].rolling(5).sum()

        dataframe['rmi_up'] = np.where(dataframe['rmi'] >= dataframe['rmi'].shift(), 1.0, -1.0)
        dataframe['rmi_up_trend'] = np.where(dataframe['rmi_up'].rolling(5).sum() > 0.0, 1.0, -1.0)

        dataframe['rmi_dn'] = np.where(dataframe['rmi'] <= dataframe['rmi'].shift(), 1.0, -1.0)
        dataframe['rmi_dn_count'] = dataframe['rmi_dn'].rolling(8).sum()

        # Indicators used only for ROI and Custom Stoploss
        ssldown, sslup = cta.SSLChannels_ATR(dataframe, length=21)
        dataframe['sroc'] = cta.SROC(dataframe, roclen=21, emalen=13, smooth=21)
        dataframe['ssl_dir'] = 0
        dataframe['ssl_dir'] = np.where(sslup > ssldown, 1.0, -1.0)

        return dataframe

    # populate dataframe with desired technical indicators
    # NOTE: OK to throw (almost) anything in here, just add it to the parameter list
    # The whole idea is to create a dimension-reduced mapping anyway
    # Warning: do not use indicators that might produce 'inf' results, it messes up the scaling
    def add_indicators(self, dataframe: DataFrame) -> DataFrame:

        win_size = max(self.curr_lookahead, 14)

        # these averages are used internally, do not remove!
        dataframe['sma'] = ta.SMA(dataframe, timeperiod=win_size)
        dataframe['ema'] = ta.EMA(dataframe, timeperiod=win_size)
        dataframe['tema'] = ta.TEMA(dataframe, timeperiod=win_size)
        dataframe['tema_stddev'] = dataframe['tema'].rolling(win_size).std()

        # RSI
        period = 14
        smoothD = 3
        SmoothK = 3
        dataframe['rsi'] = ta.RSI(dataframe, timeperiod=win_size)
        stochrsi = (dataframe['rsi'] - dataframe['rsi'].rolling(period).min()) / (
                dataframe['rsi'].rolling(period).max() - dataframe['rsi'].rolling(period).min())
        dataframe['srsi_k'] = stochrsi.rolling(SmoothK).mean() * 100
        dataframe['srsi_d'] = dataframe['srsi_k'].rolling(smoothD).mean()

        # Bollinger Bands (must include these)
        bollinger = qtpylib.bollinger_bands(dataframe['close'], window=20, stds=2)
        dataframe['bb_lowerband'] = bollinger['lower']
        dataframe['bb_middleband'] = bollinger['mid']
        dataframe['bb_upperband'] = bollinger['upper']
        dataframe['bb_width'] = ((dataframe['bb_upperband'] - dataframe['bb_lowerband']) / dataframe['bb_middleband'])
        dataframe["bb_gain"] = ((dataframe["bb_upperband"] - dataframe["close"]) / dataframe["close"])
        dataframe["bb_loss"] = ((dataframe["bb_lowerband"] - dataframe["close"]) / dataframe["close"])

        # Donchian Channels
        dataframe['dc_upper'] = ta.MAX(dataframe['high'], timeperiod=win_size)
        dataframe['dc_lower'] = ta.MIN(dataframe['low'], timeperiod=win_size)
        dataframe['dc_mid'] = ta.TEMA(((dataframe['dc_upper'] + dataframe['dc_lower']) / 2), timeperiod=win_size)

        dataframe["dcbb_dist_upper"] = (dataframe["dc_upper"] - dataframe['bb_upperband'])
        dataframe["dcbb_dist_lower"] = (dataframe["dc_lower"] - dataframe['bb_lowerband'])

        # Fibonacci Levels (of Donchian Channel)
        dataframe['dc_dist'] = (dataframe['dc_upper'] - dataframe['dc_lower'])
        # dataframe['dc_hf'] = dataframe['dc_upper'] - dataframe['dc_dist'] * 0.236  # Highest Fib
        # dataframe['dc_chf'] = dataframe['dc_upper'] - dataframe['dc_dist'] * 0.382  # Centre High Fib
        # dataframe['dc_clf'] = dataframe['dc_upper'] - dataframe['dc_dist'] * 0.618  # Centre Low Fib
        # dataframe['dc_lf'] = dataframe['dc_upper'] - dataframe['dc_dist'] * 0.764  # Low Fib

        #  # Keltner Channels
        # keltner = qtpylib.keltner_channel(dataframe)
        # dataframe["kc_upper"] = keltner["upper"]
        # dataframe["kc_lower"] = keltner["lower"]
        # dataframe["kc_mid"] = keltner["mid"]

        # Williams %R
        dataframe['wr'] = 0.02 * (williams_r(dataframe, period=14) + 50.0)

        # Fisher RSI
        rsi = 0.1 * (dataframe['rsi'] - 50)
        dataframe['fisher_rsi'] = (np.exp(2 * rsi) - 1) / (np.exp(2 * rsi) + 1)

        # Combined Fisher RSI and Williams %R
        dataframe['fisher_wr'] = (dataframe['wr'] + dataframe['fisher_rsi']) / 2.0

        # RSI
        # dataframe['rsi'] = ta.RSI(dataframe, timeperiod=win_size)
        dataframe['rsi_14'] = ta.RSI(dataframe, timeperiod=14)

        # # EMAs
        # dataframe['ema_12'] = ta.EMA(dataframe, timeperiod=12)
        # dataframe['ema_20'] = ta.EMA(dataframe, timeperiod=20)
        # dataframe['ema_25'] = ta.EMA(dataframe, timeperiod=25)
        # dataframe['ema_35'] = ta.EMA(dataframe, timeperiod=35)
        # dataframe['ema_50'] = ta.EMA(dataframe, timeperiod=50)
        # dataframe['ema_100'] = ta.EMA(dataframe, timeperiod=100)
        # dataframe['ema_200'] = ta.EMA(dataframe, timeperiod=200)

        # SMA
        dataframe['sma_200'] = ta.SMA(dataframe, timeperiod=200)
        dataframe['sma_200_dec_20'] = np.where(dataframe['sma_200'] < dataframe['sma_200'].shift(20), 1.0, -1.0)
        dataframe['sma_200_dec_24'] = np.where(dataframe['sma_200'] < dataframe['sma_200'].shift(24), 1.0, -1.0)

        # # CMF
        # dataframe['cmf'] = chaikin_money_flow(dataframe, 20)

        # # CTI
        # dataframe['cti'] = pta.cti(dataframe["close"], length=20)

        # CRSI (3, 2, 100)
        crsi_closechange = dataframe['close'] / dataframe['close'].shift(1)
        crsi_updown = np.where(crsi_closechange.gt(1), 1.0, np.where(crsi_closechange.lt(1), -1.0, -1.0))
        dataframe['crsi'] = (ta.RSI(dataframe['close'], timeperiod=3) + ta.RSI(crsi_updown, timeperiod=2) + ta.ROC(
            dataframe['close'],
            100)) / 3

        # Williams %R
        dataframe['r_14'] = williams_r(dataframe, period=14)
        dataframe['r_480'] = williams_r(dataframe, period=480)

        # ROC
        dataframe['roc_9'] = ta.ROC(dataframe, timeperiod=9)

        # # T3 Average
        # dataframe['t3_avg'] = t3_average(dataframe)

        # # S/R
        # res_series = dataframe['high'].rolling(window=5, center=True).apply(lambda row: is_resistance(row),
        #                                                                     raw=True).shift(2)
        # sup_series = dataframe['low'].rolling(window=5, center=True).apply(lambda row: is_support(row),
        #                                                                    raw=True).shift(2)
        # dataframe['res_level'] = Series(
        #     np.where(res_series,
        #              np.where(dataframe['close'] > dataframe['open'], dataframe['close'], dataframe['open']),
        #              float('NaN'))).ffill()
        # dataframe['res_hlevel'] = Series(np.where(res_series, dataframe['high'], float('NaN'))).ffill()
        # dataframe['sup_level'] = Series(
        #     np.where(sup_series,
        #              np.where(dataframe['close'] < dataframe['open'], dataframe['close'], dataframe['open']),
        #              float('NaN'))).ffill()

        # Pump protections
        dataframe['hl_pct_change_48'] = range_percent_change(dataframe, 'HL', 48)
        dataframe['hl_pct_change_36'] = range_percent_change(dataframe, 'HL', 36)
        dataframe['hl_pct_change_24'] = range_percent_change(dataframe, 'HL', 24)
        dataframe['hl_pct_change_12'] = range_percent_change(dataframe, 'HL', 12)
        dataframe['hl_pct_change_6'] = range_percent_change(dataframe, 'HL', 6)

        # ADX
        dataframe['adx'] = ta.ADX(dataframe)

        # Plus Directional Indicator / Movement
        dataframe['dm_plus'] = ta.PLUS_DM(dataframe)
        dataframe['di_plus'] = ta.PLUS_DI(dataframe)

        # Minus Directional Indicator / Movement
        dataframe['dm_minus'] = ta.MINUS_DM(dataframe)
        dataframe['di_minus'] = ta.MINUS_DI(dataframe)
        dataframe['dm_delta'] = dataframe['dm_plus'] - dataframe['dm_minus']
        dataframe['di_delta'] = dataframe['di_plus'] - dataframe['di_minus']

        # MACD
        macd = ta.MACD(dataframe)
        dataframe['macd'] = macd['macd']
        dataframe['macdsignal'] = macd['macdsignal']
        dataframe['macdhist'] = macd['macdhist']

        # Stoch fast
        stoch_fast = ta.STOCHF(dataframe)
        dataframe['fastd'] = stoch_fast['fastd']
        dataframe['fastk'] = stoch_fast['fastk']
        dataframe['fast_diff'] = dataframe['fastd'] - dataframe['fastk']

        # # SAR Parabol
        # dataframe['sar'] = ta.SAR(dataframe)

        dataframe['mom'] = ta.MOM(dataframe, timeperiod=14)

        # priming indicators
        dataframe['color'] = np.where((dataframe['close'] > dataframe['open']), 1.0, -1.0)
        dataframe['rsi_7'] = ta.RSI(dataframe, timeperiod=7)
        dataframe['roc_6'] = ta.ROC(dataframe, timeperiod=6)
        dataframe['primed'] = np.where(dataframe['color'].rolling(3).sum() == 3.0, 1.0, -1.0)
        dataframe['in_the_mood'] = np.where(dataframe['rsi_7'] > dataframe['rsi_7'].rolling(12).mean(), 1.0, -1.0)
        dataframe['moist'] = np.where(qtpylib.crossed_above(dataframe['macd'], dataframe['macdsignal']), 1.0, -1.0)
        dataframe['throbbing'] = np.where(dataframe['roc_6'] > dataframe['roc_6'].rolling(12).mean(), 1.0, -1.0)

        # MFI
        dataframe['mfi'] = ta.MFI(dataframe)
        # dataframe['mfi_norm'] = self.norm_column(dataframe['mfi'])
        # dataframe['mfi_entry'] = np.where((dataframe['mfi_norm'] > 0.5), 1.0, 0.0)
        # dataframe['mfi_exit'] = np.where((dataframe['mfi_norm'] <= -0.5), 1.0, 0.0)
        # dataframe['mfi_signal'] = dataframe['mfi_buy'] - dataframe['mfi_sell']

        # Volume Flow Indicator (MFI) for volume based on the direction of price movement
        # dataframe['vfi'] = fta.VFI(dataframe, period=14)
        # dataframe['vfi_norm'] = self.norm_column(dataframe['vfi'])
        # dataframe['vfi_entry'] = np.where((dataframe['vfi_norm'] > 0.5), 1.0, 0.0)
        # dataframe['vfi_exit'] = np.where((dataframe['vfi_norm'] <= -0.5), 1.0, 0.0)
        # dataframe['vfi_signal'] = dataframe['vfi_buy'] - dataframe['vfi_sell']

        # ATR
        dataframe['atr'] = ta.ATR(dataframe, timeperiod=win_size)
        # dataframe['atr_norm'] = self.norm_column(dataframe['atr'])
        # dataframe['atr_entry'] = np.where((dataframe['atr_norm'] > 0.5), 1.0, 0.0)
        # dataframe['atr_exit'] = np.where((dataframe['atr_norm'] <= -0.5), 1.0, 0.0)
        # dataframe['atr_signal'] = dataframe['atr_buy'] - dataframe['atr_sell']

        # Hilbert Transform Indicator - SineWave
        hilbert = ta.HT_SINE(dataframe)
        dataframe['htsine'] = hilbert['sine']
        dataframe['htleadsine'] = hilbert['leadsine']

        # Oscillators

        # EWO
        dataframe['ewo'] = ewo(dataframe, 50, 200)

        # Ultimate Oscillator
        dataframe['uo'] = ta.ULTOSC(dataframe)

        # Aroon, Aroon Oscillator
        aroon = ta.AROON(dataframe)
        dataframe['aroonup'] = aroon['aroonup']
        dataframe['aroondown'] = aroon['aroondown']
        dataframe['aroonosc'] = ta.AROONOSC(dataframe)

        # Awesome Oscillator
        dataframe['ao'] = qtpylib.awesome_oscillator(dataframe)

        # Commodity Channel Index: values [Oversold:-100, Overbought:100]
        dataframe['cci'] = ta.CCI(dataframe)

        # DWT model
        # if in backtest or hyperopt, then we have to do rolling calculations
        if self.dp.runmode.value in ('hyperopt', 'backtest', 'plot'):
            dataframe['dwt'] = dataframe['close'].rolling(window=self.dwt_window).apply(self.roll_get_dwt)
            dataframe['smooth'] = dataframe['close'].rolling(window=self.dwt_window).apply(self.roll_smooth)
            dataframe['dwt_smooth'] = dataframe['dwt'].rolling(window=self.dwt_window).apply(self.roll_smooth)
        else:
            dataframe['dwt'] = self.get_dwt(dataframe['close'])
            dataframe['smooth'] = gaussian_filter1d(dataframe['close'], 2)
            dataframe['dwt_smooth'] = gaussian_filter1d(dataframe['dwt'], 2)

        # smoothed version - useful for trends
        # dataframe['dwt_smooth'] = gaussian_filter1d(dataframe['dwt'], 8)
        # dataframe['mid'] = abs((dataframe['close'] - dataframe['open']) / 2.0)

        dataframe['dwt_gain'] = 100.0 * (dataframe['dwt'] - dataframe['dwt'].shift()) / dataframe['dwt'].shift()
        dataframe['dwt_profit'] = dataframe['dwt_gain'].clip(lower=0.0)
        dataframe['dwt_loss'] = dataframe['dwt_gain'].clip(upper=0.0)

        # Sequences of consecutive up/downs
        dataframe['dwt_dir'] = 0.0
        dataframe['dwt_dir'] = np.where(dataframe['dwt_smooth'].diff() > 0, 1.0, -1.0)

        dataframe['dwt_dir_up'] = dataframe['dwt_dir'].clip(lower=0.0)
        dataframe['dwt_nseq_up'] = dataframe['dwt_dir_up'] * (dataframe['dwt_dir_up'].groupby(
            (dataframe['dwt_dir_up'] != dataframe['dwt_dir_up'].shift()).cumsum()).cumcount() + 1)
        dataframe['dwt_nseq_up'] = dataframe['dwt_nseq_up'].clip(lower=0.0, upper=20.0) # removes startup artifacts

        dataframe['dwt_dir_dn'] = abs(dataframe['dwt_dir'].clip(upper=0.0))
        dataframe['dwt_nseq_dn'] = dataframe['dwt_dir_dn'] * (dataframe['dwt_dir_dn'].groupby(
            (dataframe['dwt_dir_dn'] != dataframe['dwt_dir_dn'].shift()).cumsum()).cumcount() + 1)
        dataframe['dwt_nseq_dn'] = dataframe['dwt_nseq_dn'].clip(lower=0.0, upper=20.0)


        # TODO: remove/fix any columns that contain 'inf'
        self.check_inf(dataframe)

        # TODO: fix NaNs
        dataframe.fillna(0.0, inplace=True)

        return dataframe

    # 'hidden' indicators. These are ostensibly backward looking, but may inadvertently use means etc.
    def add_hidden_indicators(self, dataframe: DataFrame) -> DataFrame:

        win_size = max(self.curr_lookahead, 14)

        dataframe['dwt_deriv'] = np.gradient(dataframe['dwt_smooth'])
        # dataframe['dwt_deriv'] = np.gradient(dataframe['dwt'])
        dataframe['dwt_top'] = np.where(qtpylib.crossed_below(dataframe['dwt_deriv'], 0.0), 1, 0)
        dataframe['dwt_bottom'] = np.where(qtpylib.crossed_above(dataframe['dwt_deriv'], 0.0), 1, 0)

        dataframe['dwt_diff'] = 100.0 * (dataframe['dwt'] - dataframe['close']) / dataframe['close']
        dataframe['dwt_smooth_diff'] = 100.0 * (dataframe['dwt'] - dataframe['dwt_smooth']) / dataframe['dwt_smooth']

        dataframe['dwt_trend'] = np.where(dataframe['dwt_dir'].rolling(5).sum() > 3.0, 1.0, -1.0)


        # get rolling mean & stddev so that we have a localised estimate of (recent) activity
        dataframe['dwt_mean'] = dataframe['dwt'].rolling(win_size).mean()
        dataframe['dwt_std'] = dataframe['dwt'].rolling(win_size).std()
        dataframe['dwt_profit_mean'] = dataframe['dwt_profit'].rolling(win_size).mean()
        dataframe['dwt_profit_std'] = dataframe['dwt_profit'].rolling(win_size).std()
        dataframe['dwt_loss_mean'] = dataframe['dwt_loss'].rolling(win_size).mean()
        dataframe['dwt_loss_std'] = dataframe['dwt_loss'].rolling(win_size).std()

        # Sequences of consecutive up/downs
        dataframe['dwt_nseq'] = dataframe['dwt_dir'].rolling(window=win_size, min_periods=1).sum()

        dataframe['dwt_nseq_up'] = dataframe['dwt_nseq'].clip(lower=0.0)
        dataframe['dwt_nseq_up_mean'] = dataframe['dwt_nseq_up'].rolling(window=win_size).mean()
        dataframe['dwt_nseq_up_std'] = dataframe['dwt_nseq_up'].rolling(window=win_size).std()
        dataframe['dwt_nseq_up_thresh'] = dataframe['dwt_nseq_up_mean'] + \
                                          self.n_profit_stddevs * dataframe['dwt_nseq_up_std']
        dataframe['dwt_nseq_exit'] = np.where(dataframe['dwt_nseq_up'] > dataframe['dwt_nseq_up_thresh'], 1.0, 0.0)

        dataframe['dwt_nseq_dn'] = dataframe['dwt_nseq'].clip(upper=0.0)
        dataframe['dwt_nseq_dn_mean'] = dataframe['dwt_nseq_dn'].rolling(window=win_size).mean()
        dataframe['dwt_nseq_dn_std'] = dataframe['dwt_nseq_dn'].rolling(window=win_size).std()
        dataframe['dwt_nseq_dn_thresh'] = dataframe['dwt_nseq_dn_mean'] - self.n_loss_stddevs * dataframe[
            'dwt_nseq_dn_std']
        dataframe['dwt_nseq_entry'] = np.where(dataframe['dwt_nseq_dn'] < dataframe['dwt_nseq_dn_thresh'], 1.0, 0.0)

        # Recent min/max
        dataframe['dwt_recent_min'] = dataframe['dwt'].rolling(window=win_size).min()
        dataframe['dwt_recent_max'] = dataframe['dwt'].rolling(window=win_size).max()
        dataframe['dwt_maxmin'] = 100.0 * (dataframe['dwt_recent_max'] - dataframe['dwt_recent_min']) / \
                                  dataframe['dwt_recent_max']
        dataframe['dwt_delta_min'] = 100.0 * (dataframe['dwt_recent_min'] - dataframe['close']) / \
                                  dataframe['close']
        dataframe['dwt_delta_max'] = 100.0 * (dataframe['dwt_recent_max'] - dataframe['close']) / \
                                  dataframe['close']
        # longer term high/low
        dataframe['dwt_low'] = dataframe['dwt_smooth'].rolling(window=self.startup_candle_count).min()
        dataframe['dwt_high'] = dataframe['dwt_smooth'].rolling(window=self.startup_candle_count).max()

        # # these are (primarily) clues for the ML algorithm:
        # dataframe['dwt_at_min'] = np.where(dataframe['dwt_smooth'] <= dataframe['dwt_recent_min'], 1.0, 0.0)
        # dataframe['dwt_at_max'] = np.where(dataframe['dwt_smooth'] >= dataframe['dwt_recent_max'], 1.0, 0.0)
        dataframe['dwt_at_low'] = np.where(dataframe['dwt_smooth'] <= dataframe['dwt_low'], 1.0, 0.0)
        dataframe['dwt_at_high'] = np.where(dataframe['dwt_smooth'] >= dataframe['dwt_high'], 1.0, 0.0)

        return dataframe

    # calculate future gains. Used for setting targets
    def add_future_data(self, dataframe: DataFrame) -> DataFrame:

        # yes, we lookahead in the data!
        lookahead = self.curr_lookahead

        win_size = max(lookahead, 14)

        # make a copy of the dataframe so that we do not put any forward looking data into the main dataframe
        # Also, use a different name to avoid cut & paste errors
        future_df = dataframe.copy()

        # we can either use the actual closing price, or the DWT model (smoother)

        use_dwt = True
        if use_dwt:
            price_col = 'full_dwt'

            # get the 'full' DWT transform. This models the entire dataframe, so cannot be used in the 'main' dataframe
            future_df['full_dwt'] = self.get_dwt(dataframe['close'])

        else:
            price_col = 'close'
            future_df['full_dwt'] = 0.0

        # calculate future gains
        future_df['future_close'] = future_df[price_col].shift(-lookahead)

        future_df['future_gain'] = 100.0 * (future_df['future_close'] - future_df[price_col]) / future_df[price_col]
        future_df['future_gain'].clip(lower=-5.0, upper=5.0, inplace=True)

        future_df['future_profit'] = future_df['future_gain'].clip(lower=0.0)
        future_df['future_loss'] = future_df['future_gain'].clip(upper=0.0)

        # get rolling mean & stddev so that we have a localised estimate of (recent) future activity
        # Note: window in past because we already looked forward
        future_df['profit_mean'] = future_df['future_profit'].rolling(win_size).mean()
        future_df['profit_std'] = future_df['future_profit'].rolling(win_size).std()
        future_df['profit_max'] = future_df['future_profit'].rolling(win_size).max()
        future_df['profit_min'] = future_df['future_profit'].rolling(win_size).min()
        future_df['loss_mean'] = future_df['future_loss'].rolling(win_size).mean()
        future_df['loss_std'] = future_df['future_loss'].rolling(win_size).std()
        future_df['loss_max'] = future_df['future_loss'].rolling(win_size).max()
        future_df['loss_min'] = future_df['future_loss'].rolling(win_size).min()

        # future_df['profit_threshold'] = future_df['profit_mean'] + self.n_profit_stddevs * abs(future_df['profit_std'])
        # future_df['loss_threshold'] = future_df['loss_mean'] - self.n_loss_stddevs * abs(future_df['loss_std'])

        future_df['profit_threshold'] = future_df['dwt_profit_mean'] + self.n_profit_stddevs * abs(
            future_df['dwt_profit_std'])
        future_df['loss_threshold'] = future_df['dwt_loss_mean'] - self.n_loss_stddevs * abs(future_df['dwt_loss_std'])

        future_df['profit_diff'] = (future_df['future_profit'] - future_df['profit_threshold']) * 10.0
        future_df['loss_diff'] = (future_df['future_loss'] - future_df['loss_threshold']) * 10.0

        # future_df['entry_signal'] = np.where(future_df['profit_diff'] > 0.0, 1.0, 0.0)
        # future_df['exit_signal'] = np.where(future_df['loss_diff'] < 0.0, -1.0, 0.0)

        # these explicitly uses dwt
        future_df['future_dwt'] = future_df['full_dwt'].shift(-lookahead)
        # future_df['curr_trend'] = np.where(future_df['full_dwt'].shift(-1) > future_df['full_dwt'], 1.0, -1.0)
        # future_df['future_trend'] = np.where(future_df['future_dwt'].shift(-1) > future_df['future_dwt'], 1.0, -1.0)

        future_df['trend'] = np.where(future_df[price_col] >= future_df[price_col].shift(), 1.0, -1.0)
        future_df['ftrend'] = np.where(future_df['future_close'] >= future_df['future_close'].shift(), 1.0, -1.0)

        future_df['curr_trend'] = np.where(future_df['trend'].rolling(3).sum() > 0.0, 1.0, -1.0)
        future_df['future_trend'] = np.where(future_df['ftrend'].rolling(3).sum() > 0.0, 1.0, -1.0)

        future_df['dwt_dir'] = 0.0
        future_df['dwt_dir'] = np.where(dataframe['dwt'].diff() >= 0, 1, -1)
        future_df['dwt_dir_up'] = np.where(dataframe['dwt'].diff() >= 0, 1, 0)
        future_df['dwt_dir_dn'] = np.where(dataframe['dwt'].diff() < 0, 1, 0)

        # build forward-looking sum of up/down trends
        future_win = pd.api.indexers.FixedForwardWindowIndexer(window_size=int(win_size))  # don't use a big window

        # future_df['future_nseq'] = future_df['curr_trend'].rolling(window=future_win, min_periods=1).sum()

        # future_df['future_nseq_up'] = 0.0
        # future_df['future_nseq_up'] = np.where(
        #     future_df["dwt_dir_up"].eq(1),
        #     future_df.groupby(future_df["dwt_dir_up"].ne(future_df["dwt_dir_up"].shift(1)).cumsum()).cumcount() + 1,
        #     0.0
        # )
        # future_df['future_nseq_up'] = future_df['future_nseq'].clip(lower=0.0)
        future_df['future_nseq_up'] = future_df['dwt_nseq_up'].shift(-win_size)

        future_df['future_nseq_up_mean'] = future_df['future_nseq_up'].rolling(window=future_win).mean()
        future_df['future_nseq_up_std'] = future_df['future_nseq_up'].rolling(window=future_win).std()
        future_df['future_nseq_up_thresh'] = future_df['future_nseq_up_mean'] + self.n_profit_stddevs * future_df[
            'future_nseq_up_std']

        # future_df['future_nseq_dn'] = -np.where(
        #     future_df["dwt_dir_dn"].eq(1),
        #     future_df.groupby(future_df["dwt_dir_dn"].ne(future_df["dwt_dir_dn"].shift(1)).cumsum()).cumcount() + 1,
        #     0.0
        # )
        # print(future_df['future_nseq_dn'])
        # future_df['future_nseq_dn'] = future_df['future_nseq'].clip(upper=0.0)
        future_df['future_nseq_dn'] = future_df['dwt_nseq_dn'].shift(-win_size)

        future_df['future_nseq_dn_mean'] = future_df['future_nseq_dn'].rolling(future_win).mean()
        future_df['future_nseq_dn_std'] = future_df['future_nseq_dn'].rolling(future_win).std()
        future_df['future_nseq_dn_thresh'] = future_df['future_nseq_dn_mean'] \
                                             - self.n_loss_stddevs * future_df['future_nseq_dn_std']

        # Recent min/max
        # future_df['future_min'] = future_df[price_col].rolling(window=future_win).min()
        # future_df['future_max'] = future_df[price_col].rolling(window=future_win).max()
        future_df['future_min'] = future_df['dwt'].rolling(window=future_win).min()
        future_df['future_max'] = future_df['dwt'].rolling(window=future_win).max()

        future_df['future_maxmin'] = 100.0 * (future_df['future_max'] - future_df['future_min']) / \
                                  future_df['future_max']
        future_df['future_delta_min'] = 100.0 * (future_df['future_min'] - future_df['close']) / \
                                  future_df['close']
        future_df['future_delta_max'] = 100.0 * (future_df['future_max'] - future_df['close']) / \
                                  future_df['close']

        future_df['future_maxmin'] = future_df['future_maxmin'].clip(lower=0.0, upper=10.0)

        # get average gain & stddev
        profit_mean = future_df['future_profit'].mean()
        profit_std = future_df['future_profit'].std()
        loss_mean = future_df['future_loss'].mean()
        loss_std = future_df['future_loss'].std()

        # update thresholds
        if self.profit_threshold != profit_mean:
            newval = profit_mean + self.n_profit_stddevs * profit_std
            if self.dbg_verbose:
                print("    Profit threshold {:.4f} -> {:.4f}".format(self.profit_threshold, newval))
            self.profit_threshold = newval

        if self.loss_threshold != loss_mean:
            newval = loss_mean - self.n_loss_stddevs * abs(loss_std)
            if self.dbg_verbose:
                print("    Loss threshold {:.4f} -> {:.4f}".format(self.loss_threshold, newval))
            self.loss_threshold = newval

        return future_df

    ################################

    # creates the entry/exit labels absed on looking ahead into the supplied dataframe
    def create_training_data(self, dataframe: DataFrame):

        future_df = self.add_hidden_indicators(dataframe.copy())
        future_df = self.add_future_data(future_df)

        future_df['train_entry'] = 0.0
        future_df['train_exit'] = 0.0

        # use sequence trends as criteria
        future_df['train_entry'] = self.get_train_entry_signals(future_df)
        future_df['train_exit'] = self.get_train_exit_signals(future_df)

        entries = future_df['train_entry'].copy()
        if entries.sum() < 3:
            print("OOPS! <3 ({:.0f}) entry signals generated. Check training criteria".format(entries.sum()))

        exits = future_df['train_exit'].copy()
        if entries.sum() < 3:
            print("OOPS! <3 ({:.0f}) exit signals generated. Check training criteria".format(exits.sum()))

        self.save_debug_data(future_df)
        self.save_debug_indicators(future_df)

        return entries, exits

    def save_debug_data(self, future_df: DataFrame):

        # Debug support: add commonly used indicators so that they can be viewed
        # the list below is available for any subclass. Subclasses themselves can add more by overriding
        # the func save_debug_indicators()

        dbg_list = [
            'full_dwt', 'train_entry', 'train_exit',
            'future_gain', 'future_min', 'future_max',
            'profit_min', 'profit_max', 'profit_threshold',
            'loss_min', 'loss_max', 'loss_threshold',
        ]

        if len(dbg_list) > 0:
            for indicator in dbg_list:
                self.add_debug_indicator(future_df, indicator)

        return

    # empty func. Meant to be overridden by subclass
    def save_debug_indicators(self, future_df: DataFrame):
        pass
        return

    # adds an indicator to the main frame for debug (e.g. plotting). Column will be prefixed with '%', which will
    # cause it to be removed before normalisation and fitting of models
    def add_debug_indicator(self, future_df: DataFrame, indicator):
        dbg_indicator = '%' + indicator
        if not (dbg_indicator in self.dbg_curr_df):
            self.dbg_curr_df[dbg_indicator] = future_df[indicator]

    ###################

    # returns (rolling) smoothed version of input column
    def roll_smooth(self, col) -> np.float:
        # must return scalar, so just calculate prediction and take last value

        # smooth = gaussian_filter1d(col, 4)
        smooth = gaussian_filter1d(col, 2)

        length = len(smooth)
        if length > 0:
            return smooth[length - 1]
        else:
            print("model:", smooth)
            return 0.0

    def get_dwt(self, col):

        a = np.array(col)

        # de-trend the data
        w_mean = a.mean()
        w_std = a.std()
        a_notrend = (a - w_mean) / w_std
        # a_notrend = a_notrend.clip(min=-3.0, max=3.0)

        # get DWT model of data
        restored_sig = self.dwtModel(a_notrend)

        # re-trend
        model = (restored_sig * w_std) + w_mean

        return model

    def roll_get_dwt(self, col) -> np.float:
        # must return scalar, so just calculate prediction and take last value

        model = self.get_dwt(col)

        length = len(model)
        if length > 0:
            return model[length - 1]
        else:
            # cannot calculate DWT (e.g. at startup), just return original value
            return col[len(col) - 1]

    def dwtModel(self, data):

        # the choice of wavelet makes a big difference
        # for an overview, check out: https://www.kaggle.com/theoviel/denoising-with-direct-wavelet-transform
        # wavelet = 'db1'
        wavelet = 'db8'
        # wavelet = 'bior1.1'
        # wavelet = 'haar'  # deals well with harsh transitions
        level = 1
        wmode = "smooth"
        tmode = "hard"
        length = len(data)

        # Apply DWT transform
        coeff = pywt.wavedec(data, wavelet, mode=wmode)

        # remove higher harmonics
        std = np.std(coeff[level])
        sigma = (1 / 0.6745) * self.madev(coeff[-level])
        # sigma = self.madev(coeff[-level])
        uthresh = sigma * np.sqrt(2 * np.log(length))

        coeff[1:] = (pywt.threshold(i, value=uthresh, mode=tmode) for i in coeff[1:])

        # inverse DWT transform
        model = pywt.waverec(coeff, wavelet, mode=wmode)

        # there is a known bug in waverec where odd numbered lengths result in an extra item at the end
        diff = len(model) - len(data)
        return model[0:len(model) - diff]
        # return model[diff:]

    def madev(self, d, axis=None):
        """ Mean absolute deviation of a signal """
        return np.mean(np.absolute(d - np.mean(d, axis)), axis)

    # add indicators used by stoploss/custom sell logic
    def add_stoploss_indicators(self, dataframe, pair) -> DataFrame:
        if not pair in self.custom_trade_info:
            self.custom_trade_info[pair] = {}
            if not 'had_trend' in self.custom_trade_info[pair]:
                self.custom_trade_info[pair]['had_trend'] = False

    # get a scaler for scaling/normalising the data (in a func because I change it routinely)
    def get_scaler(self):
        # uncomment the one yu want
        # return StandardScaler()
        # return RobustScaler()
        return MinMaxScaler()

    def norm_column(self, col):
        return self.zscore_column(col)

    # normalises a column. Data is in units of 1 stddev, i.e. a value of 1.0 represents 1 stdev above mean
    def zscore_column(self, col):
        return (col - col.mean()) / col.std()

    # applies MinMax scaling to a column. Returns all data in range [0,1]
    def minmax_column(self, col):
        result = col
        # print(col)

        if (col.dtype == 'str') or (col.dtype == 'object'):
            result = 0.0
        else:
            result = col
            cmax = max(col)
            cmin = min(col)
            denom = float(cmax - cmin)
            if denom == 0.0:
                result = 0.0
            else:
                result = (col - col.min()) / denom

        return result

    def check_inf(self, dataframe):
        col_name = dataframe.columns.to_series()[np.isinf(dataframe).any()]
        if len(col_name) > 0:
            print("***")
            print("*** Infinity in cols: ", col_name)
            print("***")

    def remove_debug_columns(self, dataframe: DataFrame) -> DataFrame:
        drop_list = dataframe.filter(regex='^%').columns
        if len(drop_list) > 0:
            for col in drop_list:
                dataframe = dataframe.drop(col, axis=1)
            dataframe.reindex()
        return dataframe

    # Normalise a dataframe
    def norm_dataframe(self, dataframe: DataFrame) -> DataFrame:
        self.check_inf(dataframe)

        df = dataframe.copy()
        if 'date' in df.columns:
            dates = pd.to_datetime(df['date'], utc=True)
            # dates = pd.to_datetime(df['date'])
            start_date = datetime(2020, 1, 1).astimezone(timezone.utc)
            df['date'] = dates.astype('int64')
            df['days_from_start'] = (dates - start_date).dt.days
            df['day_of_week'] = dates.dt.dayofweek
            df['day_of_month'] = dates.dt.day
            df['week_of_year'] = dates.dt.isocalendar().week
            df['month'] = dates.dt.month
            # df['year'] = dates.dt.year

        df = self.remove_debug_columns(df)

        df.set_index('date')
        df.reindex()

        cols = df.columns
        self.scaler = self.get_scaler()

        df = pd.DataFrame(self.scaler.fit_transform(df), columns=cols)

        return df

    # Normalise a dataframe using Z-Score normalisation (mean=0, stddev=1)
    def zscore_dataframe(self, dataframe: DataFrame) -> DataFrame:
        self.check_inf(dataframe)
        return ((dataframe - dataframe.mean()) / dataframe.std())

    # Scale a dataframe using sklearn builtin scaler
    def scale_dataframe(self, dataframe: DataFrame) -> DataFrame:
        self.check_inf(dataframe)

        temp = dataframe.copy()
        if 'date' in temp.columns:
            temp['date'] = pd.to_datetime(temp['date']).astype('int64')

        temp = self.remove_debug_columns(temp)

        temp.reindex()

        scaler = RobustScaler()
        scaler = scaler.fit(temp)
        temp = scaler.transform(temp)
        return temp

    # remove outliers from normalised dataframe
    def remove_outliers(self, df_norm: DataFrame, entries, exits):

        # for col in df_norm.columns.values:
        #     if col != 'date':
        #         df_norm = df_norm[(df_norm[col] <= 3.0)]
        # return df_norm
        df = df_norm.copy()
        df['%temp_entry'] = entries.copy()
        df['%temp_exit'] = exits.copy()
        #
        df2 = df[((df >= -3.0) & (df <= 3.0)).all(axis=1)]
        # df_out = df[~((df >= -3.0) & (df <= 3.0)).all(axis=1)] # for debug
        ndrop = df_norm.shape[0] - df2.shape[0]
        if ndrop > 0:
            b = df2['%temp_entry'].copy()
            s = df2['%temp_exit'].copy()
            df2.drop('%temp_entry', axis=1, inplace=True)
            df2.drop('%temp_exit', axis=1, inplace=True)
            df2.reindex()
            # if self.dbg_verbose:
            print("    Removed ", ndrop, " outliers")
            # print(" df2:", df2)
            # print(" df_out:", df_out)
            # print ("df_norm:", df_norm.shape, "df2:", df2.shape, "df_out:", df_out.shape)
        else:
            # no outliers, just return originals
            df2 = df_norm
            b = entries
            s = exits
        return df2, b, s

    # build a 'viable' dataframe sample set. Needed because the positive labels are sparse
    def build_viable_dataset(self, size: int, df_norm: DataFrame, entries, exits):
        # if self.dbg_verbose:
        #     print("     df_norm:{} size:{} entries:{} exits:{}".format(df_norm.shape, size, entries.shape[0], exits.shape[0]))

        # copy and combine the data into one dataframe
        df = df_norm.copy()
        df['%temp_entry'] = entries.copy()
        df['%temp_exit'] = exits.copy()

        # df_entry = df[( (df['%temp_entry'] > 0) ).all(axis=1)]
        # df_exit = df[((df['%temp_exit'] > 0)).all(axis=1)]
        # df_nosig = df[((df['%temp_entry'] == 0) & (df['%temp_exit'] == 0)).all(axis=1)]

        df_entry = df.loc[df['%temp_entry'] == 1]
        df_exit = df.loc[df['%temp_exit'] == 1]
        df_nosig = df.loc[(df['%temp_entry'] == 0) & (df['%temp_exit'] == 0)]

        # make sure there aren't too many entries & exits
        # We are aiming for a roughly even split between entries, exits, and 'no signal' (no entry or exit)
        max_signals = int(2 * size / 3)
        entry_train_size = df_entry.shape[0]
        exit_train_size = df_exit.shape[0]

        if max_signals > df_nosig.shape[0]:
            max_signals = int((size - df_nosig.shape[0])) - 1

        if ((df_entry.shape[0] + df_exit.shape[0]) > max_signals):
            # both exceed max?
            sig_size = int(max_signals / 2)
            # if self.dbg_verbose:
            #     print("     sig_size:{} max_signals:{} entries:{} exits:{}".format(sig_size, max_signals, df_entry.shape[0],
            #                                                                     df_exit.shape[0]))

            if (df_entry.shape[0] > sig_size) & (df_exit.shape[0] > sig_size):
                # resize both entry & exit to 1/3 of requested size
                entry_train_size = sig_size
                exit_train_size = sig_size
            else:
                # only one them is too big, so figure out which
                if (df_entry.shape[0] > df_exit.shape[0]):
                    entry_train_size = max_signals - df_exit.shape[0]
                else:
                    exit_train_size = max_signals - df_entry.shape[0]

            # if self.dbg_verbose:
            #     print("     entry_train_size:{} exit_train_size:{}".format(entry_train_size, exit_train_size))

        if entry_train_size < df_entry.shape[0]:
            df_entry, _ = train_test_split(df_entry, train_size=entry_train_size, shuffle=True)
        if exit_train_size < df_exit.shape[0]:
            df_exit, _ = train_test_split(df_exit, train_size=exit_train_size, shuffle=True)

        # extract enough rows to fill the requested size
        fill_size = size - entry_train_size - exit_train_size - 1
        # if self.dbg_verbose:
        #     print("     df_nosig:{} fill_size:{}".format(df_nosig.shape, fill_size))

        if fill_size < df_nosig.shape[0]:
            df_nosig, _ = train_test_split(df_nosig, train_size=fill_size, shuffle=True)

        # print("viable df - entries:{} exits:{} fill:{}".format(df_entry.shape[0], df_exit.shape[0], df_nosig.shape[0]))

        # concatenate the dataframes
        frames = [df_entry, df_exit, df_nosig]
        df2 = pd.concat(frames)

        # shuffle rows
        df2 = df2.sample(frac=1)

        # separate out the data, entries & exits
        b = df2['%temp_entry'].copy()
        s = df2['%temp_exit'].copy()
        df2.drop('%temp_entry', axis=1, inplace=True)
        df2.drop('%temp_exit', axis=1, inplace=True)
        df2.reindex()

        if self.dbg_verbose:
            print("     df2:", df2.shape, " b:", b.shape, " s:", s.shape)

        return df2, b, s

    # map column into [0,1]
    def get_binary_labels(self, col):
        binary_encoder = LabelEncoder().fit([min(col), max(col)])
        result = binary_encoder.transform(col)
        # print ("label input:  ", col)
        # print ("label output: ", result)
        return result

    # train the PCA reduction and classification models

    def train_models(self, curr_pair, dataframe: DataFrame, entries, exits):

        # only run if interval reaches 0 (no point retraining every camdle)
        count = self.pair_model_info[curr_pair]['interval']
        if (count > 0):
            self.pair_model_info[curr_pair]['interval'] = count - 1
            return
        else:
            # reset interval to a random number between 1 and the amount of lookahead
            # self.pair_model_info[curr_pair]['interval'] = random.randint(1, self.curr_lookahead)
            self.pair_model_info[curr_pair]['interval'] = random.randint(2, max(32, self.curr_lookahead))

        # Reset models for this pair. Makes it safe to just return on error
        self.pair_model_info[curr_pair]['pca_size'] = 0
        self.pair_model_info[curr_pair]['pca'] = None
        self.pair_model_info[curr_pair]['clf_entry_name'] = ""
        self.pair_model_info[curr_pair]['clf_entry'] = None
        self.pair_model_info[curr_pair]['clf_exit_name'] = ""
        self.pair_model_info[curr_pair]['clf_exit'] = None

        # check input - need at least 2 samples or classifiers will not train
        if entries.sum() < 2:
            print("*** ERR: insufficient entries in expected results. Check training data")
            # print(entries)
            return

        if exits.sum() < 2:
            print("*** ERR: insufficient exits in expected results. Check training data")
            return

        rand_st = 27  # use fixed number for reproducibility

        remove_outliers = False
        if remove_outliers:
            # norm dataframe before splitting, otherwise variances are skewed
            full_df_norm = self.norm_dataframe(dataframe)
            full_df_norm, entries, exits = self.remove_outliers(full_df_norm, entries, exits)
        else:
            full_df_norm = self.norm_dataframe(dataframe).clip(lower=-3.0, upper=3.0)  # supress outliers

        # constrain size to what will be available in run modes
        data_size = int(min(975, full_df_norm.shape[0]))

        # get 'viable' data set (includes all entries/exits)
        v_df_norm, v_entries, v_exits = self.build_viable_dataset(data_size, full_df_norm, entries, exits)

        train_size = int(0.8 * data_size)
        test_size = data_size - train_size

        df_train, df_test, train_entries, test_entries, train_exits, test_exits, = train_test_split(v_df_norm,
                                                                                              v_entries,
                                                                                              v_exits,
                                                                                              train_size=train_size,
                                                                                              random_state=rand_st,
                                                                                              shuffle=True)
        if self.dbg_verbose:
            print("     dataframe:", v_df_norm.shape, ' -> train:', df_train.shape, " + test:", df_test.shape)
            print("     entries:", entries.shape, ' -> train:', train_entries.shape, " + test:", test_entries.shape)
            print("     exits:", exits.shape, ' -> train:', train_exits.shape, " + test:", test_exits.shape)

        print("    #training samples:", len(df_train), " #entries:", int(train_entries.sum()), ' #exit:', 
              int(train_exits.sum()))

        #TODO: if low number of entries/exits, try k-fold sampling

        entry_labels = self.get_binary_labels(entries)
        exit_labels = self.get_binary_labels(exits)
        train_entry_labels = self.get_binary_labels(train_entries)
        train_exit_labels = self.get_binary_labels(train_exits)
        test_entry_labels = self.get_binary_labels(test_entries)
        test_exit_labels = self.get_binary_labels(test_exits)

        # create the PCA analysis model

        pca = self.get_pca(df_train)

        df_train_pca = DataFrame(pca.transform(df_train))

        # DEBUG:
        # print("")
        print("   ", curr_pair, " - input: ", df_train.shape, " -> pca: ", df_train_pca.shape)

        if df_train_pca.shape[1] <= 1:
            print("***")
            print("** ERR: PCA reduced to 1. Must be training data still in dataframe!")
            print("df_train columns: ", df_train.columns.values)
            print("df_train_pca columns: ", df_train_pca.columns.values)
            print("***")
            return

        # Create entry/exit classifiers for the model

        # check that we have enough positives to train
        entry_ratio = 100.0 * (train_entries.sum() / len(train_entries))
        if (entry_ratio < 0.5):
            print("*** ERR: insufficient number of positive entry labels ({:.2f}%)".format(entry_ratio))
            return

        entry_clf, entry_clf_name = self.get_entry_classifier(df_train_pca, train_entry_labels)

        exit_ratio = 100.0 * (train_exits.sum() / len(train_exits))
        if (exit_ratio < 0.5):
            print("*** ERR: insufficient number of positive exit labels ({:.2f}%)".format(exit_ratio))
            return

        exit_clf, exit_clf_name = self.get_exit_classifier(df_train_pca, train_exit_labels)

        # save the models

        self.pair_model_info[curr_pair]['pca'] = pca
        self.pair_model_info[curr_pair]['pca_size'] = df_train_pca.shape[1]
        self.pair_model_info[curr_pair]['clf_entry_name'] = entry_clf_name
        self.pair_model_info[curr_pair]['clf_entry'] = entry_clf
        self.pair_model_info[curr_pair]['clf_exit_name'] = exit_clf_name
        self.pair_model_info[curr_pair]['clf_exit'] = exit_clf

        # if scan specified, test against the test dataframe
        if self.dbg_test_classifier and self.dbg_verbose:

            df_test_pca = DataFrame(pca.transform(df_test))
            if not (entry_clf is None):
                pred_entries = entry_clf.predict(df_test_pca)
                print("")
                print("Predict - entry Signals (", type(entry_clf).__name__, ")")
                print(classification_report(test_entry_labels, pred_entries))
                print("")

            if not (exit_clf is None):
                pred_exits = exit_clf.predict(df_test_pca)
                print("")
                print("Predict - exit Signals (", type(exit_clf).__name__, ")")
                print(classification_report(test_exit_labels, pred_exits))
                print("")

    autoencoder = None

    # get the PCA model for the supplied dataframe (dataframe must be normalised)
    def get_pca(self, df_norm: DataFrame):

        ncols = df_norm.shape[1]  # allow all components to get the full variance matrix
        whiten = True

        pca_type = 0

        # there are various types of PCA, plus alternatives like ICA and Feature Extraction
        if pca_type == 0:
            pca = skd.PCA(n_components=ncols, whiten=whiten, svd_solver='full').fit(df_norm)
            var_ratios = pca.explained_variance_ratio_

            # if self.dbg_verbose:
            #     print ("PCA variance_ratio: ", pca.explained_variance_ratio_)

            # scan variance and only take if column contributes >x%
            ncols = 0
            var_sum = 0.0
            variance_threshold = 0.999
            # variance_threshold = 0.99
            while ((var_sum < variance_threshold) & (ncols < len(var_ratios))):
                var_sum = var_sum + var_ratios[ncols]
                ncols = ncols + 1

            # if necessary, re-calculate pca with reduced column set
            if (ncols != df_norm.shape[1]):
                # pca = skd.PCA(n_components=ncols, svd_solver="randomized", whiten=True).fit(df_norm)
                pca = skd.PCA(n_components=ncols, whiten=whiten, svd_solver='full').fit(df_norm)

            self.check_pca(pca, df_norm)

            if self.dbg_analyse_pca and self.dbg_verbose:
                self.analyse_pca(pca, df_norm)

        elif pca_type == 1:
            # accurate, but slow
            print("    Using KernelPCA...")
            pca = skd.KernelPCA(n_components=ncols, remove_zero_eig=True).fit(df_norm)
            eigenvalues = pca.eigenvalues_
            # print(var_ratios)
            # Note: eigenvalues are not bounded, so just have to go by min value
            ncols = 0
            val_threshold = 0.5
            while ((eigenvalues[ncols] > val_threshold) & (ncols < len(eigenvalues))):
                ncols = ncols + 1

            # if necessary, re-calculate pca with reduced column set
            if (ncols != df_norm.shape[1]):
                # pca = skd.PCA(n_components=ncols, svd_solver="randomized", whiten=True).fit(df_norm)
                pca = skd.KernelPCA(n_components=ncols, remove_zero_eig=True).fit(df_norm)

        elif pca_type == 2:
            print("    Using FactorAnalysis...")
            # Note: this is SUPER slow, do not recommend using it :-(
            # it is useful to run this once, and check to see which indicators are useful or not
            pca = skd.FactorAnalysis(n_components=ncols).fit(df_norm)
            var_ratios = pca.noise_variance_
            # print(var_ratios)

            # if self.dbg_verbose:
            #     print ("PCA variance_ratio: ", pca.explained_variance_ratio_)

            # scan variance and only take if column contributes >x%
            ncols = 0
            variance_threshold = 0.09
            col_names = df_norm.columns
            for i in range(len(var_ratios)):
                if var_ratios[i] > variance_threshold:
                    c = '#' if (var_ratios[i] > 0.5) else '+'
                    print("    {} ({:.3f}) {:<24}".format(c, var_ratios[i], col_names[i]))
                    ncols = ncols + 1
                else:
                    c = '!' if (var_ratios[i] < 0.05) else '-'
                    print("                                {} ({:.3f}) {:<20}".format(c, var_ratios[i], col_names[i]))

            # if necessary, re-calculate pca with reduced column set
            if (ncols != df_norm.shape[1]):
                # pca = skd.PCA(n_components=ncols, svd_solver="randomized", whiten=True).fit(df_norm)
                pca = skd.FactorAnalysis(n_components=ncols).fit(df_norm)

        elif pca_type == 3:
            # fast, but not as accurate
            print("    Using Locally Linear Embedding...")
            pca = LocallyLinearEmbedding(n_components=4, eigen_solver='dense', method="modified").fit(df_norm)

        elif pca_type == 4:
            # a bit slow, still debugging...
            print("    Using Autoencoder...")
            # pca = LocallyLinearEmbedding(n_components=4, eigen_solver='dense', method="modified").fit(df_norm)
            if self.autoencoder is None:
                # self.autoencoder = AutoEncoder(df_norm.shape[1])
                self.autoencoder = CompressionAutoEncoder(df_norm.shape[1], tag="Buy")
            pca = self.autoencoder

        elif pca_type == 5:
            # Restricted Boltzmann Machine
            print("    Using Restricted Boltzmann Machine...")
            pca = RBMEncoder()


        else:
            print("*** ERR - unknown PCA type ***")
            pca = None

        return pca

    # does a quick for suspicious values. Separate func because we always want to call this
    def check_pca(self, pca, df):

        ratios = pca.explained_variance_ratio_
        loadings = pd.DataFrame(pca.components_.T, index=df.columns.values)

        # check variance ratios
        var_big = np.where(ratios >= 0.5)[0]
        if len(var_big) > 0:
            # print("    !!! high variance in columns: ", var_big)
            print("    !!! high variance in columns: ", df.columns.values[var_big])
            # print("    !!! variances: ", ratios)

        var_0 = np.where(ratios == 0)[0]
        if len(var_0) > 0:
            print("    !!! zero variance in columns: ", var_0)

        # check PCA rows
        inf_rows = loadings[(np.isinf(loadings)).any(axis=1)].index.values.tolist()

        if len(inf_rows) > 0:
            print("    !!! inf values in rows: ", inf_rows)

        na_rows = loadings[loadings.isna().any(axis=1)].index.values.tolist()
        if len(na_rows) > 0:
            print("    !!! na values in rows: ", na_rows)

        zero_rows = loadings[(loadings == 0).any(axis=1)].index.values.tolist()
        if len(zero_rows) > 0:
            print("    !!! No contribution from indicator(s) - remove ?! : ", zero_rows)

        return

    def analyse_pca(self, pca, df):
        print("")
        print("Variance Ratios:")
        ratios = pca.explained_variance_ratio_
        print(ratios)
        print("")

        # print matrix of weightings for selected components
        loadings = pd.DataFrame(pca.components_.T, index=df.columns.values)

        l2 = loadings.abs()
        l3 = loadings.mul(ratios)
        ranks = loadings.rank()

        loadings['Score'] = l2.sum(axis=1)
        loadings['Score0'] = loadings[loadings.columns.values[0]].abs()
        loadings['Rank'] = loadings['Score'].rank(ascending=False)
        loadings['Rank0'] = loadings['Score0'].rank(ascending=False)
        print("Loadings, by PC0:")
        print(loadings.sort_values('Rank0').head(n=30))
        print("")
        # print("Loadings, by All Columns:")
        # print(loadings.sort_values('Rank').head(n=30))
        # print("")

        # weighted by variance ratios
        l3a = l3.abs()
        l3['Score'] = l3a.sum(axis=1)
        l3['Rank'] = loadings['Score'].rank(ascending=False)
        print("Loadings, Weighted by Variance Ratio")
        print(l3.sort_values('Rank').head(n=20))

        # # rankings per column
        ranks['Score'] = ranks.sum(axis=1)
        ranks['Rank'] = ranks['Score'].rank(ascending=True)
        print("Rankings per column")
        print(ranks.sort_values('Rank', ascending=True).head(n=30))

        # print(loadings.head())
        # print(l3.head())

    # get a classifier for the supplied dataframe (normalised) and known results
    def get_entry_classifier(self, df_norm: DataFrame, results):

        clf = None
        name = ""
        labels = self.get_binary_labels(results)

        if results.sum() <= 2:
            print("***")
            print("*** ERR: insufficient positive results in entry data")
            print("***")
            return clf, name

        # If already done, just get previous result and re-fit
        if self.pair_model_info[self.curr_pair]['clf_entry']:
            clf = self.pair_model_info[self.curr_pair]['clf_entry']
            clf = clf.fit(df_norm, labels)
            name = self.pair_model_info[self.curr_pair]['clf_entry_name']
        else:
            if self.dbg_scan_classifiers:
                if self.dbg_verbose:
                    print("    Finding best entry classifier:")
                clf, name = self.find_best_classifier(df_norm, labels, tag="entry")
            else:
                clf, name = self.classifier_factory(self.default_classifier, df_norm, labels)
                clf = clf.fit(df_norm, labels)

        return clf, name

    # get a classifier for the supplied dataframe (normalised) and known results
    def get_exit_classifier(self, df_norm: DataFrame, results):

        clf = None
        name = ""
        labels = self.get_binary_labels(results)

        if results.sum() <= 2:
            print("***")
            print("*** ERR: insufficient positive results in exit data")
            print("***")
            return clf, name

        # If already done, just get previous result and re-fit
        if self.pair_model_info[self.curr_pair]['clf_exit']:
            clf = self.pair_model_info[self.curr_pair]['clf_exit']
            clf = clf.fit(df_norm, labels)
            name = self.pair_model_info[self.curr_pair]['clf_exit_name']
        else:
            if self.dbg_scan_classifiers:
                if self.dbg_verbose:
                    print("    Finding best exit classifier:")
                clf, name = self.find_best_classifier(df_norm, labels, tag="exit")
            else:
                clf, name = self.classifier_factory(self.default_classifier, df_norm, labels)
                clf = clf.fit(df_norm, labels)

        return clf, name

    # default classifier
    default_classifier = 'LDA'  # select based on testing

    # list of potential classifier types - set to the list that you want to compare
    classifier_list = [
        'LogisticRegression', 'GaussianNB', 'SGD',
        'GradientBoosting', 'AdaBoost', 'linearSVC', 'sigmoidSVC',
        'LDA', 'XGBoost'
    ]

    # factory to create classifier based on name
    def classifier_factory(self, name, data, labels):
        clf = None

        if name == 'LogisticRegression':
            clf = LogisticRegression(max_iter=10000)
        elif name == 'DecisionTree':
            clf = DecisionTreeClassifier()
        elif name == 'RandomForest':
            clf = RandomForestClassifier()
        elif name == 'GaussianNB':
            clf = GaussianNB()
        elif name == 'MLP':
            param_grid = {
                'hidden_layer_sizes': [(30, 2), (30, 80, 2), (30, 60, 30, 2)],
                'max_iter': [50, 100, 150],
                'activation': ['tanh', 'relu'],
                'solver': ['sgd', 'adam', 'lbfgs'],
                'alpha': [0.0001, 0.05],
                'learning_rate': ['constant', 'adaptive'],
            }
            clf = MLPClassifier(hidden_layer_sizes=(64, 32, 1),
                                max_iter=50,
                                activation='relu',
                                learning_rate='adaptive',
                                alpha=1e-5,
                                solver='lbfgs',
                                verbose=0)


        elif name == 'KNeighbors':
            clf = KNeighborsClassifier(n_neighbors=3)
        elif name == 'SGD':
            clf = SGDClassifier()
        elif name == 'GradientBoosting':
            clf = GradientBoostingClassifier()
        elif name == 'AdaBoost':
            clf = AdaBoostClassifier()
        elif name == 'QDA':
            clf = QuadraticDiscriminantAnalysis()
        elif name == 'linearSVC':
            clf = LinearSVC(dual=False)
        elif name == 'gaussianSVC':
            clf = SVC(kernel='rbf')
        elif name == 'polySVC':
            clf = SVC(kernel='poly')
        elif name == 'sigmoidSVC':
            clf = SVC(kernel='sigmoid')
        elif name == 'Voting':
            # choose 4 decent classifiers
            c1, _ = self.classifier_factory('AdaBoost', data, labels)
            c2, _ = self.classifier_factory('GaussianNB', data, labels)
            c3, _ = self.classifier_factory('LDA', data, labels)
            c4, _ = self.classifier_factory('sigmoidSVC', data, labels)
            clf = VotingClassifier(estimators=[('c1', c1), ('c2', c2), ('c3', c3), ('c4', c4)], voting='hard')
        elif name == 'LDA':
            clf = LinearDiscriminantAnalysis()
        elif name == 'QDA':
            clf = QuadraticDiscriminantAnalysis()
        elif name == 'XGBoost':
            clf = XGBClassifier()

        else:
            print("Unknown classifier: ", name)
            clf = None
        return clf, name

    # tries different types of classifiers and returns the best one
    # tag parameter identifies where to save performance stats (default is not to save)
    def find_best_classifier(self, df, results, tag=""):

        if self.dbg_verbose:
            print("      Evaluating classifiers...")

        # Define dictionary with CLF and performance metrics
        scoring = {'accuracy': make_scorer(accuracy_score),
                   'precision': make_scorer(precision_score),
                   'recall': make_scorer(recall_score),
                   'f1_score': make_scorer(f1_score)}

        folds = 5
        clf_dict = {}
        models_scores_table = pd.DataFrame(index=['Accuracy', 'Precision', 'Recall', 'F1'])

        best_score = -0.1
        best_classifier = ""

        labels = self.get_binary_labels(results)

        # split into test/train for evaluation, then re-fit once selected
        # df_train, df_test, res_train, res_test = train_test_split(df, results, train_size=0.5)
        df_train, df_test, res_train, res_test = train_test_split(df, labels, train_size=0.8,
                                                                  random_state=27, shuffle=True)
        # print("df_train:",  df_train.shape, " df_test:", df_test.shape,
        #       "res_train:", res_train.shape, "res_test:", res_test.shape)

        # check there are enough training samples
        # TODO: if low train/test samples, use k-fold sampling nstead
        if res_train.sum() < 2:
            print("    Insufficient +ve (train) results to fit: ", res_train.sum())
            return None, ""

        if res_test.sum() < 2:
            print("    Insufficient +ve (test) results: ", res_test.sum())
            return None, ""

        for cname in self.classifier_list:
            clf, _ = self.classifier_factory(cname, df_train, res_train)

            if clf is not None:

                # fit to the training data
                clf_dict[cname] = clf
                clf = clf.fit(df_train, res_train)

                # assess using the test data. Do *not* use the training data for testing
                pred_test = clf.predict(df_test)
                # score = f1_score(results, prediction, average=None)[1]
                score = f1_score(res_test, pred_test, average='macro')

                if self.dbg_verbose:
                    print("      {0:<20}: {1:.3f}".format(cname, score))

                if score > best_score:
                    best_score = score
                    best_classifier = cname

                # update classifier stats
                if tag:
                    if not (tag in self.classifier_stats):
                        self.classifier_stats[tag] = {}

                    if not (cname in self.classifier_stats[tag]):
                        self.classifier_stats[tag][cname] = {'count': 0, 'score': 0.0, 'selected': 0}

                    curr_count = self.classifier_stats[tag][cname]['count']
                    curr_score = self.classifier_stats[tag][cname]['score']
                    self.classifier_stats[tag][cname]['count'] = curr_count + 1
                    self.classifier_stats[tag][cname]['score'] = (curr_score * curr_count + score) / (curr_count + 1)

        if best_score <= 0.0:
            print("   No classifier found")
            return None, ""

        clf = clf_dict[best_classifier]

        # print("")
        if best_score < self.min_f1_score:
            print("!!!")
            print("!!! WARNING: F1 score below threshold ({:.3f})".format(best_score))
            print("!!!")
            return None, ""

        # update stats for selected classifier
        if tag:
            if best_classifier in self.classifier_stats[tag]:
                self.classifier_stats[tag][best_classifier]['selected'] = self.classifier_stats[tag][best_classifier] \
                                                                              ['selected'] + 1

        print("       ", tag, " model selected: ", best_classifier, " Score:{:.3f}".format(best_score))
        # print("")

        return clf, best_classifier

    # make predictions for supplied dataframe (returns column)
    def predict(self, dataframe: DataFrame, pair, clf):

        # predict = 0
        predict = None

        pca = self.pair_model_info[pair]['pca']

        if clf:
            # print("    predicting... - dataframe:", dataframe.shape)
            df_norm = self.norm_dataframe(dataframe)
            df_norm_pca = pca.transform(df_norm)
            predict = clf.predict(df_norm_pca)

        else:
            print("Null CLF for pair: ", pair)

        # print (predict)
        return predict

    def predict_entry(self, df: DataFrame, pair):
        clf = self.pair_model_info[pair]['clf_entry']

        if clf is None:
            print("    No entry Classifier for pair ", pair, " -Skipping predictions")
            self.pair_model_info[pair]['interval'] = min(self.pair_model_info[pair]['interval'], 4)
            predict = df['close'].copy()  # just to get the size
            predict = 0.0
            return predict

        print("    predicting entries...")
        predict = self.predict(df, pair, clf)

        # if self.dbg_test_classifier:
        #     # DEBUG: check accuracy
        #     signals = df['train_entry_signal']
        #     labels = self.get_binary_labels(signals)
        #
        #     if  self.dbg_verbose:
        #         print("")
        #         print("Predict - entry Signals (", type(clf).__name__, ")")
        #         print(classification_report(labels, predict))
        #         print("")
        #
        #     score = f1_score(labels, predict, average='macro')
        #     if score <= 0.5:
        #         print("")
        #         print("!!! WARNING: (entry) F1 score below 51% ({:.3f})".format(score))
        #         print("    Classifier:", type(clf).__name__)
        #         print("")

        return predict

    def predict_exit(self, df: DataFrame, pair):
        clf = self.pair_model_info[pair]['clf_exit']
        if clf is None:
            print("    No exit Classifier for pair ", pair, " -Skipping predictions")
            self.pair_model_info[pair]['interval'] = min(self.pair_model_info[pair]['interval'], 4)
            predict = df['close']  # just to get the size
            predict = 0.0
            return predict

        print("    predicting exits...")
        predict = self.predict(df, pair, clf)

        # if self.dbg_test_classifier:
        #     # DEBUG: check accuracy
        #     signals = df['train_exit_signal']
        #     labels = self.get_binary_labels(signals)
        #
        #     if self.dbg_verbose:
        #         print("")
        #         print("Predict - exit Signals (", type(clf).__name__, ")")
        #         print(classification_report(labels, predict))
        #         print("")
        #
        #     score = f1_score(labels, predict, average='macro')
        #     if score <= 0.5:
        #         print("")
        #         print("!!! WARNING: (entry) F1 score below 51% ({:.3f})".format(score))
        #         print("    Classifier:", type(clf).__name__)
        #         print("")

        return predict

    ###################################
    # Debug stuff

    curr_state = {}

    def set_state(self, pair, state: State):
        # if self.dbg_verbose:
        #     if pair in self.curr_state:
        #         print("  ", pair, ": ", self.curr_state[pair], " -> ", state)
        #     else:
        #         print("  ", pair, ": ", " -> ", state)

        self.curr_state[pair] = state

    def get_state(self, pair) -> State:
        return self.curr_state[pair]

    def show_debug_info(self, pair):
        # print("")
        # print("pair_model_info:")
        print("  ", pair, ": ", self.pair_model_info[pair])
        # print("")

    def show_all_debug_info(self):
        print("")
        if (len(self.pair_model_info) > 0):
            # print("Model Info:")
            # print("----------")
            table = PrettyTable(["Pair", "PCA Size", "entry Classifier", "exit Classifier"])
            table.title = "Model Information"
            table.align = "l"
            table.align["PCA Size"] = "c"
            table.reversesort = False
            table.sortby = 'Pair'

            for pair in self.pair_model_info:
                table.add_row([pair,
                               self.pair_model_info[pair]['pca_size'],
                               self.pair_model_info[pair]['clf_entry_name'],
                               self.pair_model_info[pair]['clf_exit_name']
                               ])

            print(table)

        if len(self.classifier_stats) > 0:
            # print("Classifier Statistics:")
            # print("---------------------")
            print("")
            if 'entry' in self.classifier_stats:
                print("")
                table = PrettyTable(["Classifier", "Mean Score", "Selected"])
                table.title = "entry Classifiers"
                table.align["Classifier"] = "l"
                table.align["Mean Score"] = "c"
                table.float_format = '.4'
                for cls in self.classifier_stats['entry']:
                    table.add_row([cls,
                                   self.classifier_stats['entry'][cls]['score'],
                                   self.classifier_stats['entry'][cls]['selected']])
                table.reversesort = True
                # table.sortby = 'Mean Score'
                print(table.get_string(sort_key=operator.itemgetter(2, 1), sortby="Selected"))
                print("")

            if 'exit' in self.classifier_stats:
                print("")
                table = PrettyTable(["Classifier", "Mean Score", "Selected"])
                table.title = "exit Classifiers"
                table.align["Classifier"] = "l"
                table.align["Mean Score"] = "c"
                table.float_format = '.4'
                for cls in self.classifier_stats['exit']:
                    table.add_row([cls,
                                   self.classifier_stats['exit'][cls]['score'],
                                   self.classifier_stats['exit'][cls]['selected']])
                table.reversesort = True
                # table.sortby = 'Mean Score'
                print(table.get_string(sort_key=operator.itemgetter(2, 1), sortby="Selected"))
                print("")

            print("")

    ###################################

    """
    entry Signal
    """

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = []
        dataframe.loc[:, 'entry_tag'] = ''
        curr_pair = metadata['pair']

        self.set_state(curr_pair, self.State.RUNNING)

        if not self.dp.runmode.value in ('hyperopt'):
            if PCA.first_run:
                PCA.first_run = False  # note use of clas variable, not instance variable
                # self.show_debug_info(curr_pair)
                self.show_all_debug_info()

        # add some fairly loose guards, to help prevent 'bad' predictions

        # # ATR in entry range
        # conditions.append(dataframe['atr_signal'] > 0.0)

        # some trading volume
        conditions.append(dataframe['volume'] > 0)

        # MFI
        conditions.append(dataframe['mfi'] < 30.0)

        # below TEMA
        conditions.append(dataframe['close'] < dataframe['tema'])

        # PCA/Classifier triggers
        pca_cond = (
            (qtpylib.crossed_above(dataframe['predict_entry'], 0.5))
        )
        conditions.append(pca_cond)

        # add strategy-specific conditions (from subclass)
        strat_cond = self.get_strategy_buy_conditions(dataframe)
        if strat_cond is not None:
            conditions.append(strat_cond)

        # set entry tags
        dataframe.loc[pca_cond, 'entry_tag'] += 'pca_entry '

        if conditions:
            dataframe.loc[reduce(lambda x, y: x & y, conditions), 'entry'] = 1
        else:
            dataframe['entry'] = 0

        return dataframe

    ###################################

    """
    Exit Signal
    """

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = []
        dataframe.loc[:, 'exit_tag'] = ''
        curr_pair = metadata['pair']

        self.set_state(curr_pair, self.State.RUNNING)

        if not self.dp.runmode.value in ('hyperopt'):
            if PCA.first_run:
                PCA.first_run = False  # note use of clas variable, not instance variable
                # self.show_debug_info(curr_pair)
                self.show_all_debug_info()

        conditions.append(dataframe['volume'] > 0)

        # MFI
        conditions.append(dataframe['mfi'] > 70.0)

        # above TEMA
        conditions.append(dataframe['close'] > dataframe['tema'])

        # PCA triggers
        pca_cond = (
            qtpylib.crossed_above(dataframe['predict_exit'], 0.5)
        )

        conditions.append(pca_cond)

        # add strategy-specific conditions (from subclass)
        strat_cond = self.get_strategy_sell_conditions(dataframe)
        if strat_cond is not None:
            conditions.append(strat_cond)

        dataframe.loc[pca_cond, 'exit_tag'] += 'pca_exit '

        if conditions:
            dataframe.loc[reduce(lambda x, y: x & y, conditions), 'exit'] = 1
        else:
            dataframe['exit'] = 0

        return dataframe

    ###################################

    """
    Custom Stoploss
    """

    def custom_stoploss(self, pair: str, trade: 'Trade', current_time: datetime, current_rate: float,
                        current_profit: float, **kwargs) -> float:

        # self.set_state(pair, self.State.STOPLOSS)

        dataframe, last_updated = self.dp.get_analyzed_dataframe(pair=pair, timeframe=self.timeframe)
        last_candle = dataframe.iloc[-1].squeeze()
        trade_dur = int((current_time.timestamp() - trade.open_date_utc.timestamp()) // 60)
        in_trend = self.custom_trade_info[trade.pair]['had_trend']

        # limit stoploss
        if current_profit < self.cstop_max_stoploss.value:
            return 0.01

        # Determine how we exit when we are in a loss
        if current_profit < self.cstop_loss_threshold.value:
            if self.cstop_bail_how.value == 'roc' or self.cstop_bail_how.value == 'any':
                # Dynamic bailout based on rate of change
                if last_candle['sroc'] <= self.cstop_bail_roc.value:
                    return 0.01
            if self.cstop_bail_how.value == 'time' or self.cstop_bail_how.value == 'any':
                # Dynamic bailout based on time, unless time_trend is true and there is a potential reversal
                if trade_dur > self.cstop_bail_time.value:
                    if self.cstop_bail_time_trend.value == True and in_trend == True:
                        return 1
                    else:
                        return 0.01
        return 1

    ###################################

    """
    Custom Exit
    """

    def custom_exit(self, pair: str, trade: 'Trade', current_time: 'datetime', current_rate: float,
                    current_profit: float, **kwargs):

        dataframe, _ = self.dp.get_analyzed_dataframe(pair=pair, timeframe=self.timeframe)
        last_candle = dataframe.iloc[-1].squeeze()

        trade_dur = int((current_time.timestamp() - trade.open_date_utc.timestamp()) // 60)
        max_profit = max(0, trade.calc_profit_ratio(trade.max_rate))
        pullback_value = max(0, (max_profit - self.cexit_pullback_amount.value))
        in_trend = False

        # Mod: just take the profit:
        # Above 3%, sell if MFA > 90
        if current_profit > 0.03:
            if last_candle['mfi'] > 90:
                return 'mfi_90'

        # Sell any positions at a loss if they are held for more than one day.
        if current_profit < 0.0 and (current_time - trade.open_date_utc).days >= 2:
            return 'unclog'

        # Determine our current ROI point based on the defined type
        if self.cexit_roi_type.value == 'static':
            min_roi = self.cexit_roi_start.value
        elif self.cexit_roi_type.value == 'decay':
            min_roi = cta.linear_decay(self.cexit_roi_start.value, self.cexit_roi_end.value, 0,
                                       self.cexit_roi_time.value, trade_dur)
        elif self.cexit_roi_type.value == 'step':
            if trade_dur < self.cexit_roi_time.value:
                min_roi = self.cexit_roi_start.value
            else:
                min_roi = self.cexit_roi_end.value

        # Determine if there is a trend
        if self.cexit_trend_type.value == 'rmi' or self.cexit_trend_type.value == 'any':
            if last_candle['rmi_up_trend'] == 1:
                in_trend = True
        if self.cexit_trend_type.value == 'ssl' or self.cexit_trend_type.value == 'any':
            if last_candle['ssl_dir'] == 1:
                in_trend = True
        if self.cexit_trend_type.value == 'candle' or self.cexit_trend_type.value == 'any':
            if last_candle['candle_up_trend'] == 1:
                in_trend = True

        # Don't exit if we are in a trend unless the pullback threshold is met
        if in_trend == True and current_profit > 0:
            # Record that we were in a trend for this trade/pair for a more useful exit message later
            self.custom_trade_info[trade.pair]['had_trend'] = True
            # If pullback is enabled and profit has pulled back allow a exit, maybe
            if self.cexit_pullback.value == True and (current_profit <= pullback_value):
                if self.cexit_pullback_respect_roi.value == True and current_profit > min_roi:
                    return 'intrend_pullback_roi'
                elif self.cexit_pullback_respect_roi.value == False:
                    if current_profit > min_roi:
                        return 'intrend_pullback_roi'
                    else:
                        return 'intrend_pullback_noroi'
            # We are in a trend and pullback is disabled or has not happened or various criteria were not met, hold
            return None
        # If we are not in a trend, just use the roi value
        elif in_trend == False:
            if self.custom_trade_info[trade.pair]['had_trend']:
                if current_profit > min_roi:
                    self.custom_trade_info[trade.pair]['had_trend'] = False
                    return 'trend_roi'
                elif self.cexit_endtrend_respect_roi.value == False:
                    self.custom_trade_info[trade.pair]['had_trend'] = False
                    return 'trend_noroi'
            elif current_profit > min_roi:
                return 'notrend_roi'
        else:
            return None


#######################

# Utility functions


# Elliot Wave Oscillator
def ewo(dataframe, sma1_length=5, sma2_length=35):
    sma1 = ta.EMA(dataframe, timeperiod=sma1_length)
    sma2 = ta.EMA(dataframe, timeperiod=sma2_length)
    smadif = (sma1 - sma2) / dataframe['close'] * 100
    return smadif


# Chaikin Money Flow
def chaikin_money_flow(dataframe, n=20, fillna=False) -> Series:
    """Chaikin Money Flow (CMF)
    It measures the amount of Money Flow Volume over a specific period.
    http://stockcharts.com/school/doku.php?id=chart_school:technical_indicators:chaikin_money_flow_cmf
    Args:
        dataframe(pandas.Dataframe): dataframe containing ohlcv
        n(int): n period.
        fillna(bool): if True, fill nan values.
    Returns:
        pandas.Series: New feature generated.
    """
    mfv = ((dataframe['close'] - dataframe['low']) - (dataframe['high'] - dataframe['close'])) / (
            dataframe['high'] - dataframe['low'])
    mfv = mfv.fillna(0.0)  # float division by zero
    mfv *= dataframe['volume']
    cmf = (mfv.rolling(n, min_periods=0).sum()
           / dataframe['volume'].rolling(n, min_periods=0).sum())
    if fillna:
        cmf = cmf.replace([np.inf, -np.inf], np.nan).fillna(0)
    return Series(cmf, name='cmf')


# Williams %R
def williams_r(dataframe: DataFrame, period: int = 14) -> Series:
    """Williams %R, or just %R, is a technical analysis oscillator showing the current closing price in relation to the high and low
        of the past N days (for a given N). It was developed by a publisher and promoter of trading materials, Larry Williams.
        Its purpose is to tell whether a stock or commodity market is trading near the high or the low, or somewhere in between,
        of its recent trading range.
        The oscillator is on a negative scale, from −100 (lowest) up to 0 (highest).
    """

    highest_high = dataframe["high"].rolling(center=False, window=period).max()
    lowest_low = dataframe["low"].rolling(center=False, window=period).min()

    WR = Series(
        (highest_high - dataframe["close"]) / (highest_high - lowest_low),
        name=f"{period} Williams %R",
    )

    return WR * -100


# Volume Weighted Moving Average
def vwma(dataframe: DataFrame, length: int = 10):
    """Indicator: Volume Weighted Moving Average (VWMA)"""
    # Calculate Result
    pv = dataframe['close'] * dataframe['volume']
    vwma = Series(ta.SMA(pv, timeperiod=length) / ta.SMA(dataframe['volume'], timeperiod=length))
    vwma = vwma.fillna(0, inplace=True)
    return vwma


# Exponential moving average of a volume weighted simple moving average
def ema_vwma_osc(dataframe, len_slow_ma):
    slow_ema = Series(ta.EMA(vwma(dataframe, len_slow_ma), len_slow_ma))
    return ((slow_ema - slow_ema.shift(1)) / slow_ema.shift(1)) * 100


def t3_average(dataframe, length=5):
    """
    T3 Average by HPotter on Tradingview
    https://www.tradingview.com/script/qzoC9H1I-T3-Average/
    """
    df = dataframe.copy()

    df['xe1'] = ta.EMA(df['close'], timeperiod=length)
    df['xe1'].fillna(0, inplace=True)
    df['xe2'] = ta.EMA(df['xe1'], timeperiod=length)
    df['xe2'].fillna(0, inplace=True)
    df['xe3'] = ta.EMA(df['xe2'], timeperiod=length)
    df['xe3'].fillna(0, inplace=True)
    df['xe4'] = ta.EMA(df['xe3'], timeperiod=length)
    df['xe4'].fillna(0, inplace=True)
    df['xe5'] = ta.EMA(df['xe4'], timeperiod=length)
    df['xe5'].fillna(0, inplace=True)
    df['xe6'] = ta.EMA(df['xe5'], timeperiod=length)
    df['xe6'].fillna(0, inplace=True)
    b = 0.7
    c1 = -b * b * b
    c2 = 3 * b * b + 3 * b * b * b
    c3 = -6 * b * b - 3 * b - 3 * b * b * b
    c4 = 1 + 3 * b + b * b * b + 3 * b * b
    df['T3Average'] = c1 * df['xe6'] + c2 * df['xe5'] + c3 * df['xe4'] + c4 * df['xe3']

    return df['T3Average']


def pivot_points(dataframe: DataFrame, mode='fibonacci') -> Series:
    if mode == 'simple':
        hlc3_pivot = (dataframe['high'] + dataframe['low'] + dataframe['close']).shift(1) / 3
        res1 = hlc3_pivot * 2 - dataframe['low'].shift(1)
        sup1 = hlc3_pivot * 2 - dataframe['high'].shift(1)
        res2 = hlc3_pivot + (dataframe['high'] - dataframe['low']).shift()
        sup2 = hlc3_pivot - (dataframe['high'] - dataframe['low']).shift()
        res3 = hlc3_pivot * 2 + (dataframe['high'] - 2 * dataframe['low']).shift()
        sup3 = hlc3_pivot * 2 - (2 * dataframe['high'] - dataframe['low']).shift()
        return hlc3_pivot, res1, res2, res3, sup1, sup2, sup3
    elif mode == 'fibonacci':
        hlc3_pivot = (dataframe['high'] + dataframe['low'] + dataframe['close']).shift(1) / 3
        hl_range = (dataframe['high'] - dataframe['low']).shift(1)
        res1 = hlc3_pivot + 0.382 * hl_range
        sup1 = hlc3_pivot - 0.382 * hl_range
        res2 = hlc3_pivot + 0.618 * hl_range
        sup2 = hlc3_pivot - 0.618 * hl_range
        res3 = hlc3_pivot + 1 * hl_range
        sup3 = hlc3_pivot - 1 * hl_range
        return hlc3_pivot, res1, res2, res3, sup1, sup2, sup3
    elif mode == 'DeMark':
        demark_pivot_lt = (dataframe['low'] * 2 + dataframe['high'] + dataframe['close'])
        demark_pivot_eq = (dataframe['close'] * 2 + dataframe['low'] + dataframe['high'])
        demark_pivot_gt = (dataframe['high'] * 2 + dataframe['low'] + dataframe['close'])
        demark_pivot = np.where((dataframe['close'] < dataframe['open']), demark_pivot_lt,
                                np.where((dataframe['close'] > dataframe['open']), demark_pivot_gt, demark_pivot_eq))
        dm_pivot = demark_pivot / 4
        dm_res = demark_pivot / 2 - dataframe['low']
        dm_sup = demark_pivot / 2 - dataframe['high']
        return dm_pivot, dm_res, dm_sup


def heikin_ashi(dataframe, smooth_inputs=False, smooth_outputs=False, length=10):
    df = dataframe[['open', 'close', 'high', 'low']].copy().fillna(0)
    if smooth_inputs:
        df['open_s'] = ta.EMA(df['open'], timeframe=length)
        df['high_s'] = ta.EMA(df['high'], timeframe=length)
        df['low_s'] = ta.EMA(df['low'], timeframe=length)
        df['close_s'] = ta.EMA(df['close'], timeframe=length)

        open_ha = (df['open_s'].shift(1) + df['close_s'].shift(1)) / 2
        high_ha = df.loc[:, ['high_s', 'open_s', 'close_s']].max(axis=1)
        low_ha = df.loc[:, ['low_s', 'open_s', 'close_s']].min(axis=1)
        close_ha = (df['open_s'] + df['high_s'] + df['low_s'] + df['close_s']) / 4
    else:
        open_ha = (df['open'].shift(1) + df['close'].shift(1)) / 2
        high_ha = df.loc[:, ['high', 'open', 'close']].max(axis=1)
        low_ha = df.loc[:, ['low', 'open', 'close']].min(axis=1)
        close_ha = (df['open'] + df['high'] + df['low'] + df['close']) / 4

    open_ha = open_ha.fillna(0)
    high_ha = high_ha.fillna(0)
    low_ha = low_ha.fillna(0)
    close_ha = close_ha.fillna(0)

    if smooth_outputs:
        open_sha = ta.EMA(open_ha, timeframe=length)
        high_sha = ta.EMA(high_ha, timeframe=length)
        low_sha = ta.EMA(low_ha, timeframe=length)
        close_sha = ta.EMA(close_ha, timeframe=length)

        return open_sha, close_sha, low_sha
    else:
        return open_ha, close_ha, low_ha


# Range midpoint acts as Support
def is_support(row_data) -> bool:
    conditions = []
    for row in range(len(row_data) - 1):
        if row < len(row_data) // 2:
            conditions.append(row_data[row] > row_data[row + 1])
        else:
            conditions.append(row_data[row] < row_data[row + 1])
    result = reduce(lambda x, y: x & y, conditions)
    return result


# Range midpoint acts as Resistance
def is_resistance(row_data) -> bool:
    conditions = []
    for row in range(len(row_data) - 1):
        if row < len(row_data) // 2:
            conditions.append(row_data[row] < row_data[row + 1])
        else:
            conditions.append(row_data[row] > row_data[row + 1])
    result = reduce(lambda x, y: x & y, conditions)
    return result


def range_percent_change(dataframe: DataFrame, method, length: int) -> float:
    """
    Rolling Percentage Change Maximum across interval.

    :param dataframe: DataFrame The original OHLC dataframe
    :param method: High to Low / Open to Close
    :param length: int The length to look back
    """
    if method == 'HL':
        return (dataframe['high'].rolling(length).max() - dataframe['low'].rolling(length).min()) / dataframe[
            'low'].rolling(length).min()
    elif method == 'OC':
        return (dataframe['open'].rolling(length).max() - dataframe['close'].rolling(length).min()) / dataframe[
            'close'].rolling(length).min()
    else:
        raise ValueError(f"Method {method} not defined!")
