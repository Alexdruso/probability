# Copyright 2018 The TensorFlow Probability Authors.
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
# ============================================================================
"""Tests for tfb.MaskedAutoregressiveFlow."""

import itertools

# Dependency imports

from absl.testing import parameterized
import numpy as np
import six
import tensorflow.compat.v1 as tf1
import tensorflow.compat.v2 as tf
from tensorflow_probability.python import bijectors as tfb
from tensorflow_probability.python import distributions as tfd
from tensorflow_probability.python import math as tfp_math
from tensorflow_probability.python.bijectors import masked_autoregressive
from tensorflow_probability.python.internal import prefer_static as ps
from tensorflow_probability.python.internal import tensorshape_util
from tensorflow_probability.python.internal import test_util

tfk = tf.keras
tfkl = tf.keras.layers


def _funnel_bijector_fn(x):
  """Funnel transform."""
  batch_shape = ps.shape(x)[:-1]
  ndims = 4
  scale = tf.concat(
      [
          tf.ones(ps.concat([batch_shape, [1]], axis=0)),
          tf.exp(x[..., :1] / 2) *
          tf.ones(ps.concat([batch_shape, [ndims - 1]], axis=0)),
      ],
      axis=-1,
  )
  return tfb.Scale(scale)


def _masked_autoregressive_2d_template(base_template, event_shape):

  def wrapper(x):
    x_flat = tf.reshape(
        x, ps.concat([ps.shape(x)[:-len(event_shape)], [-1]], -1))
    t = base_template(x_flat)
    if tf.is_tensor(t):
      x_shift, x_log_scale = tf.unstack(t, axis=-1)
    else:
      x_shift, x_log_scale = t
    return (tf.reshape(x_shift, ps.shape(x)),
            tf.reshape(x_log_scale, ps.shape(x)))

  return wrapper


def _masked_autoregressive_shift_and_log_scale_fn(hidden_units,
                                                  shift_only=False,
                                                  activation="relu",
                                                  name=None,
                                                  **kwargs):
  params = 1 if shift_only else 2
  layer = tfb.AutoregressiveNetwork(params, hidden_units=hidden_units,
                                    activation=activation, name=name, **kwargs)

  if shift_only:
    return lambda x: (layer(x)[..., 0], None)

  return layer


def _masked_autoregressive_gated_bijector_fn(hidden_units,
                                             activation="relu",
                                             name=None,
                                             **kwargs):
  layer = tfb.AutoregressiveNetwork(
      2, hidden_units=hidden_units, activation=activation, name=name, **kwargs)

  def _bijector_fn(x):
    if tensorshape_util.rank(x.shape) == 1:
      x = x[tf.newaxis, ...]
      reshape_output = lambda x: x[0]
    else:
      reshape_output = lambda x: x

    shift, logit_gate = tf.unstack(layer(x), axis=-1)
    shift = reshape_output(shift)
    logit_gate = reshape_output(logit_gate)
    gate = tf.nn.sigmoid(logit_gate)
    return tfb.Shift(shift=(1. - gate) * shift)(tfb.Scale(scale=gate))

  return _bijector_fn


@test_util.test_all_tf_execution_regimes
class GenMaskTest(test_util.TestCase):

  def test346Exclusive(self):
    expected_mask = np.array(
        [[0, 0, 0, 0],
         [0, 0, 0, 0],
         [1, 0, 0, 0],
         [1, 0, 0, 0],
         [1, 1, 0, 0],
         [1, 1, 0, 0]])
    mask = masked_autoregressive._gen_mask(
        num_blocks=3, n_in=4, n_out=6, mask_type="exclusive")
    self.assertAllEqual(expected_mask, mask)

  def test346Inclusive(self):
    expected_mask = np.array(
        [[1, 0, 0, 0],
         [1, 0, 0, 0],
         [1, 1, 0, 0],
         [1, 1, 0, 0],
         [1, 1, 1, 0],
         [1, 1, 1, 0]])
    mask = masked_autoregressive._gen_mask(
        num_blocks=3, n_in=4, n_out=6, mask_type="inclusive")
    self.assertAllEqual(expected_mask, mask)


