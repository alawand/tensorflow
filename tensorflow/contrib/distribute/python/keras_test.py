# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Tests for tf.keras models using DistributionStrategy."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
from absl.testing import parameterized
import numpy as np

from tensorflow.contrib.distribute.python import combinations
from tensorflow.contrib.distribute.python import mirrored_strategy
from tensorflow.contrib.distribute.python import tpu_strategy
from tensorflow.contrib.distribute.python import values
from tensorflow.python import keras
from tensorflow.python.data.ops import dataset_ops
from tensorflow.python.estimator import keras as keras_lib
from tensorflow.python.estimator import run_config as run_config_lib
from tensorflow.python.framework import constant_op
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import random_seed
from tensorflow.python.framework import test_util
from tensorflow.python.keras import testing_utils
from tensorflow.python.keras.engine import distributed_training_utils
from tensorflow.python.ops.parsing_ops import gen_parsing_ops
from tensorflow.python.platform import gfile
from tensorflow.python.platform import test
from tensorflow.python.summary.writer import writer_cache
from tensorflow.python.training import gradient_descent
from tensorflow.python.training import rmsprop


_RANDOM_SEED = 1337
_TRAIN_SIZE = 200
_INPUT_SIZE = (10,)
_NUM_CLASS = 2


# TODO(anjalisridhar): Add a decorator that will allow us to run these tests as
# part of the tf.keras unit tests suite.
def simple_sequential_model():
  model = keras.models.Sequential()
  model.add(keras.layers.Dense(16, activation='relu', input_shape=_INPUT_SIZE))
  model.add(keras.layers.Dropout(0.1))
  model.add(keras.layers.Dense(_NUM_CLASS, activation='softmax'))
  return model


def simple_functional_model():
  a = keras.layers.Input(shape=_INPUT_SIZE)
  b = keras.layers.Dense(16, activation='relu')(a)
  b = keras.layers.Dropout(0.1)(b)
  b = keras.layers.Dense(_NUM_CLASS, activation='softmax')(b)
  model = keras.models.Model(inputs=[a], outputs=[b])
  return model


def multi_inputs_multi_outputs_model():
  input_a = keras.layers.Input(shape=(16,), name='input_a')
  input_b = keras.layers.Input(shape=(16,), name='input_b')
  input_m = keras.layers.Input(shape=(8,), dtype='string', name='input_m')
  dense = keras.layers.Dense(8, name='dense_1')

  interm_a = dense(input_a)
  # Read m
  interm_m = keras.layers.Lambda(gen_parsing_ops.string_to_number)(input_m)
  interm_s = keras.layers.Lambda(lambda k: k[0] * k[1])([interm_m, interm_a])
  interm_b = dense(input_b)
  merged = keras.layers.concatenate([interm_s, interm_b], name='merge')
  output_c = keras.layers.Dense(3, activation='softmax', name='dense_2')(merged)
  output_d = keras.layers.Dense(2, activation='softmax', name='dense_3')(merged)
  model = keras.models.Model(
      inputs=[input_a, input_b, input_m], outputs=[output_c, output_d])
  model.compile(
      loss='categorical_crossentropy',
      optimizer=gradient_descent.GradientDescentOptimizer(0.001),
      metrics={
          'dense_2': 'categorical_accuracy',
          'dense_3': 'categorical_accuracy'
      })
  return model


def get_ds_train_input_fn():
  np.random.seed(_RANDOM_SEED)
  (x_train, y_train), _ = testing_utils.get_test_data(
      train_samples=_TRAIN_SIZE,
      test_samples=50,
      input_shape=_INPUT_SIZE,
      num_classes=_NUM_CLASS)
  y_train = keras.utils.to_categorical(y_train)

  dataset = dataset_ops.Dataset.from_tensor_slices((x_train, y_train))
  dataset = dataset.batch(32)
  return dataset


def get_ds_test_input_fn():
  np.random.seed(_RANDOM_SEED)
  _, (x_test, y_test) = testing_utils.get_test_data(
      train_samples=_TRAIN_SIZE,
      test_samples=50,
      input_shape=_INPUT_SIZE,
      num_classes=_NUM_CLASS)
  y_test = keras.utils.to_categorical(y_test)

  dataset = dataset_ops.Dataset.from_tensor_slices((x_test, y_test))
  dataset = dataset.batch(32)
  return dataset


def get_multi_inputs_multi_outputs_data():
  (a_train, c_train), (a_test, c_test) = testing_utils.get_test_data(
      train_samples=_TRAIN_SIZE,
      test_samples=50,
      input_shape=(16,),
      num_classes=3,
      random_seed=_RANDOM_SEED)
  (b_train, d_train), (b_test, d_test) = testing_utils.get_test_data(
      train_samples=_TRAIN_SIZE,
      test_samples=50,
      input_shape=(16,),
      num_classes=2,
      random_seed=_RANDOM_SEED)
  (m_train, _), (m_test, _) = testing_utils.get_test_data(
      train_samples=_TRAIN_SIZE,
      test_samples=50,
      input_shape=(8,),
      num_classes=2,
      random_seed=_RANDOM_SEED)

  c_train = keras.utils.to_categorical(c_train)
  c_test = keras.utils.to_categorical(c_test)
  d_train = keras.utils.to_categorical(d_train)
  d_test = keras.utils.to_categorical(d_test)

  train_data = {
      'input_a': a_train,
      'input_b': b_train,
      'input_m': m_train,
      'output_c': c_train,
      'output_d': d_train
  }
  test_data = {
      'input_a': a_test,
      'input_b': b_test,
      'input_m': m_test,
      'output_c': c_test,
      'output_d': d_test
  }

  return (train_data, test_data)


def batch_wrapper(dataset, batch_size, distribution):
  # TPUs currently require fully defined input shapes, drop_remainder ensures
  # the input will have fully defined shapes.
  if isinstance(distribution, tpu_strategy.TPUStrategy):
    return dataset.batch(batch_size, drop_remainder=True)
  else:
    return dataset.batch(batch_size)


