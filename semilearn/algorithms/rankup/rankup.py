# Copyright (c) 2024 Pin-Yen Huang.
# Licensed under the MIT License.

import numpy as np

from .rda import RDAHook
from .net import RankUp_Net

from semilearn.core import AlgorithmBase
from semilearn.core.utils import ALGORITHMS
from semilearn.algorithms.utils import SSL_Argument, str2bool
from semilearn.algorithms.hooks import PseudoLabelingHook, FixedThresholdingHook

from semilearn.core.criterions import CELoss, ClsConsistencyLoss


@ALGORITHMS.register("rankup")
class RankUp(AlgorithmBase):
    """
    RankUp algorithm (https://arxiv.org/abs/2410.22124).

    Args:
        - args (`argparse`):
            algorithm arguments
        - net_builder (`callable`):
            network loading function
        - tb_log (`TBLog`):
            tensorboard logger
        - logger (`logging.Logger`):
            logger to use
        - use_rda (`bool`, *optional*, default to `False`):
            If True, use RankUp with RDA
        - reg_unsup_warm_up (`float`, *optional*, defaults to 0.4):
            Ramp up for weights for unsupervised loss
        - rda_num_refine_iter (`int`):
            Number of iterations to apply RDA
        - T (`float`):
            Temperature for pseudo-label sharpening
        - p_cutoff(`float`):
            Confidence threshold for generating pseudo-labels
        - hard_label (`bool`, *optional*, default to `False`):
            If True, targets have [Batch size] shape with int values. If False, the target is vector
    """

    def __init__(self, args, net_builder, tb_log=None, logger=None):
        self.init(
            use_rda=args.use_rda,
            reg_unsup_warm_up=args.reg_unsup_warm_up,
            rda_num_refine_iter=args.rda_num_refine_iter,
            cls_ulb_loss_ratio=args.cls_ulb_loss_ratio,
            cls_loss_ratio=args.cls_loss_ratio,
            T=args.T,
            p_cutoff=args.p_cutoff,
            hard_label=args.hard_label,
        )
        super().__init__(args, net_builder, tb_log, logger)
        self.cls_loss = CELoss()
        self.cls_consistency_loss = ClsConsistencyLoss()

    def init(self, use_rda, reg_unsup_warm_up, rda_num_refine_iter, cls_ulb_loss_ratio, cls_loss_ratio, T, p_cutoff, hard_label):
        self.use_rda = use_rda
        self.reg_unsup_warm_up = reg_unsup_warm_up
        self.rda_num_refine_iter = rda_num_refine_iter
        self.cls_ulb_loss_ratio = cls_ulb_loss_ratio
        self.cls_loss_ratio = cls_loss_ratio
        self.T = T
        self.p_cutoff = p_cutoff
        self.use_hard_label = hard_label

    def set_hooks(self):
        super().set_hooks()
        # reset PseudoLabelingHook hook
        self.register_hook(
            RDAHook(
                train_ulb_len=len(self.dataset_dict["train_ulb"]),
                lb_targets=np.copy(self.dataset_dict["train_lb"].targets),
                num_refine_iter=self.rda_num_refine_iter,
            ),
            "RDAHook",
        )
        self.register_hook(PseudoLabelingHook(), "PseudoLabelingHook")
        self.register_hook(FixedThresholdingHook(), "MaskingHook")

    def set_model(self, **kwargs):
        """
        overwrite the initialize model function
        """
        model = super().set_model(**kwargs)
        model = RankUp_Net(model)
        return model

    def set_ema_model(self, **kwargs):
        """
        overwrite the initialize ema model function
        """
        ema_model = self.net_builder(pretrained=self.args.use_pretrain, pretrained_path=self.args.pretrain_path, **kwargs)
        ema_model = RankUp_Net(ema_model)
        ema_model.load_state_dict(self.model.state_dict())
        return ema_model

    def train_step(self, x_lb, y_lb, idx_ulb, x_ulb_w, x_ulb_s):
        self.idx_ulb = idx_ulb

        # inference and calculate sup losses
        with self.amp_cm():
            outs_x_lb = self.model(x_lb, use_arc=True, targets=y_lb)
            logits_x_lb = outs_x_lb["logits"]
            feats_x_lb = outs_x_lb["feat"]
            logits_arc_x_lb = outs_x_lb["logits_arc"]
            arc_y_lb = outs_x_lb["targets_arc"]

            self.bn_controller.freeze_bn(self.model)
            outs_x_ulb_w = self.model(x_ulb_w, use_arc=True)
            logits_x_ulb_w = outs_x_ulb_w["logits"]
            feats_x_ulb_w = outs_x_ulb_w["feat"]
            logits_arc_x_ulb_w = outs_x_ulb_w["logits_arc"]
            probs_x_ulb_w = self.compute_prob(logits_arc_x_ulb_w.detach())
            self.bn_controller.unfreeze_bn(self.model)

            outs_x_ulb_s = self.model(x_ulb_s, use_arc=True)
            logits_x_ulb_s = outs_x_ulb_s["logits_arc"]
            feats_x_ulb_s = outs_x_ulb_s["feat"]

            feat_dict = {"x_lb": feats_x_lb, "x_ulb_w": feats_x_ulb_w, "x_ulb_s": feats_x_ulb_s}

            reg_sup_loss = self.reg_loss(logits_x_lb, y_lb, reduction="mean")

            reg_unsup_loss = 0
            if self.use_rda:
                # generate unlabeled targets using rda hook
                reg_pseudo_label = self.call_hook(
                    "gen_ulb_targets",
                    "RDAHook",
                    logits=logits_x_ulb_w,
                )
                reg_pseudo_label = reg_pseudo_label.detach()
                reg_unsup_loss = self.reg_consistency_loss(logits_x_ulb_w, reg_pseudo_label, "mse")

            # compute mask
            mask = self.call_hook("masking", "MaskingHook", logits_x_ulb=probs_x_ulb_w, softmax_x_ulb=False)

            # generate unlabeled targets using pseudo label hook
            arc_pseudo_label = self.call_hook(
                "gen_ulb_targets",
                "PseudoLabelingHook",
                logits=probs_x_ulb_w,
                use_hard_label=self.use_hard_label,
                T=self.T,
                softmax=False,
            )

            cls_sup_loss = self.cls_loss(logits_arc_x_lb, arc_y_lb, reduction="mean")
            cls_unsup_loss = self.cls_consistency_loss(logits_x_ulb_s, arc_pseudo_label, "ce", mask=mask)

            reg_unsup_warmup = np.clip(self.it / (self.reg_unsup_warm_up * self.num_train_iter), a_min=0.0, a_max=1.0)
            total_reg_loss = reg_sup_loss + self.reg_ulb_loss_ratio * reg_unsup_loss * reg_unsup_warmup
            total_cls_loss = cls_sup_loss + self.cls_ulb_loss_ratio * cls_unsup_loss
            total_loss = total_reg_loss + self.cls_loss_ratio * total_cls_loss

        out_dict = self.process_out_dict(loss=total_loss, feat=feat_dict)
        log_dict = self.process_log_dict(reg_loss=total_reg_loss.item(), cls_loss=total_cls_loss.item(), total_loss=total_loss.item())
        return out_dict, log_dict

    @staticmethod
    def get_argument():
        return [
            SSL_Argument("--use_rda", str2bool, False),
            SSL_Argument("--reg_unsup_warm_up", float, 0.4),
            SSL_Argument("--rda_num_refine_iter", int, 1024),
            SSL_Argument("--cls_ulb_loss_ratio", float, 1.0),
            SSL_Argument("--cls_loss_ratio", float, 1.0),
            SSL_Argument("--T", float, 0.5),
            SSL_Argument("--p_cutoff", float, 0.95),
            SSL_Argument("--hard_label", str2bool, True),
        ]