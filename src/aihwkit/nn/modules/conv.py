# -*- coding: utf-8 -*-

# (C) Copyright 2020 IBM. All Rights Reserved.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Convolution layers."""

from typing import Optional, Tuple, Union

from torch import Tensor, arange, cat, float64, int32, ones
from torch.nn import Conv2d, Conv3d, Unfold
from torch.nn.functional import pad
from torch.nn.modules.utils import _pair, _triple

from aihwkit.nn.functions import AnalogIndexedFunction
from aihwkit.nn.modules.base import AnalogModuleBase, RPUConfigAlias


class AnalogConv2d(Conv2d, AnalogModuleBase):
    """2D convolution layer that uses an analog tile.

    Applies a 2D convolution over an input signal composed of several input
    planes, using an analog tile for its forward, backward and update passes.

    Note:
        The tensor parameters of this layer (``.weight`` and ``.bias``) are not
        guaranteed to contain the same values as the internal weights and biases
        stored in the analog tile. Please use ``set_weights`` and
        ``get_weights`` when attempting to read or modify the weight/bias. This
        read/write process can simulate the (noisy and inexact) analog writing
        and reading of the resistive elements.

    Args:
        in_channels: number of channels in the input image.
        out_channels: number of channels produced by the convolution.
        kernel_size: size of the convolving kernel.
        stride: stride of the convolution-
        padding: zero-padding added to both sides of the input.
        dilation: spacing between kernel elements.
        groups: number of blocked connections from input channels to output
            channels.
        bias: whether to use a bias row on the analog tile or not
        padding_mode: padding strategy. Only ``'zeros'`` is supported.
        rpu_config: resistive processing unit configuration.
        realistic_read_write: whether to enable realistic read/write
           for setting initial weights and read out of weights
    """
    # pylint: disable=abstract-method

    __constants__ = ['stride', 'padding', 'dilation', 'groups',
                     'padding_mode', 'output_padding', 'in_channels',
                     'out_channels', 'kernel_size']
    in_channels: int
    out_channels: int
    kernel_size: Tuple[int, int]
    stride: Tuple[int, int]
    padding: Tuple[int, int]
    dilation: Tuple[int, int]
    realistic_read_write: bool
    fold_indices: Tensor
    input_size: float
    in_features: int
    out_features: int

    def __init__(
            self,
            in_channels: int,
            out_channels: int,
            kernel_size: Union[int, Tuple],
            stride: Union[int, Tuple] = 1,
            padding: Union[int, Tuple] = 0,
            dilation: Union[int, Tuple] = 1,
            groups: int = 1,
            bias: bool = True,
            padding_mode: str = 'zeros',
            rpu_config: Optional[RPUConfigAlias] = None,
            realistic_read_write: bool = False
    ):
        # pylint: disable=too-many-arguments
        if groups != 1:
            raise ValueError('Only one group is supported')
        if padding_mode != 'zeros':
            raise ValueError('Only "zeros" padding mode is supported')

        kernel_size = _pair(kernel_size)
        self.in_features = (in_channels // groups) * kernel_size[0] * kernel_size[1]  # type: ignore
        self.out_features = out_channels

        # Create the tile and set the analog.
        self.analog_tile = self._setup_tile(self.in_features,
                                            self.out_features,
                                            bias,
                                            rpu_config,
                                            realistic_read_write)

        # Call super() after tile creation, including ``reset_parameters``.
        super().__init__(in_channels, out_channels, kernel_size, stride,
                         padding, dilation, groups, bias, padding_mode)

        # Setup the Parameter custom attributes needed by the optimizer.
        self.weight.is_weight = True
        if bias:
            self.bias.is_bias = True

        # Set the index matrices.
        self.fold_indices = Tensor().detach()
        self.input_size = 0

    def reset_parameters(self) -> None:
        """Reset the parameters (weight and bias)."""
        super().reset_parameters()
        self.set_weights(self.weight, self.bias)

    def forward(self, x_input: Tensor) -> Tensor:
        """Computes the forward pass."""
        # pylint: disable=arguments-differ

        def get_size(size: int, i: int) -> int:
            """Calculate the output image sizes"""
            nom = (size + 2 * self.padding[i] - self.dilation[i] * (self.kernel_size[i] - 1) - 1)
            return nom // self.stride[i] + 1

        input_size = x_input.numel()/x_input.size(0)
        if not self.fold_indices.numel() or self.input_size != input_size:
            # pytorch just always uses NCHW order?
            fold_indices = arange(2, input_size+2, dtype=float64).detach()
            shape = [1] + list(x_input.shape[1:])
            fold_indices = fold_indices.reshape(*shape)
            unfold = Unfold(kernel_size=self.kernel_size,
                            stride=self.stride,
                            padding=self.padding,
                            dilation=self.dilation)
            fold_indices = unfold(fold_indices).flatten().round().to(dtype=int32)

            if self.use_bias:
                out_image_size = fold_indices.numel() // (self.kernel_size[0]*self.kernel_size[1])
                fold_indices = cat((fold_indices, ones(out_image_size, dtype=int32)), 0)

            self.fold_indices = fold_indices.to(x_input.device)

            x_height = x_input.size(2)
            x_width = x_input.size(3)

            d_height = get_size(x_height, 0)
            d_width = get_size(x_width, 1)

            image_sizes = [self.in_channels, x_height, x_width, d_height, d_width]
            self.input_size = input_size
            self.analog_tile.set_indexed(self.fold_indices, image_sizes)  # type: ignore

        return AnalogIndexedFunction.apply(self.analog_tile, x_input, self.weight,
                                           self.bias, not self.training)

    def extra_repr(self) -> str:
        output = ('{in_channels}, {out_channels}, kernel_size={kernel_size}'
                  ', stride={stride}')
        if self.padding != (0,) * len(self.padding):
            output += ', padding={padding}'
        if self.dilation != (1,) * len(self.dilation):
            output += ', dilation={dilation}'
        if not self.use_bias:
            output += ', bias=False'
        return output.format(**self.__dict__)


class AnalogConv3d(Conv3d, AnalogModuleBase):
    """3D convolution layer that uses an analog tile.

    Applies a 3D convolution over an input signal composed of several input
    planes, using an analog tile for its forward, backward and update passes.

    Note:
        The tensor parameters of this layer (``.weight`` and ``.bias``) are not
        guaranteed to contain the same values as the internal weights and biases
        stored in the analog tile. Please use ``set_weights`` and
        ``get_weights`` when attempting to read or modify the weight/bias. This
        read/write process can simulate the (noisy and inexact) analog writing
        and reading of the resistive elements.

    Args:
        in_channels: number of channels in the input image.
        out_channels: number of channels produced by the convolution.
        kernel_size: size of the convolving kernel.
        stride: stride of the convolution-
        padding: zero-padding added to both sides of the input.
        dilation: spacing between kernel elements.
        groups: number of blocked connections from input channels to output
            channels.
        bias: whether to use a bias row on the analog tile or not
        padding_mode: padding strategy. Only ``'zeros'`` is supported.
        rpu_config: resistive processing unit configuration.
        realistic_read_write: whether to enable realistic read/write
           for setting initial weights and read out of weights
    """
    # pylint: disable=abstract-method

    __constants__ = ['stride', 'padding', 'dilation', 'groups',
                     'padding_mode', 'output_padding', 'in_channels',
                     'out_channels', 'kernel_size']
    in_channels: int
    out_channels: int
    kernel_size: Tuple[int, int, int]
    stride: Tuple[int, int, int]
    padding: Tuple[int, int, int]
    dilation: Tuple[int, int, int]
    realistic_read_write: bool
    fold_indices: Tensor
    input_size: float
    in_features: int
    out_features: int

    def __init__(
            self,
            in_channels: int,
            out_channels: int,
            kernel_size: Union[int, Tuple],
            stride: Union[int, Tuple] = 1,
            padding: Union[int, Tuple] = 0,
            dilation: Union[int, Tuple] = 1,
            groups: int = 1,
            bias: bool = True,
            padding_mode: str = 'zeros',
            rpu_config: Optional[RPUConfigAlias] = None,
            realistic_read_write: bool = False
    ):
        # pylint: disable=too-many-arguments
        if groups != 1:
            raise ValueError('Only one group is supported')
        if padding_mode != 'zeros':
            raise ValueError('Only "zeros" padding mode is supported')
        if dilation != 1:
            raise ValueError('Only dilation = 1 is supported')

        kernel_size = _triple(kernel_size)
        self.in_features = (in_channels // groups) * \
            kernel_size[0] * kernel_size[1] * kernel_size[2]  # type: ignore
        self.out_features = out_channels

        # Create the tile and set the analog.
        self.analog_tile = self._setup_tile(self.in_features,
                                            self.out_features,
                                            bias,
                                            rpu_config,
                                            realistic_read_write)

        # Call super() after tile creation, including ``reset_parameters``.
        super().__init__(in_channels, out_channels, kernel_size, stride,
                         padding, dilation, groups, bias, padding_mode)

        # Setup the Parameter custom attributes needed by the optimizer.
        self.weight.is_weight = True
        if bias:
            self.bias.is_bias = True

        # Set the index matrices.
        self.fold_indices = Tensor().detach()
        self.input_size = 0

    def reset_parameters(self) -> None:
        """Reset the parameters (weight and bias)."""
        super().reset_parameters()
        self.set_weights(self.weight, self.bias)

    def forward(self, x_input: Tensor) -> Tensor:
        """Computes the forward pass."""
        # pylint: disable=arguments-differ,too-many-locals

        def get_size(size: int, i: int) -> int:
            """Calculate the output image sizes"""
            nom = (size + 2 * self.padding[i] - self.dilation[i] * (self.kernel_size[i] - 1) - 1)
            return nom // self.stride[i] + 1

        input_size = x_input.numel()/x_input.size(0)
        if not self.fold_indices.numel() or self.input_size != input_size:
            # pytorch just always uses NCDHW order?
            fold_indices = arange(2, x_input.size(2)*x_input.size(3)*x_input.size(4)+2,
                                  dtype=float64).detach()
            shape = [1] + [1] + list(x_input.shape[2:])
            fold_indices = fold_indices.reshape(*shape)
            if not all(item == 0 for item in self.padding):
                fold_indices = pad(fold_indices, pad=(
                    self.padding[2], self.padding[2],
                    self.padding[1], self.padding[1],
                    self.padding[0], self.padding[0]), mode="constant", value=0)
            unfold = fold_indices.unfold(2, self.kernel_size[0], self.stride[0]).\
                unfold(3, self.kernel_size[1], self.stride[1]).\
                unfold(4, self.kernel_size[2], self.stride[2]).clone()

            fold_indices = unfold.reshape(-1, self.kernel_size[0] * self.kernel_size[1] *
                                          self.kernel_size[2]).transpose(0, 1).flatten().round()

            # concatenate the matrix index for different channels
            fold_indices_orig = fold_indices.clone()
            for i in range(self.in_channels-1):
                fold_indices_tmp = fold_indices_orig.clone()
                for j in range(fold_indices_orig.size(0)):
                    if fold_indices_orig[j] != 0:
                        fold_indices_tmp[j] += (input_size/self.in_channels)*(i+1)

                fold_indices = cat([fold_indices, fold_indices_tmp], dim=0).clone()

            fold_indices = fold_indices.to(dtype=int32)

            if self.use_bias:
                out_image_size = fold_indices.numel() // (self.kernel_size[0] *
                                                          self.kernel_size[1] *
                                                          self.kernel_size[2])
                fold_indices = cat((fold_indices, ones(out_image_size, dtype=int32)), 0)

            self.fold_indices = fold_indices.to(x_input.device)

            x_depth = x_input.size(2)
            x_height = x_input.size(3)
            x_width = x_input.size(4)

            d_depth = get_size(x_depth, 0)
            d_height = get_size(x_height, 1)
            d_width = get_size(x_width, 2)

            image_sizes = [self.in_channels, x_depth, x_height, x_width, d_depth, d_height, d_width]
            self.input_size = input_size
            self.analog_tile.set_indexed(self.fold_indices, image_sizes)  # type: ignore

        return AnalogIndexedFunction.apply(self.analog_tile, x_input, self.weight,
                                           self.bias, not self.training)

    def extra_repr(self) -> str:
        output = ('{in_channels}, {out_channels}, kernel_size={kernel_size}'
                  ', stride={stride}')
        if self.padding != (0,) * len(self.padding):
            output += ', padding={padding}'
        if self.dilation != (1,) * len(self.dilation):
            output += ', dilation={dilation}'
        if not self.use_bias:
            output += ', bias=False'
        return output.format(**self.__dict__)
