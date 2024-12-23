# Copyright (c) Microsoft Corporation.
# Modifications Copyright (c) 2024 Pin-Yen Huang.
# Licensed under the MIT License.

import torch
import numpy as np

from semilearn.core import AlgorithmBase
from semilearn.core.utils import ALGORITHMS
from semilearn.algorithms.utils import SSL_Argument


@ALGORITHMS.register("meanteacher")
class MeanTeacher(AlgorithmBase):
    """
    MeanTeacher algorithm (https://arxiv.org/abs/1703.01780).

    Args:
    - args (`argparse`):
        algorithm arguments
    - net_builder (`callable`):
        network loading function
    - tb_log (`TBLog`):
        tensorboard logger
    - logger (`logging.Logger`):
        logger to use
    - reg_unsup_warm_up (`float`, *optional*, defaults to 0.4):
        Ramp up for weights for unsupervised loss
    """

    def __init__(self, args, net_builder, tb_log=None, logger=None, **kwargs):
        super().__init__(args, net_builder, tb_log, logger, **kwargs)
        # mean teacher specified arguments
        self.reg_init(reg_unsup_warm_up=args.reg_unsup_warm_up)

    def reg_init(self, reg_unsup_warm_up=0.4):
        self.reg_unsup_warm_up = reg_unsup_warm_up

    def train_step(self, x_lb, y_lb, x_ulb_w, x_ulb_w_2, **kwargs):
        # inference and calculate sup/unsup losses
        with self.amp_cm():
            outs_x_lb = self.model(x_lb)
            logits_x_lb = outs_x_lb["logits"]
            feats_x_lb = outs_x_lb["feat"]

            self.ema.apply_shadow()
            with torch.no_grad():
                self.bn_controller.freeze_bn(self.model)
                outs_x_ulb_w = self.model(x_ulb_w)
                logits_x_ulb_w = outs_x_ulb_w["logits"]  # self.model(x_ulb_w)
                feats_x_ulb_w = outs_x_ulb_w["feat"]
                self.bn_controller.unfreeze_bn(self.model)
            self.ema.restore()

            self.bn_controller.freeze_bn(self.model)
            outs_x_ulb_w_2 = self.model(x_ulb_w_2)
            logits_x_ulb_w_2 = outs_x_ulb_w_2["logits"]
            feats_x_ulb_w_2 = outs_x_ulb_w_2["feat"]
            self.bn_controller.unfreeze_bn(self.model)

            # extract features for further use in the classification algorithm.
            feat_dict = {"x_lb": feats_x_lb, "x_ulb_w": feats_x_ulb_w, "x_ulb_w_2": feats_x_ulb_w_2}
            for k in kwargs:
                feat_dict[k] = self.model(kwargs[k], only_feat=True)

            sup_loss = self.reg_loss(logits_x_lb, y_lb, reduction="mean")
            unsup_loss = self.reg_consistency_loss(logits_x_ulb_w_2, logits_x_ulb_w.detach(), "mse")

            unsup_warmup = np.clip(self.it / (self.reg_unsup_warm_up * self.num_train_iter), a_min=0.0, a_max=1.0)
            total_loss = sup_loss + self.reg_ulb_loss_ratio * unsup_loss * unsup_warmup

        out_dict = self.process_out_dict(loss=total_loss, feat=feat_dict)
        log_dict = self.process_log_dict(total_loss=total_loss.item())
        log_dict["train_reg/sup_loss"] = sup_loss.item()
        log_dict["train_reg/unsup_loss"] = unsup_loss.item()
        log_dict["train_reg/total_loss"] = total_loss.item()
        return out_dict, log_dict

    @staticmethod
    def get_argument():
        return [
            SSL_Argument("--reg_unsup_warm_up", float, 0.4, "warm up ratio for regression unsupervised loss"),
        ]
