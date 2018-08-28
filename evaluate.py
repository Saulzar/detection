import torch
import math
import gc

import torch.nn.functional as F
from torch.autograd import Variable

from tools.image import index_map
import tools.image.cv as cv

import tools.confusion as c

from tools.image.transforms import normalize_batch
from tools import Struct, tensor

import detection.box as box

from detection import evaluate


def to_device(t, device):
    if isinstance(t, list):
        return [to_device(x, device) for x in t]

    return t.to(device)


def eval_train(loss_func, device=torch.cuda.current_device()):

    def f(model, data):
        image, targets, lengths = data['image'], data['targets'], data['lengths']

        norm_data = to_device(normalize_batch(image), device)
        predictions = model(norm_data)

        class_loss, loc_loss, n = loss_func(to_device(targets, device), predictions)
        error = class_loss + loc_loss

        stats = Struct(error=error.item(), class_loss=class_loss.item(), loc_loss=loc_loss.item(), size=image.size(0), boxes=lengths.sum(), matches=n)
        return Struct(error=error, statistics=stats)

    return f

def summarize_train(name, stats, epoch, globals={}):
    avg_loss = stats.error / stats.size
    avg_loc = stats.loc_loss / stats.size
    avg_class = stats.class_loss / stats.size
    avg_matched = stats.matches / stats.size
    avg_boxes= stats.boxes / stats.size

    print(name + ' epoch: {}\tBoxes (truth, matches) {:.2f} {:.2f} \tLoss (class, loc, total): {:.6f}, {:.6f}, {:.6f}'.format(epoch, avg_boxes, avg_matched, avg_class, avg_loc, avg_loss))
    return avg_loss


def evaluate_image(model, image, encoder, nms_params=box.nms_defaults, device=torch.cuda.current_device()):
    return evaluate_batch(model, image.unsqueeze(0), encoder, nms_params, device)


def evaluate_batch(model, images, encoder, nms_params, device):
    assert images.size(0) == 1, "evaluate_batch: expected batch size of 1 for evaluation"

    norm_data = to_device(normalize_batch(images), device)
    loc_preds, class_preds = model(norm_data)

    loc_preds = loc_preds.detach()
    class_preds = class_preds.detach()

    gc.collect()
    return encoder.decode_batch(images, loc_preds.detach(), class_preds.detach(), nms_params=nms_params)[0]



def eval_test(encoder, nms_params=box.nms_defaults, device=torch.cuda.current_device()):

    def f(model, data):

        images, target_boxes, target_labels = data['image'], data['boxes'], data['labels']
        boxes, labels, confs = evaluate_batch(model, images, encoder, nms_params, device)

        thresholds = [0.5 + inc * 0.05 for inc in range(0, 10)]
        scores = torch.FloatTensor(10).zero_()

        def mAP(iou):
            _, _, score = evaluate.mAP(boxes, labels, confs, target_boxes.type_as(boxes).squeeze(0), target_labels.type_as(labels).squeeze(0), threshold = iou)
            return score

        if(boxes.size(0) > 0):
            scores = torch.FloatTensor([mAP(t) for t in thresholds])


        stats = Struct(AP=scores.mean(), mAPs=scores, size=1)
        return Struct(statistics=stats)

    return f

def summarize_test(name, stats, epoch, globals={}):
    mAPs =' '.join(['{:.2f}'.format(mAP * 100.0) for mAP in stats.mAPs / stats.size])
    AP = stats.AP * 100.0 / stats.size
    print(name + ' epoch: {}\t AP: {:.2f}\t mAPs@[0.5-0.95]: [{}]'.format(epoch, AP, mAPs))

    return AP
