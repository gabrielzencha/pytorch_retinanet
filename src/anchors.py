# Modified From : https://github.com/facebookresearch/detectron2/blob/master/detectron2/modeling/anchor_generator.py
import math
from typing import *

import torch
from torch import device, nn
from torch.functional import Tensor

from .config import *
from .utils import ifnone


class BufferList(nn.Module):
    """
    Similar to nn.ParameterList, but for buffers
    """

    def __init__(self, buffers):
        super(BufferList, self).__init__()
        for i, buffer in enumerate(buffers):
            self.register_buffer(str(i), buffer)

    def __len__(self):
        return len(self._buffers)

    def __iter__(self):
        return iter(self._buffers.values())


def _broadcast_params(params, num_features, name) -> List[List[float]]:
    """
    If one size (or aspect ratio) is specified and there are multiple feature
    maps, we "broadcast" anchors of that single size (or aspect ratio)
    over all feature maps.
    If params is list[float], or list[list[float]] with len(params) == 1, repeat
    it num_features time.
    Returns:
        list[list[float]]: param for each feature
    """
    assert isinstance(
        params, (list, tuple)
    ), f"{name} in anchor generator has to be a list! Got {params}."
    assert len(params), f"{name} in anchor generator cannot be empty!"
    if not isinstance(params[0], (list, tuple)):  # list[float]
        return [params] * num_features
    if len(params) == 1:
        return list(params) * num_features
    assert len(params) == num_features, (
        f"Got {name} of length {len(params)} in anchor generator, "
        f"but the number of input features is {num_features}!"
    )
    return params