def get_model():
  x = keras.layers.Input(shape=(3,), name='input')
  y = keras.layers.Dense(4, name='dense')(x)
  model = keras.Model(x, y)
  return model


def get_dataset(distribution):
  inputs = np.zeros((10, 3), dtype=np.float32)
  targets = np.zeros((10, 4), dtype=np.float32)
  dataset = dataset_ops.Dataset.from_tensor_slices((inputs, targets))
  dataset = dataset.repeat(100)
  dataset = batch_wrapper(dataset, 10, distribution)
  return dataset


def get_predict_dataset(distribution):
  inputs = np.zeros((10, 3), dtype=np.float32)
  dataset = dataset_ops.Dataset.from_tensor_slices(inputs)
  dataset = dataset.repeat(100)
  dataset = batch_wrapper(dataset, 10, distribution)
  return dataset


def multi_input_output_model():
  a = keras.layers.Input(shape=(3,), name='input_a')
  b = keras.layers.Input(shape=(5,), name='input_b')
  # TODO(anjalisridhar): Change the output dimension of the second Dense layer
  # once the iterator output validation issue has been fixed.
  dense_1 = keras.layers.Dense(7, name='dense_1')
  dense_2 = keras.layers.Dense(7, name='dense_2')
  c = dense_1(a)
  d = dense_2(b)
  e = keras.layers.Dropout(0.5, name='dropout')(c)
  model = keras.models.Model([a, b], [d, e])
  return model


def get_correctness_test_inputs(use_numpy, with_distribution,
                                x_train, y_train, x_predict):
  """Generates the inputs for correctness check when enable Keras with DS."""
  global_batch_size = 64
  batch_size = global_batch_size
  # TODO(b/118776054): Use global batch size for Keras/DS support.
  use_per_core_batch_size = (
      with_distribution and
      not isinstance(with_distribution, tpu_strategy.TPUStrategy))
  if use_per_core_batch_size:
    batch_size //= with_distribution.num_replicas_in_sync

  if use_numpy:
    training_inputs = {
        'batch_size': batch_size,
        'x': x_train,
        'y': y_train,
        'epochs': 1,
        'shuffle': False,
    }
    eval_inputs = {
        'batch_size': batch_size,
        'x': x_train,
        'y': y_train,
    }
    predict_inputs = {
        'x': np.array(x_predict, dtype=np.float32),
    }
  else:
    # For dataset inputs, we do not pass batch_size to
    # keras.fit/evaluate/predict. The batch size is part of the dataset.
    train_dataset = dataset_ops.Dataset.from_tensor_slices(
        (x_train, y_train))
    x = batch_wrapper(train_dataset, batch_size, with_distribution)

    training_inputs = {
        'batch_size': None,
        'x': x,
        'y': None,
        'epochs': 1,
        'shuffle': False,
        'steps_per_epoch': len(x_train) // global_batch_size,
    }
    eval_inputs = {
        'batch_size': None,
        'x': x,
        'y': None,
        'steps': 20,
    }
    predict_batch_size = len(x_predict)
    if use_per_core_batch_size:
      predict_batch_size //= with_distribution.num_replicas_in_sync
    predict_dataset = dataset_ops.Dataset.from_tensor_slices(x_predict)
    predict_dataset = batch_wrapper(predict_dataset,
                                    predict_batch_size, with_distribution)
    predict_inputs = {
        'steps': 1,
        'x': predict_dataset,
    }

  return training_inputs, eval_inputs, predict_inputs


strategies = [combinations.default_strategy,
              combinations.one_device_strategy,
              combinations.mirrored_strategy_with_gpu_and_cpu,
              combinations.mirrored_strategy_with_two_gpus,
              combinations.tpu_strategy,  # steps_per_run=2
              combinations.tpu_strategy_one_step]


def strategy_minus_tpu_combinations():
  return combinations.combine(
      distribution=[combinations.default_strategy,
                    combinations.one_device_strategy,
                    combinations.mirrored_strategy_with_gpu_and_cpu,
                    combinations.mirrored_strategy_with_two_gpus],
      mode=['graph'])


def strategy_combinations():
  return combinations.combine(
      distribution=strategies,
      mode=['graph'])


def strategy_and_optimizer_combinations():
  return combinations.combine(
      distribution=strategies,
      optimizer=[combinations.adagrad_optimizer_v1_fn,
                 combinations.adam_optimizer_v1_fn,
                 combinations.gradient_descent_optimizer_v1_fn,
                 combinations.rmsprop_optimizer_v1_fn],
      mode=['graph'])


def strategy_and_inputs():
  return combinations.combine(
      distribution=strategies,
      use_numpy=[True, False],
      mode=['graph'])


