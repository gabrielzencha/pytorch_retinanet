from typing import *
import torch
import torch.nn.functional as F
from torch import nn
from torch.functional import Tensor
from .config import *
from .utils import bbox_2_activ, matcher


def focal_loss(inputs: Tensor, targets: Tensor,) -> Tensor:
    """
    Focal Loss
    """
    alpha = FOCAL_LOSS_ALPHA
    gamma = FOCAL_LOSS_GAMMA

    ps = torch.sigmoid(inputs.detach())
    weights = targets * (1 - ps) + (1 - targets) * ps
    alphas = (1 - targets) * alpha + targets * (1 - alpha)
    weights.pow_(gamma).mul_(alphas)

    clas_loss = F.binary_cross_entropy_with_logits(inputs, targets, weights, reduction="sum")

    return clas_loss


class RetinaNetLosses(nn.Module):
    def __init__(self, num_classes) -> None:
        super(RetinaNetLosses, self).__init__()
        self.n_c = num_classes

    def _encode_class(self, idxs):
        "one_hot encode targets such that 0 is the `background`"
        target = idxs.new_zeros(len(idxs), self.n_c).float()
        mask = idxs != 0
        i1s = torch.LongTensor(list(range(len(idxs))))
        target[i1s[mask], idxs[mask] - 1] = 1
        return target

    def calc_loss(self, anchors, clas_pred, bbox_pred, clas_tgt, bbox_tgt):
        matches = matcher(anchors)
        bbox_mask = matches >= 0
        if bbox_mask.sum() != 0:
            bbox_pred = bbox_pred[bbox_mask]
            bbox_tgt = bbox_tgt[matches[bbox_mask]]
            bbox_tgt = bbox_2_activ(bbox_tgt, anchors[bbox_mask])
            bb_loss = F.smooth_l1_loss(bbox_pred, bbox_tgt)
        else:
            bb_loss = 0.0

        matches.add_(1)
        clas_tgt = clas_tgt + 1
        clas_mask = matches >= 0

        clas_pred = clas_pred[clas_mask]
        clas_tgt = torch.cat([clas_tgt.new_zeros(1).long(), clas_tgt])
        clas_tgt = clas_tgt[matches[clas_mask]]

        clas_loss = focal_loss(clas_pred, clas_tgt) / torch.clamp(bbox_mask.sum(), min=1.0)

        return clas_loss, bb_loss

    def forward(self, targets, head_outputs, anchors):
        clas_preds, bbox_preds = head_outputs["cls_preds"], head_outputs["bbox_preds"]
        loss = {}
        loss["classification_loss"] = []
        loss["regression_loss"] = []

        class_targs, bbox_targs = targets["labels"], targets["boxes"]

        for cls_pred, bb_pred, cls_targs, bb_targs, ancs in zip(
            clas_preds, bbox_preds, class_targs, bbox_targs, anchors
        ):

            # Compute loss
            clas_loss, bb_loss = self.calc_loss(ancs, cls_pred, bb_pred, cls_targs, bb_targs)

            loss["classification_loss"].append(clas_loss)
            loss["regression_loss"].append(bb_loss)

        loss["classification_loss"] = sum(loss["classification_loss"]) / len(targets)
        loss["regression_loss"] = sum(loss["regression_loss"]) / len(targets)
        return loss