class MakeDenseAutoregressiveMasksTest(test_util.TestCase):

  def testRandomMade(self):
    hidden_size = 8
    num_hidden = 3
    params = 2
    event_size = 4

    def random_made(x):
      masks = masked_autoregressive._make_dense_autoregressive_masks(
          params=params,
          event_size=event_size,
          hidden_units=[hidden_size] * num_hidden)
      output_sizes = [hidden_size] * num_hidden
      input_size = event_size
      for (mask, output_size) in zip(masks, output_sizes):
        mask = tf.cast(mask, tf.float32)
        x = tf.matmul(
            x,
            np.random.randn(input_size, output_size).astype(np.float32) * mask)
        x = tf.nn.relu(x)
        input_size = output_size
      x = tf.matmul(
          x,
          np.random.randn(input_size, params * event_size).astype(np.float32) *
          masks[-1])
      x = tf.reshape(x, [-1, event_size, params])
      return x

    y = random_made(tf.zeros([1, event_size]))
    self.assertEqual([1, event_size, params], y.shape)

  def testLeftToRight(self):
    masks = masked_autoregressive._make_dense_autoregressive_masks(
        params=2,
        event_size=3,
        hidden_units=[4, 4],
        input_order="left-to-right",
        hidden_degrees="equal")

    self.assertLen(masks, 3)
    self.assertAllEqual([
        [1, 1, 1, 1],
        [0, 0, 1, 1],
        [0, 0, 0, 0],
    ], masks[0])

    self.assertAllEqual([
        [1, 1, 1, 1],
        [1, 1, 1, 1],
        [0, 0, 1, 1],
        [0, 0, 1, 1],
    ], masks[1])

    self.assertAllEqual([
        [0, 0, 1, 1, 1, 1],
        [0, 0, 1, 1, 1, 1],
        [0, 0, 0, 0, 1, 1],
        [0, 0, 0, 0, 1, 1],
    ], masks[2])

  def testRandom(self):
    masks = masked_autoregressive._make_dense_autoregressive_masks(
        params=2,
        event_size=3,
        hidden_units=[4, 4],
        input_order="random",
        hidden_degrees="random",
        seed=1)

    self.assertLen(masks, 3)
    self.assertAllEqual([
        [1, 0, 1, 1],
        [0, 0, 0, 0],
        [1, 1, 1, 1],
    ], masks[0])

    self.assertAllEqual([
        [1, 0, 1, 1],
        [1, 1, 1, 1],
        [1, 0, 1, 1],
        [1, 0, 1, 1],
    ], masks[1])

    self.assertAllEqual([
        [0, 0, 1, 1, 0, 0],
        [1, 1, 1, 1, 0, 0],
        [0, 0, 1, 1, 0, 0],
        [0, 0, 1, 1, 0, 0],
    ], masks[2])

  def testRightToLeft(self):
    masks = masked_autoregressive._make_dense_autoregressive_masks(
        params=2,
        event_size=3,
        hidden_units=[4, 4],
        input_order=list(reversed(range(1, 4))),
        hidden_degrees="equal")

    self.assertLen(masks, 3)
    self.assertAllEqual([
        [0, 0, 0, 0],
        [0, 0, 1, 1],
        [1, 1, 1, 1],
    ], masks[0])

    self.assertAllEqual([
        [1, 1, 1, 1],
        [1, 1, 1, 1],
        [0, 0, 1, 1],
        [0, 0, 1, 1],
    ], masks[1])

    self.assertAllEqual([
        [1, 1, 1, 1, 0, 0],
        [1, 1, 1, 1, 0, 0],
        [1, 1, 0, 0, 0, 0],
        [1, 1, 0, 0, 0, 0],
    ], masks[2])

  def testUneven(self):
    masks = masked_autoregressive._make_dense_autoregressive_masks(
        params=2,
        event_size=3,
        hidden_units=[5, 3],
        input_order="left-to-right",
        hidden_degrees="equal")

    self.assertLen(masks, 3)
    self.assertAllEqual([
        [1, 1, 1, 1, 1],
        [0, 0, 0, 1, 1],
        [0, 0, 0, 0, 0],
    ], masks[0])

    self.assertAllEqual([
        [1, 1, 1],
        [1, 1, 1],
        [1, 1, 1],
        [0, 0, 1],
        [0, 0, 1],
    ], masks[1])

    self.assertAllEqual([
        [0, 0, 1, 1, 1, 1],
        [0, 0, 1, 1, 1, 1],
        [0, 0, 0, 0, 1, 1],
    ], masks[2])