class TestEstimatorDistributionStrategy(test_util.TensorFlowTestCase):

  def setUp(self):
    self._base_dir = os.path.join(self.get_temp_dir(),
                                  'keras_mirrored_strategy_test')
    gfile.MakeDirs(self._base_dir)
    self._config = run_config_lib.RunConfig(
        tf_random_seed=_RANDOM_SEED, model_dir=self._base_dir)
    self._dist = mirrored_strategy.MirroredStrategy(
        devices=['/device:GPU:0', '/device:GPU:1'])

  def tearDown(self):
    writer_cache.FileWriterCache.clear()
    if os.path.isdir(self._base_dir):
      gfile.DeleteRecursively(self._base_dir)

  def test_train_functional_with_distribution_strategy(self):
    dist = mirrored_strategy.MirroredStrategy(
        devices=['/device:GPU:0', '/device:GPU:1'])
    keras_model = simple_functional_model()
    keras_model.compile(
        loss='categorical_crossentropy',
        metrics=[keras.metrics.CategoricalAccuracy()],
        optimizer=rmsprop.RMSPropOptimizer(learning_rate=0.01))
    config = run_config_lib.RunConfig(tf_random_seed=_RANDOM_SEED,
                                      model_dir=self._base_dir,
                                      train_distribute=dist,
                                      eval_distribute=dist)
    with self.cached_session():
      est_keras = keras_lib.model_to_estimator(
          keras_model=keras_model, config=config)
      before_eval_results = est_keras.evaluate(
          input_fn=get_ds_test_input_fn, steps=1)
      est_keras.train(input_fn=get_ds_train_input_fn, steps=_TRAIN_SIZE / 16)
      after_eval_results = est_keras.evaluate(input_fn=get_ds_test_input_fn,
                                              steps=1)
      self.assertLess(after_eval_results['loss'], before_eval_results['loss'])

    writer_cache.FileWriterCache.clear()
    gfile.DeleteRecursively(self._config.model_dir)

  def test_train_sequential_with_distribution_strategy(self):
    dist = mirrored_strategy.MirroredStrategy(
        devices=['/device:GPU:0', '/device:GPU:1'])
    keras_model = simple_sequential_model()
    keras_model.compile(
        loss='categorical_crossentropy',
        metrics=[keras.metrics.CategoricalAccuracy()],
        optimizer=rmsprop.RMSPropOptimizer(learning_rate=0.01))
    config = run_config_lib.RunConfig(tf_random_seed=_RANDOM_SEED,
                                      model_dir=self._base_dir,
                                      train_distribute=dist)
    with self.cached_session():
      est_keras = keras_lib.model_to_estimator(
          keras_model=keras_model, config=config)
      before_eval_results = est_keras.evaluate(
          input_fn=get_ds_test_input_fn, steps=1)
      est_keras.train(input_fn=get_ds_train_input_fn, steps=_TRAIN_SIZE / 16)
      after_eval_results = est_keras.evaluate(input_fn=get_ds_test_input_fn,
                                              steps=1)
      self.assertLess(after_eval_results['loss'], before_eval_results['loss'])

    writer_cache.FileWriterCache.clear()
    gfile.DeleteRecursively(self._config.model_dir)

  def test_multi_inputs_multi_outputs_with_input_fn_as_dict(self):
    train_data, test_data = get_multi_inputs_multi_outputs_data()

    def train_input_fn():
      input_dict = {
          'input_a': train_data['input_a'],
          'input_b': train_data['input_b'],
          'input_m': train_data['input_m'].astype(np.str)
      }
      output_dict = {
          'dense_2': train_data['output_c'],
          'dense_3': train_data['output_d']
      }
      return dataset_ops.Dataset.from_tensor_slices((input_dict,
                                                     output_dict)).batch(16)

    def eval_input_fn():
      input_dict = {
          'input_a': test_data['input_a'],
          'input_b': test_data['input_b'],
          'input_m': test_data['input_m'].astype(np.str)
      }
      output_dict = {
          'dense_2': test_data['output_c'],
          'dense_3': test_data['output_d']
      }
      return dataset_ops.Dataset.from_tensor_slices((input_dict,
                                                     output_dict)).batch(16)

    self.do_test_multi_inputs_multi_outputs_with_input_fn(
        train_input_fn, eval_input_fn)

  def do_test_multi_inputs_multi_outputs_with_input_fn(self, train_input_fn,
                                                       eval_input_fn):
    config = run_config_lib.RunConfig(
        tf_random_seed=_RANDOM_SEED,
        model_dir=self._base_dir,
        train_distribute=self._dist)
    with self.cached_session():
      model = multi_inputs_multi_outputs_model()
      est_keras = keras_lib.model_to_estimator(keras_model=model, config=config)
      baseline_eval_results = est_keras.evaluate(
          input_fn=eval_input_fn, steps=1)
      est_keras.train(input_fn=train_input_fn, steps=_TRAIN_SIZE / 16)
      eval_results = est_keras.evaluate(input_fn=eval_input_fn, steps=1)
      self.assertLess(eval_results['loss'], baseline_eval_results['loss'])

  def test_keras_optimizer_with_distribution_strategy(self):
    dist = mirrored_strategy.MirroredStrategy(
        devices=['/device:GPU:0', '/device:GPU:1'])
    keras_model = simple_sequential_model()
    keras_model.compile(
        loss='categorical_crossentropy',
        optimizer=keras.optimizers.rmsprop(lr=0.01))

    config = run_config_lib.RunConfig(tf_random_seed=_RANDOM_SEED,
                                      model_dir=self._base_dir,
                                      train_distribute=dist)
    with self.cached_session():
      est_keras = keras_lib.model_to_estimator(keras_model=keras_model,
                                               config=config)
      with self.assertRaisesRegexp(ValueError,
                                   'Only TensorFlow native optimizers are '
                                   'supported with DistributionStrategy.'):
        est_keras.train(input_fn=get_ds_train_input_fn, steps=_TRAIN_SIZE / 16)

    writer_cache.FileWriterCache.clear()
    gfile.DeleteRecursively(self._config.model_dir)


