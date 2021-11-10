import os
import sys
import argparse
import datetime

import torch
import numpy as np
import torch.nn as nn
import torch.optim as optim

from model.config import cfg
from model.modeling.FinalNet import Net
from model.dataset.rscan_detection_dataset import RScanDetectionVotesDataset
from model.dataset.model_util_rscan import RScanDatasetConfig as DC
from my_utils.load_pretrain_model import load_pretrain_model
from my_utils.make_optimizer import build_optimizer
from model.modeling.detector.pointnet2.pytorch_utils import BNMomentumScheduler
from torch.optim import lr_scheduler
from torch.utils.data import DataLoader
from my_utils.my_collate_fn import my_collate_fn
from model.modeling.roi_head.relation_head.metric_processor import BatchMetricProcessor,  RecallKProcessor, REL_parse_gt, REL_parse_output

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = BASE_DIR
sys.path.append(os.path.join(ROOT_DIR, 'model'))
sys.path.append(os.path.join(ROOT_DIR, 'my_utils'))
sys.path.append(os.path.join(ROOT_DIR, 'scannet'))
sys.path.append(os.path.join(ROOT_DIR, '3rscan'))

parser = argparse.ArgumentParser()
parser.add_argument('--checkpoint_path', default=None, help='Model checkpoint path [default: None]')
parser.add_argument('--log_dir', default='log', help='Dump dir to save model checkpoint [default: log]')
parser.add_argument('--dump_dir', default=None, help='Dump dir to save sample outputs [default: None]')
parser.add_argument('--num_points', type=int, default=40000, help='Point Number [default: 40000]')
parser.add_argument('--num_feature_dim', type=int, default=256, help='Feature Extractor Output Dim [default: 256]')
parser.add_argument('--ap_iou_thresh', type=float, default=0.25, help='AP IoU threshold [default: 0.25]')
parser.add_argument('--max_epoch', type=int, default=241, help='Epoch to run [default: 240]')
parser.add_argument('--batch_size', type=int, default=8, help='Batch Size during training [default: 8]')
parser.add_argument('--learning_rate', type=float, default=0.001, help='Initial learning rate [default: 0.001]')
parser.add_argument('--weight_decay', type=float, default=0, help='Optimization L2 weight decay [default: 0]')
parser.add_argument('--bn_decay_step', type=int, default=20, help='Period of BN decay (in epochs) [default: 20]')
parser.add_argument('--bn_decay_rate', type=float, default=0.5, help='Decay rate for BN decay [default: 0.5]')
parser.add_argument('--lr_decay_steps', default='80,120,160,200',
                    help='When to decay the learning rate (in epochs) [default: 80,120,160]')
parser.add_argument('--lr_decay_rates', default='0.1,0.1,0.05,0.5', help='Decay rates for lr decay [default: 0.1,0.1,0.05,0.5]')
parser.add_argument('--no_height', action='store_true', help='Do NOT use height signal in input.')
parser.add_argument('--use_color', action='store_true', default=True, help='Use RGB color in input.')
parser.add_argument('--overwrite', action='store_true', help='Overwrite existing log and dump folders.')
parser.add_argument('--dump_results', action='store_true', help='Dump results.')
FLAGS = parser.parse_args()

# ==============================Argparse=======================================

BATCH_SIZE = FLAGS.batch_size
NUM_POINTS = FLAGS.num_points
FEATURE_DIM = FLAGS.num_feature_dim
MAX_EPOCH = FLAGS.max_epoch
BASE_LEARNING_RATE = FLAGS.learning_rate
BN_DECAY_STEP = FLAGS.bn_decay_step
BN_DECAY_RATE = FLAGS.bn_decay_rate
LR_DECAY_STEPS = [int(x) for x in FLAGS.lr_decay_steps.split(',')]
LR_DECAY_RATES = [float(x) for x in FLAGS.lr_decay_rates.split(',')]
assert (len(LR_DECAY_STEPS) == len(LR_DECAY_RATES))
LOG_DIR = FLAGS.log_dir
DEFAULT_DUMP_DIR = os.path.join(BASE_DIR, os.path.basename(LOG_DIR))
DUMP_DIR = FLAGS.dump_dir if FLAGS.dump_dir is not None else DEFAULT_DUMP_DIR
FLAGS.DUMP_DIR = DUMP_DIR