@test_util.test_all_tf_execution_regimes
class _MaskedAutoregressiveFlowTest(test_util.VectorDistributionTestHelpers,
                                    test_util.TestCase):

  event_shape = [4]

  @property
  def _autoregressive_flow_kwargs(self):
    return {
        "shift_and_log_scale_fn":
            tfb.masked_autoregressive_default_template(
                hidden_layers=[2], shift_only=False),
        "is_constant_jacobian":
            False,
    }

  def testNonBatchedBijector(self):
    x_ = np.arange(np.prod(self.event_shape)).astype(
        np.float32).reshape(self.event_shape)
    ma = tfb.MaskedAutoregressiveFlow(
        validate_args=True, **self._autoregressive_flow_kwargs)
    x = tf.constant(x_)
    forward_x = ma.forward(x)
    # Use identity to invalidate cache.
    inverse_y = ma.inverse(tf.identity(forward_x))
    forward_inverse_y = ma.forward(inverse_y)
    fldj = ma.forward_log_det_jacobian(x, event_ndims=len(self.event_shape))
    # Use identity to invalidate cache.
    ildj = ma.inverse_log_det_jacobian(
        tf.identity(forward_x), event_ndims=len(self.event_shape))
    self.evaluate(tf1.global_variables_initializer())
    [
        forward_x_,
        inverse_y_,
        forward_inverse_y_,
        ildj_,
        fldj_,
    ] = self.evaluate([
        forward_x,
        inverse_y,
        forward_inverse_y,
        ildj,
        fldj,
    ])
    self.assertStartsWith(ma.name, "masked_autoregressive_flow")
    self.assertAllClose(forward_x_, forward_inverse_y_, rtol=1e-6, atol=0.)
    self.assertAllClose(x_, inverse_y_, rtol=1e-4, atol=0.)
    self.assertAllClose(ildj_, -fldj_, rtol=1e-6, atol=0.)

  def testBatchedBijector(self):
    x_ = np.arange(4 * np.prod(self.event_shape)).astype(
        np.float32).reshape([4] + self.event_shape) / 10.
    ma = tfb.MaskedAutoregressiveFlow(
        validate_args=True, **self._autoregressive_flow_kwargs)
    x = tf.constant(x_)
    forward_x = ma.forward(x)
    # Use identity to invalidate cache.
    inverse_y = ma.inverse(tf.identity(forward_x))
    forward_inverse_y = ma.forward(inverse_y)
    fldj = ma.forward_log_det_jacobian(x, event_ndims=len(self.event_shape))
    # Use identity to invalidate cache.
    ildj = ma.inverse_log_det_jacobian(
        tf.identity(forward_x), event_ndims=len(self.event_shape))
    self.evaluate(tf1.global_variables_initializer())
    [
        forward_x_,
        inverse_y_,
        forward_inverse_y_,
        ildj_,
        fldj_,
    ] = self.evaluate([
        forward_x,
        inverse_y,
        forward_inverse_y,
        ildj,
        fldj,
    ])
    self.assertStartsWith(ma.name, "masked_autoregressive_flow")
    self.assertAllClose(forward_x_, forward_inverse_y_, rtol=1e-6, atol=1e-6)
    self.assertAllClose(x_, inverse_y_, rtol=1e-4, atol=1e-4)
    self.assertAllClose(ildj_, -fldj_, rtol=1e-6, atol=1e-6)

  @test_util.numpy_disable_gradient_test
  def testGradients(self):
    maf = tfb.MaskedAutoregressiveFlow(
        validate_args=True, **self._autoregressive_flow_kwargs)

    def _transform(x):
      y = maf.forward(x)
      return maf.inverse(tf.identity(y))

    self.evaluate(tf1.global_variables_initializer())
    _, gradient = tfp_math.value_and_gradient(_transform,
                                              tf.zeros(self.event_shape))
    self.assertIsNotNone(gradient)

  def testMutuallyConsistent(self):
    maf = tfb.MaskedAutoregressiveFlow(
        validate_args=True, **self._autoregressive_flow_kwargs)
    base = tfd.Independent(
        tfd.Normal(loc=tf.zeros(self.event_shape), scale=1.),
        reinterpreted_batch_ndims=len(self.event_shape))
    reshape = tfb.Reshape(
        event_shape_out=[np.prod(self.event_shape)],
        event_shape_in=self.event_shape)
    bijector = tfb.Chain([reshape, maf])
    dist = tfd.TransformedDistribution(
        distribution=base, bijector=bijector, validate_args=True)
    self.run_test_sample_consistent_log_prob(
        sess_run_fn=self.evaluate,
        dist=dist,
        num_samples=int(1e6),
        radius=1.,
        center=0.,
        rtol=0.025)

  def testInvertMutuallyConsistent(self):
    maf = tfb.Invert(
        tfb.MaskedAutoregressiveFlow(
            validate_args=True, **self._autoregressive_flow_kwargs))
    base = tfd.Independent(
        tfd.Normal(loc=tf.zeros(self.event_shape), scale=1.),
        reinterpreted_batch_ndims=len(self.event_shape))
    reshape = tfb.Reshape(
        event_shape_out=[np.prod(self.event_shape)],
        event_shape_in=self.event_shape)
    bijector = tfb.Chain([reshape, maf])
    dist = tfd.TransformedDistribution(
        distribution=base, bijector=bijector, validate_args=True)

    self.run_test_sample_consistent_log_prob(
        sess_run_fn=self.evaluate,
        dist=dist,
        num_samples=int(1e6),
        radius=1.,
        center=0.,
        rtol=0.03)

  def testVectorBijectorRaises(self):
    with self.assertRaisesRegexp(
        ValueError,
        "Bijectors with `forward_min_event_ndims` > 0 are not supported"):

      def bijector_fn(*args, **kwargs):
        del args, kwargs
        return tfb.Inline(forward_min_event_ndims=1)

      maf = tfb.MaskedAutoregressiveFlow(
          bijector_fn=bijector_fn, validate_args=True)
      maf.forward([1., 2.])

  def testRankChangingBijectorRaises(self):
    with self.assertRaisesRegexp(
        ValueError, "Bijectors which alter `event_ndims` are not supported."):

      def bijector_fn(*args, **kwargs):
        del args, kwargs
        return tfb.Inline(forward_min_event_ndims=0, inverse_min_event_ndims=1)

      maf = tfb.MaskedAutoregressiveFlow(
          bijector_fn=bijector_fn, validate_args=True)
      maf.forward([1., 2.])


