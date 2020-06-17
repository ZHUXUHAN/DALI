# Copyright (c) 2019, NVIDIA CORPORATION. All rights reserved.
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

from nvidia.dali.pipeline import Pipeline
import nvidia.dali.ops as ops
import nvidia.dali.types as types
import nvidia.dali as dali
from nvidia.dali.backend_impl import TensorListGPU
import numpy as np
from numpy.testing import assert_array_equal, assert_allclose
import os
from functools import partial
from test_utils import check_batch
from test_utils import compare_pipelines
from test_utils import get_dali_extra_path
from test_utils import RandomDataIterator
from math import floor
from nose.tools import assert_raises

test_data_root = get_dali_extra_path()
caffe_db_folder = os.path.join(test_data_root, 'db', 'lmdb')
test_data_video = os.path.join(test_data_root, 'db', 'optical_flow', 'sintel_trailer')

#std::round has different behaviour than np.round so manually add 0.5 and truncate to int
def roundint(num):
    return int(np.float32(num) + (0.5 if np.float32(num) >= 0 else -0.5))

def abs_slice_start_and_end(in_shape, slice_anchor, slice_shape, normalized_anchor, normalized_shape):
    ndim = len(in_shape)
    if normalized_anchor and normalized_shape:
        start = [roundint(np.float32(in_shape[i]) * np.float32(slice_anchor[i])) for i in range(ndim)]
        end = [roundint(np.float32(in_shape[i]) * np.float32(slice_anchor[i]+slice_shape[i])) for i in range(ndim)]
    else:
        if normalized_anchor:
            start = [roundint(np.float32(in_shape[i]) * np.float32(slice_anchor[i])) for i in range(ndim)]
        else:
            start = [roundint(np.float32(slice_anchor[i])) for i in range(ndim)]

        if normalized_shape:
            end = [start[i] + roundint(np.float32(in_shape[i]) * np.float32(slice_shape[i])) for i in range(ndim)]
        else:
            end = [start[i] + roundint(np.float32(slice_shape[i])) for i in range(ndim)]
    out_shape = [end[i]-start[i] for i in range(ndim)]
    return start, end, out_shape

class SliceSynthDataPipeline(Pipeline):
    def __init__(self, device, batch_size, layout, iterator, pos_size_iter,
                 num_threads=1, device_id=0, num_gpus=1,
                 axes=None, axis_names=None, normalized_anchor=True, normalized_shape=True,
                 extra_outputs=False, out_of_bounds_policy=None, fill_values=None):
        super(SliceSynthDataPipeline, self).__init__(
            batch_size, num_threads, device_id, seed=1234)
        self.device = device
        self.layout = layout
        self.iterator = iterator
        self.pos_size_iter = pos_size_iter
        self.inputs = ops.ExternalSource()
        self.input_crop_pos = ops.ExternalSource()
        self.input_crop_size = ops.ExternalSource()
        self.extra_outputs = extra_outputs
        self.slice = ops.Slice(device = self.device,
                                normalized_anchor = normalized_anchor,
                                normalized_shape = normalized_shape,
                                axes = axes,
                                axis_names = axis_names,
                                out_of_bounds_policy = out_of_bounds_policy,
                                fill_values = fill_values)

    def define_graph(self):
        self.data = self.inputs()
        self.crop_pos = self.input_crop_pos()
        self.crop_size = self.input_crop_size()
        data = self.data.gpu() if self.device == 'gpu' else self.data
        out = self.slice(data, self.crop_pos, self.crop_size)
        if self.extra_outputs:
            return out, self.data, self.crop_pos, self.crop_size
        else:
            return out

    def iter_setup(self):
        data = self.iterator.next()
        self.feed_input(self.data, data, layout=self.layout)

        (crop_pos, crop_size) = self.pos_size_iter.next()
        self.feed_input(self.crop_pos, crop_pos)
        self.feed_input(self.crop_size, crop_size)