class TestDistributionStrategyWithNumpyArrays(test.TestCase,
                                              parameterized.TestCase):

  @combinations.generate(strategy_combinations())
  def test_creating_var_with_numpy_arrays(self, distribution):
    with self.cached_session():
      x = np.asarray(np.random.random((64, 3)), dtype=np.float32)
      var_x = distributed_training_utils.get_var_for_numpy(distribution, x)
      val = self.evaluate(var_x.value())
      # Verify that the numpy value is copied to the variable.
      self.assertAllEqual(x, val)

  def test_calculating_batch_params(self):
    # This verifies that we calculate the number of steps when the batch size
    # is specified.
    with self.cached_session():
      # 64 is the number of input samples.
      inputs = np.zeros((64, 3), dtype=np.float32)
      # The number of replicas is equal to 3.
      strategy = mirrored_strategy.MirroredStrategy(['/device:GPU:0',
                                                     '/device:CPU:0',
                                                     '/device:GPU:1'])

      with self.assertRaisesRegexp(ValueError, 'The number of samples is not '
                                   'divisible by batch size.'):
        # The batch size(128) is larger than the number of input
        # samples(64).
        distributed_training_utils.get_input_batch_params(inputs,
                                                          128,
                                                          strategy)

      with self.assertRaisesRegexp(ValueError, 'is smaller than the number '
                                               'of replicas'):
        # The batch size(32) * num_replicas_in_sync(3) is 96 which is greater
        # than the number of input samples(64).
        distributed_training_utils.get_input_batch_params(inputs,
                                                          32,
                                                          strategy)

      # The number of replicas now is equal to 2.
      strategy = mirrored_strategy.MirroredStrategy(['/device:GPU:0',
                                                     '/device:CPU:0'])
      # 32 is the batch size per replica.
      steps = distributed_training_utils.get_input_batch_params(inputs,
                                                                32,
                                                                strategy)
      # The number of batches is the ratio of input samples(64) to
      # batch size(32) which is 2. The number of steps(1) is the ratio of
      # number of batches(2) to the number of replicas(2).
      self.assertEqual(steps, 1)

      # 16 is the batch size per replica.
      steps = distributed_training_utils.get_input_batch_params(inputs,
                                                                16,
                                                                strategy)
      # The number of batches is the ratio of input samples(64) to
      # batch size(16) which is 4. The number of steps(2) is the ratio of
      # number of batches(4) to the number of replicas(2).
      self.assertEqual(steps, 2)

  def test_calculating_batch_size(self):
    with self.cached_session():
      # 64 is the number of input samples.
      inputs = np.zeros((64, 3), dtype=np.float32)
      targets = np.zeros((64, 4), dtype=np.float32)

      model = get_model()
      optimizer = gradient_descent.GradientDescentOptimizer(0.001)
      loss = 'mse'
      strategy = mirrored_strategy.MirroredStrategy(['/device:GPU:0',
                                                     '/device:CPU:0'])
      strategy.extended._require_static_shapes = True

      model.compile(optimizer, loss, distribute=strategy)
      iterator = model._distribution_standardize_user_data(inputs,
                                                           targets,
                                                           batch_size=None,
                                                           check_steps=True,
                                                           steps_name='steps',
                                                           steps=3)

      # The global batch size(21) across all replicas is the ratio of the input
      # samples(64) to the steps(3).
      # The batch size(10) per device is the ratio of the global batch size(21)
      # to the number of replicas(2).
      # The global batch size and batch size are rounded integer values.
      self.assertEqual(10, distributed_training_utils.get_batch_dimension(
          iterator._iterator))

  @combinations.generate(strategy_combinations())
  def test_calling_model_with_numpy_arrays(self, distribution):
    with self.cached_session():
      model = get_model()

      optimizer = gradient_descent.GradientDescentOptimizer(0.001)
      loss = 'mse'
      metrics = ['mae']
      model.compile(optimizer, loss, metrics=metrics, distribute=distribution)

      inputs = np.zeros((64, 3), dtype=np.float32)
      targets = np.zeros((64, 4), dtype=np.float32)

      # Call fit with validation data
      model.fit(inputs, targets, epochs=1, batch_size=2, verbose=0,
                validation_data=(inputs, targets))

      # TODO(anjalisridhar): We need tests for when the batch size and steps are
      # smaller and results in a 0 batch_size and steps value.
      model.evaluate(inputs, targets)
      # with steps
      model.evaluate(inputs, targets, steps=2)
      # with batch_size
      model.evaluate(inputs, targets, batch_size=8)

      model.predict(inputs)
      # with steps
      model.predict(inputs, steps=2)
      # with batch_size
      model.predict(inputs, batch_size=8)

  @combinations.generate(strategy_combinations())
  def test_calling_model_with_nested_numpy_arrays(self, distribution):
    with self.cached_session():
      model = multi_input_output_model()

      optimizer = gradient_descent.GradientDescentOptimizer(learning_rate=0.001)
      loss = 'mse'
      model.compile(optimizer, loss, distribute=distribution)

      input_a_np = np.asarray(np.random.random((64, 3)), dtype=np.float32)
      input_b_np = np.asarray(np.random.random((64, 5)), dtype=np.float32)
      inputs = [input_a_np, input_b_np]

      output_d_np = np.asarray(np.random.random((64, 7)), dtype=np.float32)
      output_e_np = np.asarray(np.random.random((64, 7)), dtype=np.float32)
      targets = [output_d_np, output_e_np]

      # Call fit with validation data
      model.fit(inputs, targets, epochs=1, batch_size=8, verbose=0)

      # TODO(anjalisridhar): We need tests for when the batch size and steps are
      # smaller and results in a 0 batch_size and steps value.
      model.evaluate(inputs, targets)
      # with steps
      model.evaluate(inputs, targets, steps=2)
      # with batch_size
      model.evaluate(inputs, targets, batch_size=8)

      model.predict(inputs)
      # with steps
      model.predict(inputs, steps=2)
      # with batch_size
      model.predict(inputs, batch_size=8)

  @combinations.generate(strategy_minus_tpu_combinations())
  def test_numpy_with_sample_weights(self, distribution):
    model = get_model()
    optimizer = rmsprop.RMSPropOptimizer(learning_rate=0.001)
    loss = 'mse'
    model.compile(optimizer, loss, distribute=distribution)

    inputs = np.zeros((10, 3), np.float32)
    targets = np.zeros((10, 4), np.float32)
    sample_weights = np.ones((10), np.float32)

    model.fit(inputs, targets, sample_weight=sample_weights, epochs=1,
              steps_per_epoch=2, verbose=1)

  @combinations.generate(strategy_combinations())
  def test_flatten_predict_outputs(self, distribution):
    with self.cached_session():
      model = multi_input_output_model()

      optimizer = gradient_descent.GradientDescentOptimizer(learning_rate=0.001)
      loss = 'mse'
      model.compile(optimizer, loss, distribute=distribution)

      # We take 6 input samples with each input having a dimension of 3 or 5.
      input_a_np = np.asarray(np.random.random((6, 3)), dtype=np.float32)
      input_b_np = np.asarray(np.random.random((6, 5)), dtype=np.float32)
      inputs = [input_a_np, input_b_np]

      outs = model.predict(inputs, steps=1)
      # `predict` a list that is equal in length to the number of model outputs.
      # In this test our model has two outputs and each element of `outs`
      # corresponds to all the samples of one of the model outputs.
      self.assertEqual(2, len(outs))
      # Each of the output samples have a dimension of 7. We should process all
      # the available input samples(6).
      self.assertAllEqual([6, 7], outs[0].shape)
      self.assertAllEqual([6, 7], outs[1].shape)


