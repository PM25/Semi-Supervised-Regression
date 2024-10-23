# Copyright (c) 2024 Pin-Yen Huang.
# Licensed under the MIT License.
# Code in this file is adapted from xmed-lab/CLSS
# https://github.com/xmed-lab/CLSS

from semilearn.core import AlgorithmBase
from semilearn.core.utils import ALGORITHMS
from semilearn.algorithms.utils import SSL_Argument

from .ordinal_entropy import ordinal_entropy
from .ulb_rank import ulb_rank, ulb_rank_prdlb


@ALGORITHMS.register("clss")
class CLSS(AlgorithmBase):
    """
    CLSS algorithm (https://proceedings.neurips.cc/paper_files/paper/2023/file/b2d4051f03a7038a2771dfbbe5c7b54e-Paper-Conference.pdf).

    Args:
    - args (`argparse`):
        algorithm arguments
    - net_builder (`callable`):
        network loading function
    - tb_log (`TBLog`):
        tensorboard logger
    - logger (`logging.Logger`):
        logger to use
    """

    def __init__(self, args, net_builder, tb_log=None, logger=None, **kwargs):
        super().__init__(args, net_builder, tb_log, logger, **kwargs)
        self.reg_init(
            reg_lambda_val=args.reg_lambda_val,
            reg_lb_ctr_loss_ratio=args.reg_lb_ctr_loss_ratio,
            reg_ulb_ctr_loss_ratio=args.reg_ulb_ctr_loss_ratio,
            reg_ulb_rank_loss_ratio=args.reg_ulb_rank_loss_ratio,
        )

    def reg_init(
        self,
        reg_lambda_val=2,
        reg_lb_ctr_loss_ratio=1.0,
        reg_ulb_ctr_loss_ratio=0.05,
        reg_ulb_rank_loss_ratio=0.01,
    ):
        self.reg_lambda_val = reg_lambda_val
        self.reg_lb_ctr_loss_ratio = reg_lb_ctr_loss_ratio
        self.reg_ulb_ctr_loss_ratio = reg_ulb_ctr_loss_ratio
        self.reg_ulb_rank_loss_ratio = reg_ulb_rank_loss_ratio

    def train_step(self, x_lb, y_lb, x_ulb_w, **kwargs):
        # inference and calculate sup/unsup losses
        with self.amp_cm():
            x_lb_outputs = self.model(x_lb)
            logits_x_lb = x_lb_outputs["logits"]
            feats_x_lb = x_lb_outputs["feat"]

            x_ulb_outputs = self.model(x_ulb_w)
            logits_x_ulb_w = x_ulb_outputs["logits"]
            feats_x_ulb_w = x_ulb_outputs["feat"]

            # extract features for further use in the classification algorithm.
            feat_dict = {"x_lb": feats_x_lb, "x_ulb_w": feats_x_ulb_w}
            for k in kwargs:
                feat_dict[k] = self.model(kwargs[k], only_feat=True)

            sup_reg_loss = self.reg_loss(logits_x_lb, y_lb, reduction="mean")
            sup_ctr_loss = ordinal_entropy(feats_x_lb, y_lb)
            sup_loss = sup_reg_loss + self.reg_lb_ctr_loss_ratio * sup_ctr_loss

            unsup_ctr_loss, ft_rank = ulb_rank(feats_x_ulb_w, self.reg_lambda_val)
            unsup_rank_loss = ulb_rank_prdlb(logits_x_ulb_w.unsqueeze(1), self.reg_lambda_val, pred_inp=ft_rank)
            unsup_loss = self.reg_ulb_ctr_loss_ratio * unsup_ctr_loss + self.reg_ulb_rank_loss_ratio * unsup_rank_loss

            total_loss = sup_loss + unsup_loss

        out_dict = self.process_out_dict(loss=total_loss, feat=feat_dict)
        log_dict = self.process_log_dict(total_loss=total_loss.item())
        log_dict["train_reg/sup_loss"] = sup_loss.item()
        log_dict["train_reg/unsup_loss"] = unsup_loss.item()
        log_dict["train_reg/total_loss"] = total_loss.item()
        return out_dict, log_dict

    @staticmethod
    def get_argument():
        return [
            SSL_Argument("--reg_lambda_val", float, 2.0),
            SSL_Argument("--reg_lb_ctr_loss_ratio", float, 1.0),
            SSL_Argument("--reg_ulb_ctr_loss_ratio", float, 0.05),
            SSL_Argument("--reg_ulb_rank_loss_ratio", float, 0.01),
        ]