class SlicePipeline(Pipeline):
    def __init__(self, device, batch_size, pos_size_iter,
                 num_threads=1, device_id=0, is_fused_decoder=False,
                 axes=None, axis_names=None, normalized_anchor=True, normalized_shape=True):
        super(SlicePipeline, self).__init__(
            batch_size, num_threads, device_id, seed=1234)
        self.is_fused_decoder = is_fused_decoder
        self.pos_size_iter = pos_size_iter
        self.device = device
        self.input = ops.CaffeReader(path = caffe_db_folder, random_shuffle=False)
        self.input_crop_pos = ops.ExternalSource()
        self.input_crop_size = ops.ExternalSource()

        if self.is_fused_decoder:
            self.decode = ops.ImageDecoderSlice(device = "cpu",
                                                output_type = types.RGB,
                                                normalized_anchor=normalized_anchor,
                                                normalized_shape=normalized_shape,
                                                axis_names = axis_names,
                                                axes = axes)
        else:
            self.decode = ops.ImageDecoder(device = "cpu",
                                           output_type = types.RGB)
            self.slice = ops.Slice(device = self.device,
                                   normalized_anchor=normalized_anchor,
                                   normalized_shape=normalized_shape,
                                   axis_names = axis_names,
                                   axes = axes)

    def define_graph(self):
        inputs, labels = self.input(name="Reader")
        self.crop_pos = self.input_crop_pos()
        self.crop_size = self.input_crop_size()

        if self.is_fused_decoder:
            images = self.decode(inputs, self.crop_pos, self.crop_size)
        else:
            images = self.decode(inputs)
            if self.device == 'gpu':
                images = images.gpu()
            images = self.slice(images, self.crop_pos, self.crop_size)
        return images

    def iter_setup(self):
        (crop_pos, crop_size) = self.pos_size_iter.next()
        self.feed_input(self.crop_pos, crop_pos)
        self.feed_input(self.crop_size, crop_size)

class SliceArgsIterator(object):
    def __init__(self,
                 batch_size,
                 num_dims=3,
                 image_shape=None,  # Needed if normalized_anchor and normalized_shape are False
                 image_layout=None, # Needed if axis_names is used to specify the slice
                 normalized_anchor=True,
                 normalized_shape=True,
                 axes=None,
                 axis_names=None,
                 min_norm_anchor=0.0,
                 max_norm_anchor=0.2,
                 min_norm_shape=0.4,
                 max_norm_shape=0.75,
                 seed=54643613):
        self.batch_size = batch_size
        self.num_dims = num_dims
        self.image_shape = image_shape
        self.image_layout = image_layout
        self.normalized_anchor = normalized_anchor
        self.normalized_shape = normalized_shape
        self.axes = axes
        self.axis_names = axis_names
        self.min_norm_anchor=min_norm_anchor
        self.max_norm_anchor=max_norm_anchor
        self.min_norm_shape=min_norm_shape
        self.max_norm_shape=max_norm_shape
        self.seed=seed

        if not self.axis_names and not self.axes:
            self.axis_names = "WH"

        if self.axis_names:
            self.axes = []
            for axis_name in self.axis_names:
                assert axis_name in self.image_layout
                self.axes.append(self.image_layout.index(axis_name))
        assert(len(self.axes)>0)

    def __iter__(self):
        self.i = 0
        self.n = self.batch_size
        return self

    def __next__(self):
        pos = []
        size = []
        anchor_amplitude = self.max_norm_anchor - self.min_norm_anchor
        anchor_offset = self.min_norm_anchor
        shape_amplitude = self.max_norm_shape - self.min_norm_shape
        shape_offset = self.min_norm_shape
        np.random.seed(self.seed)
        for k in range(self.batch_size):
            norm_anchor = anchor_amplitude * np.random.rand(len(self.axes)) + anchor_offset
            norm_shape = shape_amplitude * np.random.rand(len(self.axes)) + shape_offset

            if self.normalized_anchor:
                anchor = norm_anchor
            else:
                anchor = [floor(norm_anchor[i] * self.image_shape[self.axes[i]]) for i in range(len(self.axes))]

            if self.normalized_shape:
                shape = norm_shape
            else:
                shape = [floor(norm_shape[i] * self.image_shape[self.axes[i]]) for i in range(len(self.axes))]

            pos.append(np.asarray(anchor, dtype=np.float32))
            size.append(np.asarray(shape, dtype=np.float32))
            self.i = (self.i + 1) % self.n
        return (pos, size)
    next = __next__