class TestDistributionStrategyWithDatasets(test.TestCase,
                                           parameterized.TestCase):

  @combinations.generate(strategy_combinations())
  def test_calling_model_on_same_dataset(self, distribution):
    with self.cached_session():
      model = get_model()

      optimizer = gradient_descent.GradientDescentOptimizer(0.001)
      loss = 'mse'
      metrics = ['mae', keras.metrics.CategoricalAccuracy()]
      model.compile(optimizer, loss, metrics=metrics, distribute=distribution)

      dataset = get_dataset(distribution)

      # Call fit with validation data
      model.fit(dataset, epochs=1, steps_per_epoch=2, verbose=0,
                validation_data=dataset, validation_steps=2)
      model.fit(dataset, epochs=1, steps_per_epoch=2, verbose=0,
                validation_data=dataset, validation_steps=2)
      model.predict(get_predict_dataset(distribution), steps=2)

  @combinations.generate(strategy_combinations())
  def test_model_interleaved_eval_same_as_direct_eval(self, distribution):
    with self.cached_session():
      user_controlled_model = get_model()
      user_controlled_model.compile(
          gradient_descent.GradientDescentOptimizer(0.001),
          loss='mse',
          metrics=['mae', keras.metrics.CategoricalAccuracy()],
          distribute=distribution)

      interleaved_model = get_model()
      interleaved_model.set_weights(user_controlled_model.get_weights())
      interleaved_model.compile(
          gradient_descent.GradientDescentOptimizer(0.001),
          loss='mse',
          metrics=['mae', keras.metrics.CategoricalAccuracy()],
          distribute=distribution)

      dataset = get_dataset(distribution)

      # Call fit with validation interleaved
      interleaved_output = interleaved_model.fit(
          dataset, epochs=2, steps_per_epoch=2, verbose=1,
          validation_data=dataset, validation_steps=2, shuffle=False)

      # Manually control the validation running after each epoch.
      user_controlled_output = []
      for _ in range(2):
        user_controlled_model.fit(
            dataset, epochs=1, steps_per_epoch=2, verbose=1, shuffle=False)
        user_controlled_output.append(
            user_controlled_model.evaluate(dataset, steps=2))

      self.assertEqual(interleaved_output.history['val_loss'],
                       [x[0] for x in user_controlled_output])
      self.assertEqual(interleaved_output.history['val_mean_absolute_error'],
                       [x[1] for x in user_controlled_output])
      self.assertEqual(interleaved_output.history['val_categorical_accuracy'],
                       [x[2] for x in user_controlled_output])

  # TODO(priyag): Enable this test for TPU. Currently tuples/dict don't work
  # as clone_model's input_tensors argument only seems to accept list and not
  # tuples or dict.
  def test_fit_with_tuple_and_dict_dataset_inputs(self):
    with self.cached_session():
      model = multi_input_output_model()

      optimizer = gradient_descent.GradientDescentOptimizer(learning_rate=0.001)
      loss = 'mse'
      metrics = ['mae', keras.metrics.CategoricalAccuracy()]
      strategy = mirrored_strategy.MirroredStrategy(['/device:GPU:0',
                                                     '/device:CPU:0'])
      model.compile(optimizer, loss, metrics=metrics, distribute=strategy)

      input_a_np = np.random.random((10, 3))
      input_b_np = np.random.random((10, 5))
      output_d_np = np.random.random((10, 7))
      output_e_np = np.random.random((10, 7))

      # Test with tuples
      dataset_tuple = dataset_ops.Dataset.from_tensor_slices((
          (input_a_np, input_b_np), (output_d_np, output_e_np)))
      dataset_tuple = dataset_tuple.repeat(100)
      dataset_tuple = dataset_tuple.batch(10)

      model.fit(dataset_tuple, epochs=1, steps_per_epoch=2, verbose=1)

      # Test with dict
      dataset_dict = dataset_ops.Dataset.from_tensor_slices((
          {'input_a': input_a_np, 'input_b': input_b_np},
          (output_d_np, output_e_np)))
      dataset_dict = dataset_dict.repeat(100)
      dataset_dict = dataset_dict.batch(10)

      model.fit(dataset_dict, epochs=1, steps_per_epoch=2, verbose=1)

  @combinations.generate(strategy_combinations())
  def test_fit_eval_and_predict_methods_on_dataset(self, distribution):
    with self.cached_session():
      model = get_model()

      optimizer = gradient_descent.GradientDescentOptimizer(0.001)
      loss = 'mse'
      metrics = ['mae', keras.metrics.CategoricalAccuracy()]
      model.compile(optimizer, loss, metrics=metrics, distribute=distribution)

      dataset = get_dataset(distribution)

      model.fit(dataset, epochs=1, steps_per_epoch=2, verbose=1)
      model.evaluate(dataset, steps=2, verbose=1)
      model.predict(get_predict_dataset(distribution), steps=2)

  @combinations.generate(strategy_and_optimizer_combinations())
  def test_fit_eval_and_predict_with_optimizer(self, distribution, optimizer):
    with self.cached_session():
      model = get_model()

      loss = 'mse'
      model.compile(optimizer(), loss, distribute=distribution)

      dataset = get_dataset(distribution)

      model.fit(dataset, epochs=1, steps_per_epoch=2, verbose=1)
      model.evaluate(dataset, steps=2, verbose=1)
      model.predict(get_predict_dataset(distribution), steps=2)

  @combinations.generate(strategy_minus_tpu_combinations())
  def test_dataset_with_sample_weights(self, distribution):
    model = get_model()
    optimizer = rmsprop.RMSPropOptimizer(learning_rate=0.001)
    loss = 'mse'
    model.compile(optimizer, loss, distribute=distribution)

    inputs = np.zeros((10, 3), np.float32)
    targets = np.zeros((10, 4), np.float32)
    sample_weights = np.ones((10), np.float32)
    dataset = dataset_ops.Dataset.from_tensor_slices((inputs, targets,
                                                      sample_weights))
    dataset = dataset.repeat()
    dataset = dataset.batch(10)

    model.fit(dataset, epochs=1, steps_per_epoch=2, verbose=1)
    model.evaluate(dataset, steps=2, verbose=1)
    model.predict(dataset, steps=2)

  def test_dataset_input_shape_validation(self):
    with self.cached_session():
      model = get_model()

      optimizer = rmsprop.RMSPropOptimizer(learning_rate=0.001)
      loss = 'mse'
      strategy = mirrored_strategy.MirroredStrategy(['/device:GPU:1',
                                                     '/device:GPU:0'])

      model.compile(optimizer, loss, distribute=strategy)

      # User forgets to batch the dataset
      inputs = np.zeros((10, 3), dtype=np.float32)
      targets = np.zeros((10, 4), dtype=np.float32)
      dataset = dataset_ops.Dataset.from_tensor_slices((inputs, targets))
      dataset = dataset.repeat(100)

      with self.assertRaisesRegexp(ValueError, 'expected input to have shape'):
        model.fit(dataset, epochs=1, steps_per_epoch=2, verbose=0)

      # Wrong input shape
      inputs = np.zeros((10, 5), dtype=np.float32)
      targets = np.zeros((10, 4), dtype=np.float32)
      dataset = dataset_ops.Dataset.from_tensor_slices((inputs, targets))
      dataset = dataset.repeat(100)
      dataset = dataset.batch(10)

      with self.assertRaisesRegexp(ValueError,
                                   'expected input to have shape'):
        model.fit(dataset, epochs=1, steps_per_epoch=2, verbose=0)

  @combinations.generate(combinations.combine(
      distribution=[combinations.tpu_strategy_one_step],
      mode=['graph']))
  def test_dataset_input_shape_fully_defined(self, distribution):
    with self.cached_session():
      model = get_model()

      optimizer = rmsprop.RMSPropOptimizer(learning_rate=0.001)
      loss = 'mse'
      model.compile(optimizer, loss, distribute=distribution)

      dataset = get_dataset(distribution)
      # Input shapes are not fully known. Batch dimension is unknown as we are
      # not using the drop_remainder argument.
      dataset = dataset.repeat(100).batch(10)

      with self.assertRaisesRegexp(ValueError, 'requires fully defined shapes'):
        model.fit(dataset, epochs=1, steps_per_epoch=2, verbose=0)

  def test_learning_phase_value(self):
    # TODO(anjalisridhar): Modify this test to use Lambdas since we can compare
    # meaningful values. Currently we don't pass the learning phase if the
    # Lambda layer uses the learning phase.
    with self.cached_session():
      x = keras.layers.Input(shape=(1,), name='input')
      y = keras.layers.Dense(1, kernel_initializer='ones')(x)
      z = keras.layers.Dropout(0.9999)(y)
      model = keras.Model(x, z)
      initial_weights = model.get_weights()

      optimizer = gradient_descent.GradientDescentOptimizer(0.005)
      loss = 'mse'
      metrics = ['acc']
      strategy = mirrored_strategy.MirroredStrategy(
          ['/device:GPU:0', '/device:GPU:1'])

      model.compile(optimizer, loss, metrics=metrics, distribute=strategy)

      inputs = np.ones((10, 1), dtype=np.float32)
      targets = np.ones((10, 1), dtype=np.float32)
      dataset = dataset_ops.Dataset.from_tensor_slices((inputs, targets))
      dataset = dataset.repeat().batch(8)
      hist = model.fit(dataset, epochs=1, steps_per_epoch=20, verbose=1)
      self.assertAlmostEqual(hist.history['acc'][0], 0, 0)

      model.set_weights(initial_weights)
      # TODO(psv/anjalisridhar): Enable these lines after we fix b/117431185.
      # evaluate_output = model.evaluate(dataset, steps=20)
      # self.assertAlmostEqual(evaluate_output[1], 1, 0)

      inputs = np.ones((10, 1), dtype=np.float32)
      predict_dataset = dataset_ops.Dataset.from_tensor_slices(inputs)
      predict_dataset = predict_dataset.repeat().batch(5)
      output = model.predict(predict_dataset, steps=10)
      # `predict` runs for 10 steps and in each step you process 10 samples.
      ref_output = np.ones((100, 1), dtype=np.float32)
      self.assertArrayNear(output, ref_output, 1e-1)