class AnchorGenerator(nn.Module):
    """
    Module the Generates anchors for given set of `feature maps`.

    Args:
        sizes (List[float]): is the list of anchor sizes (i.e., sqrt of anchor area)
                to use for the i-th feature map. For each area in `sizes` anchors with
                different `aspect ratios` are generated by the anchor generator.
        aspect_ratios (List[float]): list of aspect ratios (i.e., height/width) to use for anchors.
        strides (List[int]): stride of each input feature.
        offset (float): Relative offset between the center of the first anchor and the top-left
                corner of the image. Value has to be in [0, 1).
    """

    def __init__(
        self,
        sizes: List[float] = None,
        aspect_ratios: List[float] = None,
        strides: List[int] = None,
        offset: float = None,
    ) -> None:

        super(AnchorGenerator, self).__init__()
        # Anchors have areas of 32**2 to 512**2 on pyramid levels P3 to P7
        # at each pyramid level we use anchors at three aspect ratios {1:2; 1:1, 2:1}
        # at each anchor level we add anchors of sizes {2**0, 2**(1/3), 2**(2/3)} of the original set of 3 anchors
        # In total there are A=9 anchors at each feature map for each pixel
        # Unpack parameters
        strides = ifnone(strides, ANCHOR_STRIDES)
        sizes = ifnone(sizes, ANCHOR_SIZES)
        aspect_ratios = ifnone(aspect_ratios, ANCHOR_ASPECT_RATIOS)
        offset = ifnone(offset, ANCHOR_OFFSET)

        self.strides = strides
        self.num_features = len(strides)
        self.sizes = _broadcast_params(sizes, self.num_features, "sizes")
        self.aspect_ratios = _broadcast_params(
            aspect_ratios, self.num_features, "aspect_ratios"
        )
        self.offset = offset
        self.cell_anchors = self._calculate_cell_anchors(self.sizes, self.aspect_ratios)

    def _calculate_cell_anchors(self, sizes, ratios):
        return self._calculate_anchors(sizes, ratios)

    def _calculate_anchors(self, sizes, aspect_ratios) -> List[Tensor]:
        # Generate anchors of `size` (for size in sizes) of `ratio` (for ratio in aspect_ratios)
        cell_anchors = [
            self.generate_cell_anchors(s, a).float()
            for s, a in zip(sizes, aspect_ratios)
        ]
        return BufferList(cell_anchors)

    @staticmethod
    def generate_cell_anchors(sizes, aspect_ratios) -> Tensor:
        """
        Generates a Tensor storing cannonical anchor boxes, where all
        anchor boxes are of different sizes & aspect ratios centered at (0,0).
        We can later build the set of anchors for a full feature map by
        shifting and tiling these tensors.

        Args:
            sizes tuple[float]
            aspect_ratios tuple[float]

        Returns:
            Tensor of shape (len(sizes)*len(aspect_ratios), 4) storing anchor boxes in XYXY format
        """
        # instantiate empty anchor list to store anchors
        anchors = []
        # Iterate over given sizes
        for size in sizes:
            area = size ** 2.0
            for aspect_ratio in aspect_ratios:
                w = math.sqrt(area / aspect_ratio)
                h = aspect_ratio * w
                x0, y0, x1, y1 = -w / 2.0, -h / 2.0, w / 2.0, h / 2.0
                anchors.append([x0, y0, x1, y1])
        return torch.tensor(anchors)

    @property
    def num_cell_anchors(self):
        return self.num_anchors

    @property
    def num_anchors(self) -> List[int]:
        """
        Returns : List[int] : Each int is the number of anchors at every pixel
                              location in the feature map.
                              For example, if at every pixel we use anchors of 3 aspect
                              ratios and 3 sizes, the number of anchors is 9.
        """
        return [len(cell_anchors) for cell_anchors in self.cell_anchors]

    @staticmethod
    def _compute_grid_offsets(size: List[int], stride: int, offset: float, device):
        "Compute grid offsets of `size` with `stride`"
        H, W = size

        shifts_x = torch.arange(
            offset * stride, W * stride, step=stride, dtype=torch.float32, device=device
        )

        shifts_y = torch.arange(
            offset * stride, H * stride, step=stride, dtype=torch.float32, device=device
        )

        shifts_y, shifts_x = torch.meshgrid(shifts_y, shifts_x)

        shifts_x, shifts_y = shifts_x.reshape(-1), shifts_y.reshape(-1)

        return shifts_x, shifts_y

    def grid_anchors(self, grid_sizes: List[List[int]], device) -> List[Tensor]:
        """
        Returns : list[Tensor] : #feature_map tensors, each is (#locations x #cell_anchors) x 4
        """
        # List to store anchors generated for given feature maps
        anchors = []
        buffers: List[torch.Tensor] = [x[1] for x in self.cell_anchors.named_buffers()]

        # Generate `anchors` over single feature map.
        for size, stride, base_anchors in zip(grid_sizes, self.strides, buffers):
            # Compute grid offsets from `size` and `stride`
            shift_x, shift_y = self._compute_grid_offsets(
                size, stride, offset=self.offset, device=device
            )
            shifts = torch.stack((shift_x, shift_y, shift_x, shift_y), dim=1)
            # shift base anchors to get the set of anchors for a full feature map
            anchors.append(
                (shifts.view(-1, 1, 4) + base_anchors.view(1, -1, 4))
                .reshape(-1, 4)
                .to(device)
            )

        return anchors

    def forward(self, images: List[Tensor], feature_maps: List[Tensor]) -> List[Tensor]:
        """
        Generate `Anchors` for each `Feature Map`.

        Args:
          1. features (list[Tensor]): list of backbone feature maps on which to generate anchors.

        Returns:
          list[Tensor]: a list of Tensors containing all the anchors for each feature map for all Images.
                        (i.e. the cell anchors repeated over all locations in the feature map).
                        The number of anchors of each feature map is Hi x Wi x num_cell_anchors,
                        where Hi, Wi are Height & Width of the Feature Map respectively.
        """
        # Grab the size of each of the feature maps
        grid_sizes = [feature_map.shape[-2:] for feature_map in feature_maps]
        device = feature_maps[0].device
        anchors = []
        # calculate achors for all Images
        for _ in images:
            # Generate anchors for all Features Map
            ancs = self.grid_anchors(grid_sizes, device=device)
            anchors.append(ancs)

        return [torch.cat(anchors_per_image) for anchors_per_image in anchors]