def slice_func_helper(axes, axis_names, layout, normalized_anchor, normalized_shape, image, slice_anchor, slice_shape):
    # TODO(janton): remove this
    if not axes and not axis_names:
        axis_names = "WH"

    if axis_names:
        axes = []
        for axis_name in axis_names:
            assert(axis_name in layout)
            axis_pos = layout.find(axis_name)
            axes.append(axis_pos)

    shape = image.shape
    full_slice_anchor = [0] * len(shape)
    full_slice_shape = list(shape)
    for axis in axes:
        idx = axes.index(axis)
        full_slice_anchor[axis] = slice_anchor[idx]
        full_slice_shape[axis] = slice_shape[idx]

    start, end, _ = abs_slice_start_and_end(shape, full_slice_anchor, full_slice_shape, normalized_anchor, normalized_shape)

    if len(full_slice_anchor) == 1:
        return image[start[0]:end[0]]
    elif len(full_slice_anchor) == 2:
        return image[start[0]:end[0], start[1]:end[1]]
    elif len(full_slice_anchor) == 3:
        return image[start[0]:end[0], start[1]:end[1], start[2]:end[2]]
    elif len(full_slice_anchor) == 4:
        return image[start[0]:end[0], start[1]:end[1], start[2]:end[2], start[3]:end[3]]
    else:
        assert(False)

class SliceSynthDataPipelinePythonOp(Pipeline):
    def __init__(self, batch_size, layout, iterator, pos_size_iter,
                 num_threads=1, device_id=0, num_gpus=1,
                 axes=None, axis_names=None,
                 normalized_anchor=True, normalized_shape=True):
        super(SliceSynthDataPipelinePythonOp, self).__init__(
            batch_size, num_threads, device_id,
            seed=12345, exec_async=False, exec_pipelined=False)
        self.device = "cpu"
        self.layout = layout
        self.iterator = iterator
        self.pos_size_iter = pos_size_iter
        self.inputs = ops.ExternalSource()
        self.input_crop_pos = ops.ExternalSource()
        self.input_crop_size = ops.ExternalSource()

        function = partial(
            slice_func_helper, axes, axis_names, self.layout,
            normalized_anchor, normalized_shape)
        self.slice = ops.PythonFunction(function=function)

    def define_graph(self):
        self.data = self.inputs()
        self.crop_pos = self.input_crop_pos()
        self.crop_size = self.input_crop_size()
        out = self.slice(self.data, self.crop_pos, self.crop_size)
        return out

    def iter_setup(self):
        data = self.iterator.next()
        self.feed_input(self.data, data, layout=self.layout)

        (crop_pos, crop_size) = self.pos_size_iter.next()
        self.feed_input(self.crop_pos, crop_pos)
        self.feed_input(self.crop_size, crop_size)


