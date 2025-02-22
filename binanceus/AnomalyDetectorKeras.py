# base class that implements an Anomaly detector using keras models
# subclasses should override the create_model() method


import numpy as np
from pandas import DataFrame, Series
import pandas as pd

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

import random

import os

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '1'
os.environ['TF_DETERMINISTIC_OPS'] = '1'

import tensorflow as tf

seed = 42
os.environ['PYTHONHASHSEED'] = str(seed)
random.seed(seed)
tf.random.set_seed(seed)
np.random.seed(seed)

tf.compat.v1.logging.set_verbosity(tf.compat.v1.logging.WARN)

import keras
from keras import layers

import h5py

import DataframeUtils

class AnomalyDetectorKeras():

    model = None
    is_trained = False
    name = ""
    model_path = ""
    checkpoint_path = "/tmp/model.h5"
    seq_len = 8
    num_features = 64
    encoder_layer = 'encoder_output'
    encoder = None
    num_epochs = 256  # number of iterations for training
    batch_size = 1024  # batch size for training
    clean_data_required = True # train with positive rows removed
    

    def __init__(self, num_features, tag=""):
        super().__init__()

        self.loaded_from_file = False

        self.name = self.__class__.__name__ + "_" + tag
        self.model_path = self.get_model_path()
        self.num_features = num_features
        # print("    num_features: ", num_features)

        # load saved model if present
        self.model = self.load()

    # create model - subclasses should overide this
    def create_model(self, seq_len, num_features):

        model = None
        outer_dim = 64
        inner_dim = 16

        print("    WARNING: create_model() should be defined by the subclass")
        # create a simple model for illustrative purposes (or to test the framework)
        model = keras.Sequential(name=self.name)

        # Encoder
        model.add(layers.Dense(outer_dim, activation='relu', input_shape=(seq_len, num_features)))
        model.add(layers.Dense(2*outer_dim, activation='relu'))
        model.add(layers.Dense(inner_dim, activation='relu', name=self.encoder_layer)) # name is mandatory

        # Decoder
        model.add(layers.Dense(2*outer_dim, activation='relu', input_shape=(1, inner_dim)))
        model.add(layers.Dense(outer_dim, activation='relu'))


        model.add(layers.Dense(num_features, activation=None))

        # optimizer = keras.optimizers.Adam()
        optimizer = keras.optimizers.Adam(learning_rate=0.01)

        model.compile(metrics=['accuracy', 'mse'], loss='mse', optimizer=optimizer)

        return model

    # update training using the suplied (normalised) dataframe. Training is cumulative
    # the 'labels' args should contain 0.0 for normal results, '1.0' for anomalies (buy or sell)
    def train(self, df_train_norm: DataFrame, df_test_norm: DataFrame, train_labels, test_labels, force_train=False):

        if self.is_trained and not force_train:
            return

        if self.model is None:
            self.model = self.create_model(self.seq_len, self.num_features)
            if self.model is None:
                print("    ERR: model not created")
                return
            self.model.summary()

        # remove rows with positive labels?!
        if self.clean_data_required:
            df1 = df_train_norm.copy()
            df1['%labels'] = train_labels
            df1 = df1[(df1['%labels'] < 0.1)]
            df_train = df1.drop('%labels', axis=1)

            df2 = df_train_norm.copy()
            df2['%labels'] = train_labels
            df2 = df2[(df2['%labels'] < 0.1)]
            df_test = df2.drop('%labels', axis=1)
        else:
            df_train = df_train_norm.copy()
            df_test = df_test_norm.copy()

        train_tensor = DataframeUtils.df_to_tensor(df_train, self.seq_len)
        test_tensor = DataframeUtils.df_to_tensor(df_test, self.seq_len)

        monitor_field = 'loss'
        monitor_mode = "min"
        early_patience = 4
        plateau_patience = 4

        # callback to control early exit on plateau of results
        early_callback = keras.callbacks.EarlyStopping(
            monitor=monitor_field,
            mode=monitor_mode,
            patience=early_patience,
            min_delta=0.0001,
            restore_best_weights=True,
            verbose=1)

        plateau_callback = keras.callbacks.ReduceLROnPlateau(
            monitor=monitor_field,
            mode=monitor_mode,
            factor=0.1,
            min_delta=0.0001,
            patience=plateau_patience,
            verbose=0)

        # callback to control saving of 'best' model
        # Note that we use validation loss as the metric, not training loss
        checkpoint_callback = keras.callbacks.ModelCheckpoint(
            filepath=self.checkpoint_path,
            save_weights_only=True,
            monitor=monitor_field,
            mode=monitor_mode,
            save_best_only=True,
            verbose=0)

        callbacks = [plateau_callback, early_callback, checkpoint_callback]

        # if self.dbg_verbose:
        print("")
        print("    training model: {}...".format(self.name))

        # print("    train_tensor:{} test_tensor:{}".format(np.shape(train_tensor), np.shape(test_tensor)))

        # Model weights are saved at the end of every epoch, if it's the best seen so far.
        fhis = self.model.fit(train_tensor, train_tensor,
                                    batch_size=self.batch_size,
                                    epochs=self.num_epochs,
                                    callbacks=callbacks,
                                    validation_data=(test_tensor, test_tensor),
                                    verbose=1)

        # # The model weights (that are considered the best) are loaded into th model.
        # self.update_model_weights()

        self.save()
        self.is_trained = True

        return


    # evaluate model using the supplied (normalised) dataframe as test data.
    def evaluate(self, df_norm: DataFrame):
        test_tensor = DataframeUtils.df_to_tensor(df_norm, self.seq_len)

        print("    Predicting...")
        preds = self.model.predict(test_tensor, verbose=1)

        print("    Comparing...")
        score = self.model.evaluate(test_tensor, preds, return_dict=True, verbose=1)
        print("model:{} score:{} ".format(self.name, score))

        loss = tf.keras.metrics.mean_squared_error(test_tensor, preds)
        # print("    loss:{} {}".format(np.shape(loss), loss))
        loss = np.array(loss[0])
        print("    loss:")
        print("        sum:{:.3f} min:{:.3f} max:{:.3f} mean:{:.3f} std:{:.3f}".format(loss.sum(),
                                                                                       loss.min(), loss.max(),
                                                                                       loss.mean(), loss.std()))
        return

    # 'recosnstruct' a dataframe by passing it through the model
    def reconstruct(self, df_norm:DataFrame) -> DataFrame:
        cols = df_norm.columns
        tensor = DataframeUtils.df_to_tensor(df_norm, self.seq_len)
        encoded_tensor = self.model.predict(tensor, verbose=1)
        # print("    encoded_tensor:{}".format(np.shape(encoded_tensor)))
        encode_array = encoded_tensor[:, 0, :]
        encoded_array = encode_array.reshape(np.shape(encoded_tensor)[0], np.shape(encoded_tensor)[2])
        # print("    encoded_array:{}".format(np.shape(encoded_array)))

        return pd.DataFrame(encoded_array, columns=cols)

    # transform supplied (normalised) dataframe into a lower dimension version
    def transform(self, df_norm: DataFrame) -> DataFrame:
        if self.encoder is None:
            self.encoder = self.model.get_layer(self.encoder_layer)
        cols = df_norm.columns
        # tensor = np.array(df_norm).reshape(df_norm.shape[0], 1, df_norm.shape[1])
        tensor = DataframeUtils.df_to_tensor(df_norm, self.seq_len)
        encoded_tensor = self.encoder.predict(tensor, verbose=1)
        encoded_array = encoded_tensor.reshape(np.shape(encoded_tensor)[0], np.shape(encoded_tensor)[2])

        return pd.DataFrame(encoded_array, columns=cols)

    def predict(self, df_norm: DataFrame):

        # convert to tensor format and run the autoencoder
        # tensor = np.array(df_norm).reshape(df_norm.shape[0], 1, df_norm.shape[1])
        tensor = DataframeUtils.df_to_tensor(df_norm, self.seq_len)

        predict_tensor = self.model.predict(tensor, verbose=1)

        # not sure why, but predict sometimes returns an odd length
        if np.shape(predict_tensor)[0] != np.shape(tensor)[0]:
            print("    ERR: prediction length mismatch ({} vs {})".format(len(predict_tensor), np.shape(tensor)[0]))
            predictions = np.zeros(df_norm.shape[0], dtype=float)
        else:
            # get losses by comparing input to output
            msle = tf.keras.losses.msle(predict_tensor, tensor)
            msle = msle[:, 0]

            # mean + stddev method
            # threshold for anomaly scores
            threshold = np.mean(msle.numpy()) + 2.0 * np.std(msle.numpy())

            # anything anomylous results in a '1'
            predictions = np.where(msle > threshold, 1.0, 0.0)

            # # Median Absolute Deviation method
            # threshold = 3.0 # empirical for Dense
            # # threshold = 2.0 # empirical for Conv
            # z_scores = self.mad_score(msle)
            # predictions = np.where(z_scores > threshold, 1.0, 0.0)

            # # Mean Absolute Error (MAE) method
            # t1 = predict_tensor[:, 0, :].reshape(np.shape(predict_tensor)[0], np.shape(predict_tensor)[2])
            # t2 = tensor[:, 0, :].reshape(np.shape(tensor)[0], np.shape(tensor)[2])
            # print("    predict_tensor:{} tensor:{}".format(np.shape(predict_tensor), np.shape(tensor)))

            # mae_loss = np.mean(np.abs(predict_tensor - tensor), axis=1)
            # threshold = np.max(mae_loss)
            # predictions = np.where(mae_loss > threshold, 1.0, 0.0)
            # print("    predictions:{} data:{}".format(np.shape(predictions), predictions))

        return predictions

    # returns path to 'full' model file
    def get_model_path(self):
        # set as subdirectory of location of this file (so that it can be included in the repository)
        file_dir = os.path.dirname(str(Path(__file__)))
        save_dir = file_dir + "/models/" + self.__class__.__name__ + '/'
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)
        model_path = save_dir + self.name + ".h5"
        return model_path

    def get_checkpoint_path(self):
        checkpoint_dir = '/tmp'
        model_path = checkpoint_dir + "/" + self.name + "/" + "checkpoint.h5"
        return model_path
    
    def save(self, path=""):
        
        if len(path) == 0:
            self.model_path = self.get_model_path()
            path = self.model_path
        else:
            self.model_path = path
            
        print("    saving model to: ", path)
        keras.models.save_model(self.model, filepath=path)
        return

    def load(self, path=""):
        
        if len(path) == 0:
            self.model_path = self.get_model_path()
            path = self.model_path
        else:
            self.model_path = path

        model = None
        
        # if model exists, load it
        if os.path.exists(path):
            print("    Loading existing model ({})...".format(path))
            try:
                model = keras.models.load_model(path, compile=False)
                # optimizer = keras.optimizers.Adam()
                optimizer = keras.optimizers.Adam(learning_rate=0.001)
                model.compile(metrics=['accuracy', 'mse'], loss='mse', optimizer=optimizer)
                self.is_trained = True

            except Exception as e:
                print("    ", str(e))
                print("    Error loading model from {}. Check whether model format changed".format(path))
        else:
            print("    model not found ({})...".format(path))

        return model

    def model_is_trained(self) -> bool:
        return self.is_trained

    def needs_clean_data(self) -> bool:
        # print("    clean_data_required: ", self.clean_data_required)
        return self.clean_data_required
    

    def update_model_weights(self):

        # if checkpoint already exists, load the weights
        if os.path.exists(self.checkpoint_path):
            print("    Loading existing model weights ({})...".format(self.checkpoint_path))
            try:
                self.model.load_weights(self.checkpoint_path)
            except:
                print("    Error loading weights from {}. Check whether model format changed".format(
                    self.checkpoint_path))
        else:
            print("    model not found ({})...".format(self.checkpoint_path))

        return

    # Median Absolute Deviation
    def mad_score(self, points):
        """https://www.itl.nist.gov/div898/handbook/eda/section3/eda35h.htm """
        m = np.median(points)
        ad = np.abs(points - m)
        mad = np.median(ad)

        return 0.6745 * ad / mad