# ==============================Log Dump=========================================

if os.path.exists(LOG_DIR) and FLAGS.overwrite:
    print('Log folder %s already exists. Are you sure to overwrite? (Y/N)' % (LOG_DIR))
    c = input()
    if c == 'n' or c == 'N':
        print('Exiting..')
        exit()
    elif c == 'y' or c == 'Y':
        print('Overwrite the files in the log and dump folers...')
        os.system('rm -r %s' % LOG_DIR)

if not os.path.exists(LOG_DIR):
    os.mkdir(LOG_DIR)

LOG_FOUT = open(os.path.join(LOG_DIR, 'log_train.txt'), 'a')
LOG_FOUT.write(str(FLAGS) + '\n')


def log_string(out_str):
    LOG_FOUT.write(out_str + '\n')
    LOG_FOUT.flush()
    print(out_str)


if not os.path.exists(DUMP_DIR): os.mkdir(DUMP_DIR)

# ==============================Dataset DataLoader=====================================

def my_worker_init_fn(worker_id):
    np.random.seed(np.random.get_state()[1][0] + worker_id)

cfg.merge_from_file(os.path.join(ROOT_DIR, "e2e_relation_predcls_votenet_motif.yaml"))
cfg.freeze()
sys.path.append(os.path.join(ROOT_DIR, "model/dataset"))
DATASET_CONFIG = DC()
TRAIN_DATASET = RScanDetectionVotesDataset(cfg, 'train', num_points=NUM_POINTS,
                                           augment=False,
                                           use_color=False, use_height=False)
EVAL_DATASET = RScanDetectionVotesDataset(cfg, 'test', num_points=NUM_POINTS,
                                          augment=False,
                                          use_color=False, use_height=False)

print("Len of TRAIN_DATASET:" + str(len(TRAIN_DATASET)))
TRAIN_DATALOADER = DataLoader(TRAIN_DATASET, batch_size=BATCH_SIZE,
                              shuffle=True, num_workers=4, worker_init_fn=my_worker_init_fn, collate_fn=my_collate_fn)
print("Len of TRAIN_DATALOADER:" + str(len(TRAIN_DATALOADER)))
print("Len of EVAL_DATASET:" + str(len(EVAL_DATASET)))
EVAL_DATALOADER = DataLoader(EVAL_DATASET, batch_size=BATCH_SIZE,
                             shuffle=True, num_workers=4, worker_init_fn=my_worker_init_fn, collate_fn=my_collate_fn)
print("Len of EVAL_DATALOADER:" + str(len(EVAL_DATALOADER)))

# ================================Model=======================================

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
num_input_channel = 0
model = Net(cfg).cuda(0)

if torch.cuda.device_count() > 1:
    # log_string("Let's use %d GPUs!" % (torch.cuda.device_count()))
    # dim = 0 [30, xxx] -> [10, ...], [10, ...], [10, ...] on 3 GPUs
    net = model
# model.to(device)

# ================================Optimizer========================================

optimizer = optim.Adam(model.parameters(), lr=BASE_LEARNING_RATE, weight_decay=FLAGS.weight_decay)
# optimizer = build_optimizer(cfg, model, BASE_LEARNING_RATE, FLAGS.weight_decay)

VOTENET_CHECKPOINT_PATH = cfg.MODEL.BACKBONE.CKPT_DIR
EXTRACTOR_CHECKPOINT_PATH = cfg.MODEL.ROI_RELATION_HEAD.FEATURE_EXTRACTOR_CKPT_DIR
DEFAULT_CHECKPOINT_PATH = cfg.MODEL.ROI_RELATION_HEAD.CKPT_DIR
CHECKPOINT_PATH = FLAGS.checkpoint_path if FLAGS.checkpoint_path is not None \
    else DEFAULT_CHECKPOINT_PATH
model_dict = model.state_dict()
# ===============================Load Pretrain Checkpoint(Votenet only)=============================