class TestDistributionStrategyErrorCases(test.TestCase, parameterized.TestCase):

  def test_validating_dataset_input_tensors_with_shape_mismatch(self):
    with self.cached_session():
      strategy = mirrored_strategy.MirroredStrategy(['/device:GPU:0',
                                                     '/device:CPU:0'])
      a = constant_op.constant([1, 2], shape=(1, 2))
      b = constant_op.constant([[1, 2], [1, 2]], shape=(2, 2))
      x = values.DistributedValues({'/device:CPU:0': a, '/device:GPU:0': b})
      y = values.DistributedValues({'/device:CPU:0': a, '/device:GPU:0': a})
      with strategy.scope():
        # Removed device and input tensor shape details from the error message
        # since the order of the device and the corresponding input tensor shape
        # is not deterministic over different runs.
        with self.assertRaisesRegexp(ValueError,
                                     'Input tensor shapes do not match for '
                                     'distributed tensor inputs '
                                     'DistributedValues:.+'):
          distributed_training_utils.validate_distributed_dataset_inputs(
              strategy, x, y)

  def test_validating_dataset_input_tensors_with_dtype_mismatch(self):
    with self.cached_session():
      strategy = mirrored_strategy.MirroredStrategy(['/device:GPU:0',
                                                     '/device:CPU:0'])
      a = constant_op.constant([1, 2], shape=(1, 2), dtype=dtypes.int32)
      b = constant_op.constant([1, 2], shape=(1, 2), dtype=dtypes.float64)
      x = values.DistributedValues({'/device:CPU:0': a, '/device:GPU:0': b})
      y = values.DistributedValues({'/device:CPU:0': a, '/device:GPU:0': a})
      with strategy.scope():
        # Removed device and input tensor dtype details from the error message
        # since the order of the device and the corresponding input tensor dtype
        # is not deterministic over different runs.
        with self.assertRaisesRegexp(ValueError,
                                     'Input tensor dtypes do not match for '
                                     'distributed tensor inputs '
                                     'DistributedValues:.+'):
          distributed_training_utils.validate_distributed_dataset_inputs(
              strategy, x, y)

  def test_unsupported_features(self):
    with self.cached_session():
      model = get_model()

      optimizer = gradient_descent.GradientDescentOptimizer(0.001)
      loss = 'mse'
      metrics = ['mae']
      strategy = mirrored_strategy.MirroredStrategy(['/device:GPU:1',
                                                     '/device:GPU:0'])

      model.compile(optimizer, loss, metrics=metrics, distribute=strategy)

      dataset = get_dataset(strategy)

      # Test with validation split
      with self.assertRaisesRegexp(
          ValueError, '`validation_split` argument is not '
                      'supported when input `x` is a dataset or a '
                      'dataset iterator.+'):
        model.fit(dataset,
                  epochs=1, steps_per_epoch=2, verbose=0,
                  validation_split=0.5, validation_steps=2)

      # Test with sample weight.
      sample_weight = np.random.random((10,))
      with self.assertRaisesRegexp(
          ValueError, '`sample_weight` argument is not supported when input '
                      '`x` is a dataset or a dataset iterator.'):
        model.fit(
            dataset,
            epochs=1,
            steps_per_epoch=2,
            verbose=0,
            sample_weight=sample_weight)

      # Test with not specifying the `steps` argument.
      with self.assertRaisesRegexp(
          ValueError, 'you should specify the `steps_per_epoch` argument'):
        model.fit(dataset, epochs=1, verbose=0)
      with self.assertRaisesRegexp(ValueError,
                                   'you should specify the `steps` argument'):
        model.evaluate(dataset, verbose=0)

      with self.assertRaisesRegexp(ValueError,
                                   'you should specify the `steps` argument'):
        model.predict(dataset, verbose=0)

  def test_calling_with_unsupported_predefined_callbacks(self):
    with self.cached_session():
      model = get_model()

      optimizer = gradient_descent.GradientDescentOptimizer(0.001)
      loss = 'mse'
      metrics = ['mae']
      strategy = mirrored_strategy.MirroredStrategy(['/device:GPU:1',
                                                     '/device:GPU:0'])
      model.compile(optimizer, loss, metrics=metrics, distribute=strategy)

      dataset = get_dataset(strategy)

      def schedule(_):
        return 0.001
      with self.assertRaisesRegexp(ValueError,
                                   'LearningRateScheduler callback is not '
                                   'supported with DistributionStrategy.'):
        model.fit(dataset, epochs=1, steps_per_epoch=2, verbose=0,
                  callbacks=[keras.callbacks.LearningRateScheduler(schedule)])

      with self.assertRaisesRegexp(ValueError,
                                   'ReduceLROnPlateau callback is not '
                                   'supported with DistributionStrategy.'):
        model.fit(dataset, epochs=1, steps_per_epoch=2, verbose=0,
                  callbacks=[keras.callbacks.ReduceLROnPlateau()])
      with self.assertRaisesRegexp(ValueError,
                                   'histogram_freq in the TensorBoard callback '
                                   'is not supported when using '
                                   'DistributionStrategy.'):
        model.fit(dataset, epochs=1, steps_per_epoch=2, verbose=0,
                  callbacks=[keras.callbacks.TensorBoard(histogram_freq=10)])