@test_util.numpy_disable_test_missing_functionality("tf.make_template")
@test_util.jax_disable_test_missing_functionality("tf.make_template")
@test_util.test_all_tf_execution_regimes
class MaskedAutoregressiveFlowTest(_MaskedAutoregressiveFlowTest):
  pass


@test_util.numpy_disable_test_missing_functionality("Keras")
@test_util.jax_disable_test_missing_functionality("Keras")
@test_util.test_all_tf_execution_regimes
class MaskedAutoregressiveFlowShiftOnlyTest(_MaskedAutoregressiveFlowTest):

  @property
  def _autoregressive_flow_kwargs(self):
    return {
        "shift_and_log_scale_fn":
            tfb.masked_autoregressive_default_template(
                hidden_layers=[2], shift_only=True),
        "is_constant_jacobian":
            True,
    }


@test_util.numpy_disable_test_missing_functionality("Keras")
@test_util.jax_disable_test_missing_functionality("Keras")
@test_util.test_all_tf_execution_regimes
class MaskedAutoregressiveFlowShiftOnlyLayerTest(_MaskedAutoregressiveFlowTest):

  @property
  def _autoregressive_flow_kwargs(self):
    return {
        "shift_and_log_scale_fn":
            _masked_autoregressive_shift_and_log_scale_fn(
                hidden_units=[2], shift_only=True),
        "is_constant_jacobian":
            True,
    }


@test_util.numpy_disable_test_missing_functionality("tf.make_template")
@test_util.jax_disable_test_missing_functionality("tf.make_template")
@test_util.test_all_tf_execution_regimes
class MaskedAutoregressiveFlowUnrollLoopTest(_MaskedAutoregressiveFlowTest):

  @property
  def _autoregressive_flow_kwargs(self):
    return {
        "shift_and_log_scale_fn":
            tfb.masked_autoregressive_default_template(
                hidden_layers=[2], shift_only=False),
        "is_constant_jacobian":
            False,
        "unroll_loop":
            True,
    }


@test_util.numpy_disable_test_missing_functionality("Keras")
@test_util.jax_disable_test_missing_functionality("Keras")
@test_util.test_all_tf_execution_regimes
class MaskedAutoregressiveFlowUnrollLoopLayerTest(_MaskedAutoregressiveFlowTest
                                                 ):

  @property
  def _autoregressive_flow_kwargs(self):
    return {
        "shift_and_log_scale_fn":
            _masked_autoregressive_shift_and_log_scale_fn(
                hidden_units=[10, 10], activation="relu"),
        "is_constant_jacobian":
            False,
        "unroll_loop":
            True,
    }