VOTENET_CHECKPOINT_PATH = cfg.MODEL.BACKBONE.CKPT_DIR
# if VOTENET_CHECKPOINT_PATH is not None and os.path.isfile(VOTENET_CHECKPOINT_PATH):
#     pretrained_dict, ckpt = load_pretrain_model(VOTENET_CHECKPOINT_PATH, multi_gpu=False)
#     model_dict.update(pretrained_dict)
#     log_string("-> loaded votenet checkpoint")

# ===============================Load Pretrain Feature Extractor=====================================

if EXTRACTOR_CHECKPOINT_PATH is not None and os.path.isfile(EXTRACTOR_CHECKPOINT_PATH):
    extractor_checkpoint = torch.load(EXTRACTOR_CHECKPOINT_PATH)
    ckpt_dict = extractor_checkpoint['model_state_dict']
    # processed_dict = load_ckpt_from_single_gpu_to_multi_gpu(checkpoint['model_state_dict'])
    for k in list(ckpt_dict.keys()):
        if 'feat_extractor' not in k:
            ckpt_dict.pop(k)
    model_dict.update(ckpt_dict)
    log_string("-> loaded feature extractor checkpoint")

# ===============================Load Checkpoint(whole model)========================================

# Load checkpoint if there is any
it = -1
start_epoch = 0
if CHECKPOINT_PATH is not None and os.path.isfile(CHECKPOINT_PATH):
    checkpoint = torch.load(CHECKPOINT_PATH)
    ckpt_dict = checkpoint['model_state_dict']
    # processed_dict = load_ckpt_from_single_gpu_to_multi_gpu(checkpoint['model_state_dict'])
    model_dict.update(ckpt_dict)
    optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
    start_epoch = checkpoint['epoch']
    log_string("-> loaded checkpoint %s (epoch: %d)" % (CHECKPOINT_PATH, start_epoch))

model.load_state_dict(model_dict)
# ==================================BN=========================================

BN_MOMENTUM_INIT = 0.5
BN_MOMENTUM_MAX = 0.001
bn_lbmd = lambda it: max(BN_MOMENTUM_INIT * BN_DECAY_RATE ** (int(it / BN_DECAY_STEP)), BN_MOMENTUM_MAX)
bnm_scheduler = BNMomentumScheduler(model, bn_lambda=bn_lbmd, last_epoch=start_epoch - 1)


# ====================================LR==========================================

def get_current_lr(epoch):
    lr = BASE_LEARNING_RATE
    for i, lr_decay_epoch in enumerate(LR_DECAY_STEPS):
        if epoch >= lr_decay_epoch:
            lr *= LR_DECAY_RATES[i]
    return lr


def adjust_learning_rate(optimizer, epoch):
    lr = get_current_lr(epoch)
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr


# ===================================MISC=========================================

# Used for AP calculation
CONFIG_DICT = {'remove_empty_box': False, 'use_3d_nms': True,
               'nms_iou': 0.25, 'use_old_type_nms': False, 'cls_nms': True,
               'per_class_proposal': True, 'conf_thresh': 0.05,
               'dataset_config': DATASET_CONFIG}

def fix_votenet_module(model):
    model.detector.eval()
    for _, param in model.detector.named_parameters():
        param.requires_grad = False

# ===================================TRAIN=========================================