class TestDistributionStrategyWithLossMasking(test.TestCase):

  # TODO(priyag): Enable all strategies for this test. Currently it does not
  # work for TPU due to some invalid datatype.
  def test_masking(self):
    with self.cached_session():
      np.random.seed(1337)
      x = np.array([[[1], [1]], [[0], [0]]])
      model = keras.models.Sequential()
      model.add(keras.layers.Masking(mask_value=0, input_shape=(2, 1)))
      model.add(
          keras.layers.TimeDistributed(
              keras.layers.Dense(1, kernel_initializer='one')))
      strategy = mirrored_strategy.MirroredStrategy(['/device:GPU:1',
                                                     '/device:GPU:0'])

      model.compile(loss='mse',
                    optimizer=gradient_descent.GradientDescentOptimizer(0.01),
                    distribute=strategy)
      y = np.array([[[1], [1]], [[1], [1]]])
      dataset = dataset_ops.Dataset.from_tensor_slices((x, y))
      dataset = dataset.repeat(100)
      dataset = dataset.batch(10)
      hist = model.fit(x=dataset, epochs=1, steps_per_epoch=2)
      self.assertEqual(hist.history['loss'][0], 0)


class TestDistributionStrategyWithNormalizationLayer(
    test.TestCase, parameterized.TestCase):

  @combinations.generate(strategy_combinations())
  def test_batchnorm_correctness(self, distribution):
    with self.cached_session():
      model = keras.models.Sequential()
      norm = keras.layers.BatchNormalization(input_shape=(10,), momentum=0.8)
      model.add(norm)
      model.compile(loss='mse',
                    optimizer=gradient_descent.GradientDescentOptimizer(0.01),
                    distribute=distribution)

      # centered on 5.0, variance 10.0
      x = np.random.normal(loc=5.0, scale=10.0, size=(1000, 10))
      x = x.astype('float32')
      dataset = dataset_ops.Dataset.from_tensor_slices((x, x))
      dataset = dataset.repeat(100)
      dataset = batch_wrapper(dataset, 32, distribution)

      predict_dataset = dataset_ops.Dataset.from_tensor_slices(x)
      predict_dataset = predict_dataset.repeat(100)
      predict_dataset = batch_wrapper(predict_dataset, 32, distribution)

      model.fit(dataset, epochs=4, verbose=0, steps_per_epoch=10)
      out = model.predict(predict_dataset, steps=2)
      out -= keras.backend.eval(norm.beta)
      out /= keras.backend.eval(norm.gamma)
      np.testing.assert_allclose(out.mean(), 0.0, atol=1e-1)
      np.testing.assert_allclose(out.std(), 1.0, atol=1e-1)