class SlicePythonOp(Pipeline):
    def __init__(self, batch_size, pos_size_iter,
                 num_threads=1, device_id=0, num_gpus=1,
                 axes=None, axis_names=None,
                 normalized_anchor=True, normalized_shape=True):
        super(SlicePythonOp, self).__init__(
            batch_size, num_threads, device_id,
            seed=12345, exec_async=False, exec_pipelined=False)
        self.device = "cpu"
        self.layout = "HWC"
        self.pos_size_iter = pos_size_iter

        self.input = ops.CaffeReader(path = caffe_db_folder, random_shuffle=False)
        self.decode = ops.ImageDecoder(device = 'cpu', output_type = types.RGB)

        self.input_crop_pos = ops.ExternalSource()
        self.input_crop_size = ops.ExternalSource()

        function = partial(
            slice_func_helper, axes, axis_names, self.layout,
            normalized_anchor, normalized_shape)
        self.slice = ops.PythonFunction(function=function)

    def define_graph(self):
        imgs, _ = self.input()
        imgs = self.decode(imgs)
        self.crop_pos = self.input_crop_pos()
        self.crop_size = self.input_crop_size()
        out = self.slice(imgs, self.crop_pos, self.crop_size)
        return out

    def iter_setup(self):
        (crop_pos, crop_size) = self.pos_size_iter.next()
        self.feed_input(self.crop_pos, crop_pos)
        self.feed_input(self.crop_size, crop_size)


def check_slice_synth_data_vs_numpy(device, batch_size, input_shape, layout, axes, axis_names,
                                    normalized_anchor, normalized_shape):
    eiis = [RandomDataIterator(batch_size, shape=input_shape)
            for k in range(2)]
    eii_args = [SliceArgsIterator(batch_size, len(input_shape), image_shape=input_shape,
                image_layout=layout, axes=axes, axis_names=axis_names, normalized_anchor=normalized_anchor,
                normalized_shape=normalized_shape)
                for k in range(2)]

    compare_pipelines(
        SliceSynthDataPipeline(device, batch_size, layout, iter(eiis[0]), iter(eii_args[0]),
            axes=axes, axis_names=axis_names, normalized_anchor=normalized_anchor,
            normalized_shape=normalized_shape),
        SliceSynthDataPipelinePythonOp(batch_size, layout, iter(eiis[0]), iter(eii_args[1]),
            axes=axes, axis_names=axis_names, normalized_anchor=normalized_anchor,
            normalized_shape=normalized_shape),
        batch_size=batch_size, N_iterations=5)

def test_slice_synth_data_vs_numpy():
    for device in ["cpu", "gpu"]:
        for batch_size in {1, 8}:
            for input_shape, layout, axes, axis_names in \
                [((200,400,3), "HWC", None, "WH"),
                ((200,400,3), "HWC", None, "HW"),
                ((200,400,3), "HWC", None, "C"),
                ((200,400,3), "HWC", (1,0), None),
                ((200,400,3), "HWC", (0,1), None),
                ((200,400,3), "HWC", (2,), None),
                ((200,), "H", (0,), None),
                ((200,), "H", None, "H"),
                ((200,400), "HW", (1,), None),
                ((200,400), "HW", None, "W"),
                ((80, 30, 20, 3), "DHWC", (2,1,0), None),
                ((80, 30, 20, 3), "DHWC", (0,1,2), None),
                ((80, 30, 20, 3), "DHWC", (2,1), None),
                ((80, 30, 20, 3), "DHWC", None, "WHD"),
                ((80, 30, 20, 3), "DHWC", None, "DHW"),
                ((80, 30, 20, 3), "DHWC", None, "WH"),
                ((80, 30, 20, 3), "DHWC", None, "C")]:
                for normalized_anchor in [True, False]:
                    for normalized_shape in [True, False]:
                        yield check_slice_synth_data_vs_numpy, device, batch_size, \
                            input_shape, layout, axes, axis_names, normalized_anchor, normalized_shape

def check_slice_vs_fused_decoder(device, batch_size, axes, axis_names):
    eii_args = [SliceArgsIterator(batch_size, image_layout="HWC", axes=axes, axis_names=axis_names)
                for k in range(2)]
    compare_pipelines(
        SlicePipeline(device, batch_size, iter(eii_args[0]), axes=axes, axis_names=axis_names, is_fused_decoder=False),
        SlicePipeline(device, batch_size, iter(eii_args[1]), axes=axes, axis_names=axis_names, is_fused_decoder=True),
        batch_size=batch_size, N_iterations=5)