@test_util.numpy_disable_test_missing_functionality("Keras")
@test_util.jax_disable_test_missing_functionality("Keras")
@test_util.test_all_tf_execution_regimes
class MaskedAutoregressive2DTest(_MaskedAutoregressiveFlowTest):
  event_shape = [3, 2]

  @property
  def _autoregressive_flow_kwargs(self):
    return {
        "shift_and_log_scale_fn":
            _masked_autoregressive_2d_template(
                tfb.masked_autoregressive_default_template(
                    hidden_layers=[np.prod(self.event_shape)],
                    shift_only=False), self.event_shape),
        "is_constant_jacobian":
            False,
        "event_ndims":
            2,
    }


@test_util.numpy_disable_test_missing_functionality("Keras")
@test_util.jax_disable_test_missing_functionality("Keras")
@test_util.test_all_tf_execution_regimes
class MaskedAutoregressiveGatedTest(_MaskedAutoregressiveFlowTest):

  @property
  def _autoregressive_flow_kwargs(self):
    return {
        "bijector_fn":
            _masked_autoregressive_gated_bijector_fn(
                hidden_units=[10, 10], activation="relu"),
        "is_constant_jacobian":
            False,
    }


@test_util.numpy_disable_test_missing_functionality("Keras")
@test_util.jax_disable_test_missing_functionality("Keras")
@test_util.test_all_tf_execution_regimes
class MaskedAutoregressive2DLayerTest(_MaskedAutoregressiveFlowTest):
  event_shape = [3, 2]

  @property
  def _autoregressive_flow_kwargs(self):
    return {
        "shift_and_log_scale_fn":
            _masked_autoregressive_2d_template(
                _masked_autoregressive_shift_and_log_scale_fn(
                    hidden_units=[np.prod(self.event_shape)],
                    shift_only=False), self.event_shape),
        "is_constant_jacobian":
            False,
        "event_ndims":
            2,
    }


@test_util.test_all_tf_execution_regimes
class MaskedAutoregressiveFunnelTest(_MaskedAutoregressiveFlowTest):

  @property
  def _autoregressive_flow_kwargs(self):
    return {
        "bijector_fn":
            _funnel_bijector_fn,
        "is_constant_jacobian":
            False,
    }