def train_one_epoch():
    stat_dict = {}
    stat_dict['loss_rel'] = 0
    stat_dict['loss_obj'] = 0
    adjust_learning_rate(optimizer, EPOCH_CNT)
    bnm_scheduler.step()  # decay BN momentum
    model.train()  # set model to training mode
    for batch_idx, batch_data_label in enumerate(TRAIN_DATALOADER):
        for key in batch_data_label[0].keys():
            if isinstance(batch_data_label[0][key], torch.Tensor):
                batch_data_label[0][key] = batch_data_label[0][key].cuda()

        # Forward pass
        optimizer.zero_grad()
        end_dict = model(batch_data_label[0], batch_data_label[1].cuda(), batch_data_label[2].cuda(),
                         batch_data_label[3].cuda())

        from model.modeling.roi_head.relation_head.loss import make_roi_relation_loss_evaluator
        loss_processor = make_roi_relation_loss_evaluator(cfg)
        loss_relation, loss_refine, rel_cls_acc, obj_cls_acc = loss_processor(end_dict['proposals'],
                                                                              end_dict['relation_labels'],
                                                                              end_dict['relation_logits'],
                                                                              end_dict['refine_logits'],
                                                                              has_obj=False,
                                                                              has_rel=True)
        loss = loss_relation
        end_dict['obj_cls_acc'] = obj_cls_acc
        end_dict['rel_cls_acc'] = rel_cls_acc

        loss.backward()
        optimizer.step()

        # collect statistics
        stat_dict['loss_rel'] += loss_relation.item()
        stat_dict['loss_obj'] += loss_refine.item()

        batch_interval = 10
        if (batch_idx + 1) % batch_interval == 0:
            log_string(' ---- batch: %03d ----' % (batch_idx + 1))
            print("obj cls acc: " + str(end_dict['obj_cls_acc']))
            print("rel cls acc: " + str(end_dict['rel_cls_acc']))
            for key in sorted(stat_dict.keys()):
                log_string('mean %s: %f' % (key, stat_dict[key] / batch_interval))
                stat_dict[key] = 0


def eval_one_epoch():
    stat_dict = {}
    bnm_scheduler.step()
    model.eval()
    r3p2 = BatchMetricProcessor(K=3, P=0.2)
    r50p2 = RecallKProcessor(K=100, P=0.2)
    total_topK_rel_acc = []
    total_obj_cls_acc = []
    total_obj_cls_err = []
    total_lowp_rel_acc = []
    total_recallK = []
    total_mean_recallK = []
    for batch_idx, batch_data_label in enumerate(EVAL_DATALOADER):
        for key in batch_data_label[0].keys():
            if isinstance(batch_data_label[0][key], torch.Tensor):
                batch_data_label[0][key] = batch_data_label[0][key].cuda()

        with torch.no_grad():
            end_dict = model(batch_data_label[0], batch_data_label[1].cuda(), batch_data_label[2].cuda(),
                             batch_data_label[3].cuda())

        obj_labels = end_dict['obj_labels']
        rel_labels = end_dict['relation_labels']
        relation_logits = end_dict['relation_logits']
        refine_logits = end_dict['refine_logits']
        true_pair_idxs = end_dict['true_pair_idxs']

        num_pred_per_scan = []
        num_gt_per_scan = []
        for gtscan in rel_labels:
            num_gt_per_scan.append(gtscan.shape[0])
        for predscan in relation_logits:
            num_pred_per_scan.append(predscan.shape[0])
        per_batched_gt = REL_parse_gt(rel_labels, obj_labels, true_pair_idxs)
        per_batched_pred = REL_parse_output(relation_logits, refine_logits, true_pair_idxs)
        r3p2.step(per_batch_gt=per_batched_gt, per_batch_pred=per_batched_pred)
        r50p2.step(per_batch_gt=per_batched_gt, per_batch_pred=per_batched_pred,
                   num_gt_per_scan=num_gt_per_scan, num_pred_per_scan=num_pred_per_scan)

        if batch_idx % 10 == 0:
            log_string("eval batch: %s" % batch_idx)

        rk_this_batch = r50p2.computeRecallK()
        mean_rk_this_batch = r50p2.computeMeanRecallK()
        obj_cls_acc, total_num_obj = r3p2.compute_obj_cls_accuracy(obj_logits=refine_logits, obj_label=obj_labels)
        t3_rel_acc, obj_cls_err = r3p2.compute_topK_rel_accuracy_sgcls()
        l2_rel_acc = r3p2.compute_LowP_rel_accuracy_sgcls()
        total_recallK.append(rk_this_batch)
        total_obj_cls_acc.append(obj_cls_acc)
        total_topK_rel_acc.append(t3_rel_acc)
        total_obj_cls_err.append(obj_cls_err)
        total_lowp_rel_acc.append(l2_rel_acc)
        total_recallK.append(rk_this_batch)
        total_mean_recallK.append(mean_rk_this_batch)
        log_string("Top%d Accuracy this batch: %s" % (r3p2.K, str(t3_rel_acc)))
        log_string("Low%d Accuracy this batch: %s" % (r3p2.P, str(l2_rel_acc)))
        log_string("Object Classification Accuracy this batch: %s" % str(obj_cls_acc))
        log_string("Object Classification Error Induced Relation Prediction Error this batch: %s" % str(obj_cls_err))
        log_string("Total NUM of Objects in this batch: %s" % str(total_num_obj))
        log_string("Recall@%d this batch: %s" % (r50p2.K, str(rk_this_batch)))
        log_string("Mean Recall@%d this batch: %s" % (r50p2.K, str(mean_rk_this_batch)))
        r3p2.reset()
        r50p2.reset()

        del end_dict
        torch.cuda.empty_cache()

    mean_loss = {}
    for key in sorted(stat_dict.keys()):
        mean_loss[key] = stat_dict[key] / float(batch_idx + 1)

    total_recallK = torch.as_tensor(total_recallK)
    total_mean_recallK = torch.as_tensor(total_mean_recallK)
    total_topK_rel_acc = torch.as_tensor(total_topK_rel_acc)
    total_lowP_rel_acc = torch.as_tensor(total_lowp_rel_acc)
    total_obj_cls_acc = torch.as_tensor(total_obj_cls_acc)
    total_obj_cls_err = torch.as_tensor(total_obj_cls_err)
    avg_recallK = total_recallK.sum(-1) / total_recallK.shape[-1]
    avg_mean_recallK = total_mean_recallK.sum(-1) / total_mean_recallK.shape[-1]
    avg_obj_cls = total_obj_cls_acc.sum(-1) / total_obj_cls_acc.shape[-1]
    avg_topK_rel_acc = total_topK_rel_acc.sum(-1) / total_topK_rel_acc.shape[-1]
    avg_lowP_rel_acc = total_lowP_rel_acc.sum(-1) / total_lowP_rel_acc.shape[-1]
    avg_obj_err = total_obj_cls_err.sum(-1) / total_obj_cls_err.shape[-1]
    NUM_BATCHES = len(TRAIN_DATALOADER)

    log_string("Recall%d this epoch: %s" % (r50p2.K, str(avg_recallK)))
    log_string("Mean Recall%d this epoch: %s" % (r50p2.K, str(avg_mean_recallK)))
    log_string("Top%d Accuracy this epoch: %s" % (r3p2.K, str(avg_topK_rel_acc)))
    log_string("Low%f Accuracy this epoch: %s" % (r3p2.P, str(avg_lowP_rel_acc)))
    log_string("Object Classification Accuracy this epoch: %s" % str(avg_obj_cls))

    return mean_loss