def test_slice_vs_fused_decoder():
    for device in ["cpu", "gpu"]:
        for batch_size in {1}:
            for axes, axis_names in \
                [(None, "WH"), (None, "HW"),
                ((1,0), None), ((0,1), None)]:
                yield check_slice_vs_fused_decoder, device, batch_size, axes, axis_names

def check_slice_vs_numpy(device, batch_size, axes, axis_names):
    eii_args = [SliceArgsIterator(batch_size, image_layout="HWC", axes=axes, axis_names=axis_names)
                for k in range(2)]
    compare_pipelines(
        SlicePipeline(device, batch_size, iter(eii_args[0]), axes=axes, axis_names=axis_names),
        SlicePythonOp(batch_size, iter(eii_args[1]), axes=axes, axis_names=axis_names),
        batch_size=batch_size, N_iterations=5)

def test_slice_vs_numpy():
    for device in ["cpu", "gpu"]:
        for batch_size in {1}:
            for axes, axis_names in \
                [(None, "WH"), (None, "HW"),
                ((1,0), None), ((0,1), None)]:
                yield check_slice_vs_numpy, device, batch_size, axes, axis_names


def check_slice_output(sample_in, sample_out, anchor, abs_slice_shape, abs_start, abs_end, out_of_bounds_policy, fill_values, naxes=2):
    in_shape = sample_in.shape
    out_shape = sample_out.shape

    if out_of_bounds_policy == 'pad':
        assert(all([abs_slice_shape[i] == out_shape[i] for i in range(naxes)]))
    elif out_of_bounds_policy == 'trim_to_shape':
        assert(all([out_shape[i] <= in_shape[i] for i in range(naxes)]))
        for i in range(naxes):
            if abs_start[i] < 0:
                abs_start[i] = 0
            if abs_end[i] > in_shape[i]:
                abs_end[i] = in_shape[i]
            abs_slice_shape[i] = abs_end[i] - abs_start[i]
        print("Hey ", abs_slice_shape[:2], out_shape[:2])
        assert(all([abs_slice_shape[i] == out_shape[i] for i in range(naxes)]))
    else:
        assert(False) # Wrong out_of_bounds_policy

    pad_before = [-abs_start[i] if abs_start[i] < 0 else 0 for i in range(naxes)]
    pad_after = [abs_end[i] - in_shape[i] if in_shape[i] < abs_end[i] else 0 for i in range(naxes)]
    sliced = [abs_slice_shape[i] - pad_before[i] - pad_after[i] for i in range(naxes)]

    if out_of_bounds_policy == 'trim_to_shape':
        assert(all([pad_before[i] == 0 for i in range(naxes)]))
        assert(all([pad_after[i] == 0 for i in range(naxes)]))
        assert(all([sliced[i] == out_shape[i] for i in range(naxes)]))

    for i in range(out_shape[0]):
        for j in range(out_shape[1]):
            if (i >= pad_before[0] and j >= pad_before[1] and i < pad_before[0] + sliced[0] and j < pad_before[1] + sliced[1]):
                assert((sample_out[i, j, :] == sample_in[abs_start[0] + i, abs_start[1] + j, :]).all())
            else:
                assert((sample_out[i, j, :] == fill_values).all())