@test_util.numpy_disable_test_missing_functionality("Keras")
@test_util.jax_disable_test_missing_functionality("Keras")
@test_util.test_all_tf_execution_regimes
class AutoregressiveNetworkTest(test_util.TestCase):

  def _count_trainable_params(self, layer):
    ret = 0
    for w in layer.trainable_weights:
      ret += np.prod(w.shape)
    return ret

  def assertIsAutoregressive(self, f, event_size, order):
    input_order = None
    if isinstance(order, six.string_types):
      if order == "left-to-right":
        input_order = range(event_size)
      elif order == "right-to-left":
        input_order = range(event_size - 1, -1, -1)
    elif np.all(np.sort(order) == np.arange(1, event_size + 1)):
      input_order = list(np.array(order) - 1)
    if input_order is None:
      raise ValueError("Invalid input order: '{}'.".format(order))

    # Test that if we change dimension `i` of the input, then the only changed
    # dimensions `j` of the output are those with larger `input_order[j]`.
    # (We could also do this by examining gradients.)
    diff = []
    mask = []
    for i in range(event_size):
      x = np.random.randn(event_size)
      delta = np.zeros(event_size)
      delta[i] = np.random.randn()
      diff = self.evaluate(f(x + delta) - f(x))
      mask = [[input_order[i] >= input_order[j]] for j in range(event_size)]
      self.assertAllClose(np.zeros_like(diff), mask * diff, atol=0., rtol=1e-6)

  def test_layer_right_to_left_float64(self):
    made = tfb.AutoregressiveNetwork(
        params=3, event_shape=4, activation=None, input_order="right-to-left",
        dtype=tf.float64, hidden_degrees="random", hidden_units=[10, 7, 10])
    self.assertEqual((4, 3), made(np.zeros(4, dtype=np.float64)).shape)
    self.assertEqual("float64", made(np.zeros(4, dtype=np.float64)).dtype)
    self.assertEqual(5 * 10 + 11 * 7 + 8 * 10 + 11 * 12,
                     self._count_trainable_params(made))
    if not tf.executing_eagerly():
      self.evaluate(
          tf1.initializers.variables(made.trainable_variables))
    self.assertIsAutoregressive(made, event_size=4, order="right-to-left")

  def test_layer_callable_activation(self):
    made = tfb.AutoregressiveNetwork(
        params=2, activation=tf.math.exp, input_order="random",
        kernel_regularizer=tfk.regularizers.l2(0.1), bias_initializer="ones",
        hidden_units=[9], hidden_degrees="equal")
    self.assertEqual((3, 5, 2), made(np.zeros((3, 5))).shape)
    self.assertEqual(6 * 9 + 10 * 10, self._count_trainable_params(made))
    if not tf.executing_eagerly():
      self.evaluate(
          tf1.initializers.variables(made.trainable_variables))
    self.assertIsAutoregressive(made, event_size=5, order=made._input_order)

  def test_layer_smaller_hidden_layers_than_input(self):
    made = tfb.AutoregressiveNetwork(
        params=1, event_shape=9, activation="relu", use_bias=False,
        bias_regularizer=tfk.regularizers.l1(0.5), bias_constraint=tf.math.abs,
        input_order="right-to-left", hidden_units=[5, 5])
    self.assertEqual((9, 1), made(np.zeros(9)).shape)
    self.assertEqual(9 * 5 + 5 * 5 + 5 * 9, self._count_trainable_params(made))
    if not tf.executing_eagerly():
      self.evaluate(
          tf1.initializers.variables(made.trainable_variables))
    self.assertIsAutoregressive(made, event_size=9, order="right-to-left")

  def test_layer_no_hidden_units(self):
    made = tfb.AutoregressiveNetwork(
        params=4, event_shape=3, use_bias=False, hidden_degrees="random",
        kernel_constraint="unit_norm")
    self.assertEqual((2, 2, 5, 3, 4), made(np.zeros((2, 2, 5, 3))).shape)
    self.assertEqual(3 * 12, self._count_trainable_params(made))
    if not tf.executing_eagerly():
      self.evaluate(
          tf1.initializers.variables(made.trainable_variables))
    self.assertIsAutoregressive(made, event_size=3, order="left-to-right")

  def test_layer_v2_kernel_initializer(self):
    init = tf.keras.initializers.GlorotNormal()
    made = tfb.AutoregressiveNetwork(
        params=2, event_shape=4, activation="relu",
        hidden_units=[5, 5], kernel_initializer=init)
    self.assertEqual((4, 2), made(np.zeros(4)).shape)
    self.assertEqual(5 * 5 + 6 * 5 + 6 * 8, self._count_trainable_params(made))
    if not tf.executing_eagerly():
      self.evaluate(
          tf1.initializers.variables(made.trainable_variables))
    self.assertIsAutoregressive(made, event_size=4, order="left-to-right")

  def test_doc_string(self):
    # Generate data.
    n = 2000
    x2 = np.random.randn(n).astype(dtype=np.float32) * 2.
    x1 = np.random.randn(n).astype(dtype=np.float32) + (x2 * x2 / 4.)
    data = np.stack([x1, x2], axis=-1)

    # Density estimation with MADE.
    made = tfb.AutoregressiveNetwork(params=2, hidden_units=[10, 10])

    distribution = tfd.TransformedDistribution(
        distribution=tfd.Sample(tfd.Normal(0., 1.), [2]),
        bijector=tfb.MaskedAutoregressiveFlow(made))

    # Construct and fit model.
    x_ = tfkl.Input(shape=(2,), dtype=tf.float32)
    log_prob_ = distribution.log_prob(x_)
    model = tfk.Model(x_, log_prob_)

    model.compile(optimizer=tf1.train.AdamOptimizer(),
                  loss=lambda _, log_prob: -log_prob)

    batch_size = 25
    model.fit(x=data,
              y=np.zeros((n, 0), dtype=np.float32),
              batch_size=batch_size,
              epochs=1,
              steps_per_epoch=1,  # Usually `n // batch_size`.
              shuffle=True,
              verbose=True)

    # Use the fitted distribution.
    self.assertAllEqual((3, 1, 2), distribution.sample((3, 1)).shape)
    self.assertAllEqual(
        (3,), distribution.log_prob(np.ones((3, 2), dtype=np.float32)).shape)

  def test_doc_string_2(self):
    n = 2000
    c = np.r_[np.zeros(n // 2), np.ones(n // 2)]
    mean_0, mean_1 = 0, 5
    x = np.r_[np.random.randn(n // 2).astype(dtype=np.float32) + mean_0,
              np.random.randn(n // 2).astype(dtype=np.float32) + mean_1]
    shuffle_idxs = np.arange(n)
    np.random.shuffle(shuffle_idxs)
    x = x[shuffle_idxs]
    c = c[shuffle_idxs]

    seed = test_util.test_seed_stream()

    # Density estimation with MADE.
    made = tfb.AutoregressiveNetwork(
        params=2,
        hidden_units=[1],
        event_shape=(1,),
        kernel_initializer=tfk.initializers.VarianceScaling(
            0.1, seed=seed() % 2**31),
        conditional=True,
        conditional_event_shape=(1,))

    distribution = tfd.TransformedDistribution(
        distribution=tfd.Sample(tfd.Normal(loc=0., scale=1.), sample_shape=[1]),
        bijector=tfb.MaskedAutoregressiveFlow(made))

    # Construct and fit model.
    x_ = tfkl.Input(shape=(1,), dtype=tf.float32)
    c_ = tfkl.Input(shape=(1,), dtype=tf.float32)
    log_prob_ = distribution.log_prob(
        x_, bijector_kwargs={"conditional_input": c_})
    model = tfk.Model([x_, c_], log_prob_)

    model.compile(
        optimizer=tf.optimizers.Adam(learning_rate=0.1),
        loss=lambda _, log_prob: -log_prob)

    batch_size = 25
    model.fit(
        x=[x, c],
        y=np.zeros((n, 0), dtype=np.float32),
        batch_size=batch_size,
        epochs=3,
        steps_per_epoch=n // batch_size,
        shuffle=False,
        verbose=True)

    # Use the fitted distribution to sample condition on c = 1
    n_samples = 1000
    cond = 1
    samples = distribution.sample(
        (n_samples,),
        bijector_kwargs={"conditional_input": cond * np.ones((n_samples, 1))},
        seed=seed())
    # Assert mean is close to conditional mean
    self.assertAllMeansClose(samples[..., 0], mean_1, axis=0, atol=1.)

  def test_doc_string_images_case_1(self):
    # Generate fake images.
    images = np.random.choice([0, 1], size=(100, 8, 8, 3))
    n, width, height, channels = images.shape

    # Reshape images to achieve desired autoregressivity.
    event_shape = [width * height * channels]
    reshaped_images = np.reshape(images, [n, width * height * channels])

    made = tfb.AutoregressiveNetwork(params=1, event_shape=event_shape,
                                     hidden_units=[20, 20], activation="relu")

    # Density estimation with MADE.
    #
    # NOTE: Parameterize an autoregressive distribution over an event_shape of
    # [width * height * channels], with univariate Bernoulli conditional
    # distributions.
    distribution = tfd.Autoregressive(
        lambda x: tfd.Independent(  # pylint: disable=g-long-lambda
            tfd.Bernoulli(logits=tf.unstack(made(x), axis=-1)[0],
                          dtype=tf.float32),
            reinterpreted_batch_ndims=1),
        sample0=tf.zeros(event_shape, dtype=tf.float32))

    # Construct and fit model.
    x_ = tfkl.Input(shape=event_shape, dtype=tf.float32)
    log_prob_ = distribution.log_prob(x_)
    model = tfk.Model(x_, log_prob_)

    model.compile(optimizer=tf1.train.AdamOptimizer(),
                  loss=lambda _, log_prob: -log_prob)

    batch_size = 10
    model.fit(x=reshaped_images,
              y=np.zeros((n, 0), dtype=np.float32),
              batch_size=batch_size,
              epochs=1,
              steps_per_epoch=1,  # Usually `n // batch_size`.
              shuffle=True,
              verbose=True)

    # Use the fitted distribution.
    self.assertAllEqual(event_shape, distribution.sample().shape)
    self.assertAllEqual((n,), distribution.log_prob(reshaped_images).shape)

  def test_doc_string_images_case_2(self):
    # Generate fake images.
    images = np.random.choice([0, 1], size=(100, 8, 8, 3))
    n, width, height, channels = images.shape

    # Reshape images to achieve desired autoregressivity.
    reshaped_images = np.transpose(
        np.reshape(images, [n, width * height, channels]),
        axes=[0, 2, 1])

    made = tfb.AutoregressiveNetwork(params=1, event_shape=[width * height],
                                     hidden_units=[20, 20], activation="relu")

    # Density estimation with MADE.
    #
    # NOTE: Parameterize an autoregressive distribution over an event_shape of
    # [channels, width * height], with univariate Bernoulli conditional
    # distributions.
    distribution = tfd.Autoregressive(
        lambda x: tfd.Independent(  # pylint: disable=g-long-lambda
            tfd.Bernoulli(logits=tf.unstack(made(x), axis=-1)[0],
                          dtype=tf.float32),
            reinterpreted_batch_ndims=2),
        sample0=tf.zeros([channels, width * height], dtype=tf.float32))

    # Construct and fit model.
    x_ = tfkl.Input(shape=(channels, width * height), dtype=tf.float32)
    log_prob_ = distribution.log_prob(x_)
    model = tfk.Model(x_, log_prob_)

    model.compile(optimizer=tf1.train.AdamOptimizer(),
                  loss=lambda _, log_prob: -log_prob)

    batch_size = 10
    model.fit(x=reshaped_images,
              y=np.zeros((n, 0), dtype=np.float32),
              batch_size=batch_size,
              epochs=1,
              steps_per_epoch=1,  # Usually `n // batch_size`.
              shuffle=True,
              verbose=True)

    # Use the fitted distribution.
    self.assertAllEqual((7, channels, width * height),
                        distribution.sample(7).shape)
    self.assertAllEqual((n,), distribution.log_prob(reshaped_images).shape)


@test_util.numpy_disable_test_missing_functionality("Keras")
@test_util.jax_disable_test_missing_functionality("Keras")
@test_util.test_all_tf_execution_regimes
class ConditionalTests(test_util.TestCase):

  def test_conditional_missing_event_shape(self):
    with self.assertRaisesRegexp(
        ValueError,
        "`event_shape` must be provided when `conditional` is True"):

      tfb.AutoregressiveNetwork(
          params=2, conditional=True, conditional_event_shape=[4])

  def test_conditional_missing_conditional_shape(self):
    with self.assertRaisesRegexp(
        ValueError,
        "`conditional_event_shape` must be provided when `conditional` is True"
    ):

      tfb.AutoregressiveNetwork(params=2, conditional=True, event_shape=[4])

  def test_conditional_incorrect_layers(self):
    with self.assertRaisesRegexp(
        ValueError,
        "`conditional_input_layers` must be \"first_layers\" or \"all_layers\""
    ):

      tfb.AutoregressiveNetwork(
          params=2,
          conditional=True,
          event_shape=[4],
          conditional_event_shape=[4],
          conditional_input_layers="non-existent-option")

  def test_conditional_false_with_shape(self):
    with self.assertRaisesRegexp(
        ValueError,
        "`conditional_event_shape` passed but `conditional` is set to False."):

      tfb.AutoregressiveNetwork(params=2, conditional_event_shape=[4])

  def test_conditional_wrong_shape(self):
    with self.assertRaisesRegexp(
        ValueError,
        "Parameter `conditional_event_shape` must describe a rank-1 shape"):

      tfb.AutoregressiveNetwork(
          params=2,
          conditional=True,
          event_shape=[4],
          conditional_event_shape=[10, 4])

  def test_conditional_missing_tensor(self):
    with self.assertRaisesRegexp(
        ValueError, "`conditional_input` must be passed as a named argument"):

      made = tfb.AutoregressiveNetwork(
          params=2,
          event_shape=[4],
          conditional=True,
          conditional_event_shape=[6])

      made(np.random.normal(0, 1, (1, 4)))

  @parameterized.named_parameters(
      *(("{} {}".format(ns, cs), ns, cs) for ns, cs in itertools.product(
          [[3], [1, 3], [2, 3], [1, 2, 3], [2, 1, 3], [2, 2, 3]],
          [[4], [1, 4], [2, 4], [1, 2, 4], [2, 1, 4], [2, 2, 4]],
      )))
  def test_conditional_broadcasting(self, input_shape, cond_shape):
    made = tfb.AutoregressiveNetwork(
        params=2,
        event_shape=[3],
        conditional=True,
        conditional_event_shape=[4])

    made_shape = tf.shape(
        made(tf.ones(input_shape), conditional_input=tf.ones(cond_shape)))
    broadcast_shape = tf.concat(
        [
            tf.broadcast_dynamic_shape(cond_shape[:-1], input_shape[:-1]),
            input_shape[-1:]
        ],
        axis=0,
    )
    self.assertAllEqual(
        self.evaluate(tf.concat([broadcast_shape, [2]], axis=0)), made_shape)


del _MaskedAutoregressiveFlowTest

if __name__ == "__main__":
  test_util.main()