def train():
    global EPOCH_CNT
    min_loss = 1e10
    loss = 0
    fix_votenet_module(model)
    for epoch in range(start_epoch, MAX_EPOCH):
        EPOCH_CNT = epoch
        log_string('**** EPOCH %03d ****' % (epoch))
        log_string('Current learning rate: %f' % (get_current_lr(epoch)))
        log_string('Current BN decay momentum: %f' % (bnm_scheduler.lmbd(bnm_scheduler.last_epoch)))

        np.random.seed()
        train_one_epoch()
        if EPOCH_CNT == 0 or EPOCH_CNT % 5 == 4 or EPOCH_CNT == 240:  # Eval every 5 epochs
            loss = eval_one_epoch()
            pass

        save_dict = {'epoch': epoch + 1,  # after training one epoch, the start_epoch should be epoch+1
                     'optimizer_state_dict': optimizer.state_dict(),
                     'loss': loss,
                     }
        try:  # with nn.DataParallel() the net is added as a submodule of DataParallel
            save_dict['model_state_dict'] = net.module.state_dict()
        except:
            save_dict['model_state_dict'] = net.state_dict()
        torch.save(save_dict,
                   os.path.join(cfg.MODEL.ROI_RELATION_HEAD.CKPT_DIR))


if __name__ == "__main__":
    train()

    # min_loss = 1e10
    # loss = 0
    # # fix_votenet_module(model, multi_gpu=False)
    # eval_one_epoch()