def check_slice_with_out_of_bounds_policy_support(device, batch_size, input_shape=(100, 200, 3),
                                                  out_of_bounds_policy=None, fill_values=(0x76, 0xb9, 0x00),
                                                  normalized_anchor=False, normalized_shape=False):
    # This test case is written with HWC layout in mind and "HW" axes in slice arguments
    axis_names = "HW"
    naxes = len(axis_names)
    axes = None
    layout = "HWC"
    assert(len(input_shape) == 3)
    if fill_values is not None and len(fill_values) > 1:
        assert(input_shape[2] == len(fill_values))

    eii = RandomDataIterator(batch_size, shape=input_shape)
    eii_arg = SliceArgsIterator(batch_size, len(input_shape), image_shape=input_shape,
                                image_layout=layout, axes=axes, axis_names=axis_names,
                                normalized_anchor=normalized_anchor,
                                normalized_shape=normalized_shape,
                                min_norm_anchor=-0.5, max_norm_anchor=-0.1,
                                min_norm_shape=1.1, max_norm_shape=3.6)
    pipe = SliceSynthDataPipeline(device, batch_size, layout, iter(eii), iter(eii_arg),
                                  axes=axes, axis_names=axis_names,
                                  normalized_anchor=normalized_anchor,
                                  normalized_shape=normalized_shape,
                                  out_of_bounds_policy=out_of_bounds_policy,
                                  fill_values=fill_values,
                                  extra_outputs=True)
    if fill_values is None:
        fill_values = 0
    pipe.build()
    for k in range(3):
        outs = pipe.run()
        out = outs[0]
        in_data = outs[1]
        anchor_data = outs[2]
        shape_data = outs[3]
        if isinstance(out, dali.backend_impl.TensorListGPU):
            out = out.as_cpu()
        assert(batch_size == len(out))
        for idx in range(batch_size):
            sample_in = in_data.at(idx)
            sample_out = out.at(idx)
            anchor = anchor_data.at(idx)
            shape = shape_data.at(idx)
            in_shape = sample_in.shape
            out_shape = sample_out.shape
            abs_start, abs_end, abs_slice_shape = abs_slice_start_and_end(
                in_shape[:2], anchor, shape, normalized_anchor, normalized_shape)
            check_slice_output(sample_in, sample_out, anchor, abs_slice_shape, abs_start, abs_end, out_of_bounds_policy, fill_values)


def test_slice_with_out_of_bounds_policy_support():
    in_shape = (40, 80, 3)
    for out_of_bounds_policy in ['pad', 'trim_to_shape']:
        for device in ['gpu', 'cpu']:
            for batch_size in [1, 3]:
                for normalized_anchor, normalized_shape in [(False, False), (True, True)]:
                    for fill_values in [None, (0x76, 0xb0, 0x00)]:
                        yield check_slice_with_out_of_bounds_policy_support, \
                            device, batch_size, in_shape, out_of_bounds_policy, fill_values, \
                            normalized_anchor, normalized_shape

def check_slice_with_out_of_bounds_error(device, batch_size, input_shape=(100, 200, 3),
                                         normalized_anchor=False, normalized_shape=False):
    # This test case is written with HWC layout in mind and "HW" axes in slice arguments
    axis_names = "HW"
    naxes = len(axis_names)
    axes = None
    layout = "HWC"
    assert(len(input_shape) == 3)

    eii = RandomDataIterator(batch_size, shape=input_shape)
    eii_arg = SliceArgsIterator(batch_size, len(input_shape), image_shape=input_shape,
                                image_layout=layout, axes=axes, axis_names=axis_names,
                                normalized_anchor=normalized_anchor,
                                normalized_shape=normalized_shape,
                                min_norm_anchor=-0.5, max_norm_anchor=-0.1,
                                min_norm_shape=1.1, max_norm_shape=3.6)
    pipe = SliceSynthDataPipeline(device, batch_size, layout, iter(eii), iter(eii_arg),
                                  axes=axes, axis_names=axis_names,
                                  normalized_anchor=normalized_anchor,
                                  normalized_shape=normalized_shape,
                                  out_of_bounds_policy="error")

    pipe.build()
    with assert_raises(RuntimeError):
        outs = pipe.run()

def test_slice_with_out_of_bounds_error():
    in_shape = (40, 80, 3)
    for device in ['gpu', 'cpu']:
        for batch_size in [1, 3]:
            for normalized_anchor, normalized_shape in [(False, False), (True, True)]:
                yield check_slice_with_out_of_bounds_error, \
                    device, batch_size, in_shape, normalized_anchor, normalized_shape