class TestDistributionStrategyCorrectness(test.TestCase,
                                          parameterized.TestCase):

  @combinations.generate(strategy_combinations())
  def test_metric_correctness(self, distribution):
    with self.cached_session():
      keras.backend.set_image_data_format('channels_last')
      num_samples = 10000

      x_train = np.random.randint(0, 2, num_samples)
      x_train = np.reshape(x_train, (num_samples, 1))
      y_train = x_train
      x_train = x_train.astype('float32')
      y_train = y_train.astype('float32')

      # Create identity model.
      model = keras.Sequential()
      model.add(
          keras.layers.Dense(1, input_shape=(1,), kernel_initializer='ones'))
      model.compile(
          loss=keras.losses.mean_squared_error,
          optimizer=gradient_descent.GradientDescentOptimizer(0.5),
          metrics=[keras.metrics.BinaryAccuracy()],
          distribute=distribution)

      batch_size = 64
      batch_size //= distribution.num_replicas_in_sync
      train_dataset = dataset_ops.Dataset.from_tensor_slices((x_train, y_train))
      train_dataset = batch_wrapper(train_dataset, batch_size, distribution)

      history = model.fit(x=train_dataset, epochs=1, steps_per_epoch=10)
      self.assertEqual(history.history['binary_accuracy'], [1.0])

  @combinations.generate(strategy_and_inputs())
  def test_correctness(self, distribution, use_numpy):
    with self.cached_session():
      tolerance = 1e-5

      if isinstance(distribution, mirrored_strategy.MirroredStrategy):
        # TODO(b/119257215): use the default one once the flakyness is fixed.
        tolerance = 1e-4

      keras.backend.set_image_data_format('channels_last')
      np.random.seed(_RANDOM_SEED)
      random_seed.set_random_seed(_RANDOM_SEED)

      # Train, eval, and predict datasets are created with the same input numpy
      # arrays.
      # TODO(xiejw): Change this back to 10000, once we support final partial
      # batch.
      num_samples = 9984
      x_train = np.random.rand(num_samples, 1)
      y_train = 3 * x_train
      x_train = x_train.astype('float32')
      y_train = y_train.astype('float32')
      x_predict = [[1.], [2.], [3.], [4.]]

      # The model is built once and the initial weights are saved.
      # This is used to initialize the model for both the distribution and
      # non-distribution run. In addition, we add few non-linear layers to make
      # it non-trivial.
      model = keras.Sequential()
      model.add(keras.layers.Dense(10, activation='relu', input_shape=(1,)))
      model.add(keras.layers.Dense(10, activation='relu'))
      model.add(keras.layers.Dense(10, activation='relu'))
      model.add(keras.layers.Dense(1))
      initial_weights = model.get_weights()

      def fit_and_predict(with_distribution=None):
        # We have initialized the model to the same weight for the distribution
        # and non-distribution run.
        model.set_weights(initial_weights)
        model.compile(
            loss=keras.losses.mean_squared_error,
            optimizer=gradient_descent.GradientDescentOptimizer(0.5),
            distribute=with_distribution)

        training_inputs, eval_inputs, predict_inputs = (
            get_correctness_test_inputs(use_numpy, with_distribution,
                                        x_train, y_train, x_predict))

        model.fit(**training_inputs)
        eval_result = model.evaluate(**eval_inputs)
        weights = model.get_weights()
        predict_result = model.predict(**predict_inputs)

        return weights, eval_result, predict_result

      wts_with_ds, eval_with_ds, predict_with_ds = fit_and_predict(
          with_distribution=distribution)
      wts_without_ds, eval_without_ds, predict_without_ds = fit_and_predict(
          with_distribution=None)

      # Verify that the weights, eval results, predict outputs  are the same
      # within some limits of tolerance.
      self.assertAllClose(
          wts_with_ds, wts_without_ds, atol=tolerance, rtol=tolerance)
      self.assertAllClose(
          eval_with_ds, eval_without_ds, atol=tolerance, rtol=tolerance)
      self.assertAllClose(
          predict_with_ds, predict_without_ds, atol=tolerance, rtol=tolerance)


# TODO(priyag): Add a test for TPUStrategy with steps_per_run > 1.


if __name__ == '__main__':
  test.main()
