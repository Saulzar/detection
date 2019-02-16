import torch
from torch import Tensor
import math
import gc

import torch.nn.functional as F
from torch.autograd import Variable

from tools.image import index_map
import tools.image.cv as cv

import tools.confusion as c

from tools.image.transforms import normalize_batch
from tools import struct, tensor, show_shapes, show_shapes_info, Histogram, ZipList, transpose_structs, transpose_lists, pluck

import detection.box as box
from detection import evaluate
import operator
from functools import reduce


def eval_forward(model, device=torch.cuda.current_device()):

    def f(data):
        norm_data = normalize_batch(data.image).to(device)
        return model(norm_data)

    return f




def count_classes(label, num_classes):

    class_counts = (label + 1).view(-1).bincount(minlength = num_classes + 2)

    return struct(
        ignored  = class_counts[0].item(),
        negative = class_counts[1].item(),
        classes = class_counts[2:],

        positive = class_counts[2:].sum().item(),
        total = label.numel()
    )




def log_counts(class_names, counts, log):
    assert len(class_names) == counts.classes.size(0)

    class_counts = {"class_{}".format(c):count for c, count in zip(class_names, counts.classes) }

    log.scalars("train/boxes",
        struct(ignored = counts.ignored, positive = counts.positive, **class_counts))


def batch_stats(batch):
    assert(batch.dim() == 4 and batch.size(3) == 3)

    batch = batch.float().div_(255)
    flat = batch.view(-1, 3)

    return batch.size(0) * struct(mean=flat.mean(0).cpu(), std=flat.std(0).cpu())


def log_predictions(class_names, histograms, log):

    assert len(histograms) == len(class_names)
    totals = reduce(operator.add, histograms)

    if len(class_names)  > 1:
        for i in range(0, len(class_names)):
            name = class_names[i]

            log.histogram("train/positive", histograms[i].positive,  run = name)
            log.histogram("train/negative", histograms[i].negative,  run = name)

    log.histogram("train/positive", totals.positive)
    log.histogram("train/negative", totals.negative)


def prediction_stats(encoding, prediction, num_bins = 50):

    num_classes = prediction.classification.size(2)
    dist_histogram = torch.LongTensor(2, num_classes, num_bins)

    def class_histogram(i):
        pos_mask = encoding.classification == i + 1
        neg_mask = (encoding.classification > 0) & ~pos_mask

        class_pred = prediction.classification.select(2, i)

        return struct (
            positive = Histogram(values = class_pred[pos_mask], range = (0, 1), num_bins = num_bins),
            negative = Histogram(values = class_pred[neg_mask], range = (0, 1), num_bins = num_bins)
        )

    return ZipList(class_histogram(i) for i in range(0, num_classes))


def eval_stats(classes, device=torch.cuda.current_device()):
    def f(data):
        image = data.image.to(device)

        return struct(
            image = batch_stats(image),
            boxes=data.lengths.sum().item(),
            box_counts=count_classes(data.encoding.classification.to(device), len(classes)),
            size = image.size(0)
        )
    return f

def mean_results(results):
    total = reduce(operator.add, results)
    return total / total.size, total

def sum_results(results):
    return reduce(operator.add, results)



def summarize_stats(results, epoch, globals={}):
    avg = mean_results(results)
    counts = avg.box_counts

    print ("image: mean = {}, std = {}".format(str(avg.image.mean), str(avg.image.std)))
    print("instances: {:.2f}, anchors {:.2f}, anchors/instance {:.2f}, positive {:.2f},  ignored {:.2f}, negative {:.2f} "
        .format(avg.boxes, counts.total, counts.positive / avg.boxes, counts.positive, counts.ignored, counts.negative ))

    balances = counts.classes / counts.positive
    print("class balances: {}".format(str(balances.tolist())))


def train_statistics(data, loss, prediction, encoding, debug = struct(), device=torch.cuda.current_device()):

    files = [(file, loss.item()) for file, loss in zip(data.id, loss.batch.detach())]

    stats = struct(error=loss.total.item(),
        loss = loss.parts._map(Tensor.item),
        size = data.image.size(0),
        instances=data.lengths.sum().item(),
        files = files
    )

    num_classes = prediction.classification.size(2)

    # if debug.predictions:
    #     stats = stats._extend(predictions = prediction_stats(encoding, prediction))

    # if debug.boxes:
    #     stats = stats._extend(box_counts=count_classes(encoding.classification, num_classes))


    return stats

def eval_train(model, encoder, debug = struct(), device=torch.cuda.current_device()):

    def f(data):

        image = data.image.to(device)
        norm_data = normalize_batch(image)
        prediction = model(norm_data)

        loss = encoder.loss(data.encoding, prediction, device=device)

        stats = train_statistics(data, loss, prediction, data.encoding, debug, device)
        return struct(error = loss.total / image.data.size(0), statistics=stats, size = data.image.size(0))

    return f

def summarize_train_stats(results, classes, log):
    totals = sum_results(results)
    avg = totals._subset('loss', 'instances', 'error') / totals.size

    log.scalars("loss", avg.loss._extend(total = avg.error))

    class_names = [c['name']['name'] for c in classes]

    if 'box_counts' in avg:
        log_counts(class_names, avg.box_counts,  log)

    if 'predictions' in avg:
        log_predictions(class_names, totals.predictions,  log)

    loss_str = " + ".join(["{} : {:.3f}".format(k, v) for k, v in sorted(avg.loss.items())])
    return ('n: {}, instances : {:.2f}, loss: {} = {:.3f}'.format(totals.size, avg.instances, loss_str, avg.error))


def summarize_train(name, results, classes, epoch, log):
    summary = summarize_train_stats(results, classes, log)
    print('{} epoch: {} {}'.format(name, epoch, summary))


def splits(size, n, overlap=0):
    div = (size + (n - 1) * overlap) / n

    prev = div
    ranges = [(0, round(div))]
    for i in range(n - 1):
        start = prev - overlap
        prev = start + div
        r = (round(start), round(prev))
        ranges.append(r)

    return ranges

def image_splits(size, n=(1, 1), overlap=0):
    w, h = size
    nx, ny = n

    return [((lx, ly), (ux, uy))
        for lx, ux in splits(w, nx, overlap)
        for ly, uy in splits(h, ny, overlap) ]


def split_image(image, n=(1, 1), overlap=0):

    def sub_image(ranges):
        (lx, ly), (ux, uy) = ranges
        return ((lx, ly), image.narrow(0, ly, uy - ly).narrow(1, lx, ux - lx))

    size = (image.size(1), image.size(0))
    return [ sub_image(r) for r in image_splits(size, n, overlap) ]



def split_sizes(min_splits, aspect):
    """
    Find the split sizes (how many divisions on each axis)
    to split an image, depending on aspect ratio.
    """

    max_aspect = max(aspect, 1/aspect)
    minor, major = 1, 1

    while minor * major < min_splits:
        if major / minor <= pow(max_aspect, 1.8):
            major = major + 1
        else:
            minor = minor + 1
            major = minor

    if aspect >= 1:
        return major, minor
    else:
        return minor, major



def find_split_config(image, max_pixels=None):
    pixels = image.size(1) * image.size(0)

    if max_pixels is not None:
        min_splits = math.ceil(max_pixels / pixels)
        return split_sizes(min_splits, image.size(0) / image.size(1))

    return (1, 1)


# def evaluate_split(model, image, encoder, nms_params=box.nms_defaults, n=(1, 1), overlap=0, device=torch.cuda.current_device()):
#     model.eval()
#     with torch.no_grad():
#         splits = split_image(image, n, overlap)

#         outputs = [evaluate_decode(model, image, encoder, device=device, offset=offset) for offset, image in splits]
#         boxes, label, confs = zip(*outputs)

#         return encoder.nms(torch.cat(boxes, 0), torch.cat(label, 0), torch.cat(confs, 0), nms_params=nms_params)


def evaluate_image(model, image, encoder, nms_params=box.nms_defaults, device=torch.cuda.current_device(), crop_boxes=False):
    model.eval()
    with torch.no_grad():
        prediction = evaluate_decode(model, image, encoder, device, crop_boxes=crop_boxes)
        return  encoder.nms(prediction, nms_params=nms_params)


def evaluate_raw(model, image, device):

    if image.dim() == 3:
        image = image.unsqueeze(0)

    assert image.dim() == 4, "evaluate: expected image of 4d  [1,H,W,C] or 3d [H,W,C]"
    assert image.size(0) == 1, "evaluate: expected batch size of 1 for evaluation"

    def detach(p):
        return p.detach()[0]

    norm_data = normalize_batch(image).to(device)
    predictions = model(norm_data)._map(detach)

    #gc.collect()
    return predictions

def evaluate_decode(model, image, encoder, device, offset = (0, 0), crop_boxes=False):
    preds = evaluate_raw(model, image, device=device)
    p = encoder.decode(image, preds, crop_boxes=crop_boxes)

    offset = torch.Tensor([*offset, *offset]).to(device)
    return p._extend(bbox = p.bbox + offset)

def test_loss(data, encoder, encoding, prediction, debug = struct(), device=torch.cuda.current_device()):
    def unsqueeze(p):
        return p.unsqueeze(0)

    prediction = prediction._map(unsqueeze)
    loss = encoder.loss(encoding, prediction, device=device)
    return train_statistics(data, loss, prediction, encoding, debug, device)


def eval_test(model, encoder,  debug=struct(), nms_params=box.nms_defaults, device=torch.cuda.current_device(), crop_boxes=False):
    def f(data):

        model.eval()
        with torch.no_grad():
            raw_prediction = evaluate_raw(model, data.image, device)
            prediction = encoder.decode(data.image.squeeze(0), raw_prediction, crop_boxes=crop_boxes)

            train_stats = test_loss(data, encoder, data.encoding, raw_prediction, debug=debug, device=device)

            return struct (
                id = data.id,
                target = data.target._map(Tensor.to, device),

                prediction = encoder.nms(prediction, nms_params=nms_params),

                # for summary of loss
                instances=data.lengths.sum().item(),
                train_stats = train_stats,

                size = data.image.size(0),
            )
    return f


def percentiles(t, n=100):
    assert t.dim() == 1
    return torch.from_numpy(np.percentile(t.numpy(), np.arange(0, n)))

def mean(xs):
    return sum(xs) / len(xs)

# def calibrate():
    


def compute_AP(results, class_names):
    thresholds = list(range(30, 100, 5))

    compute_mAP = evaluate.mAP_classes(results, num_classes = len(class_names))
    info = transpose_structs ([compute_mAP(t / 100) for t in thresholds])

    info.classes = transpose_lists(info.classes)
    assert len(info.classes) == len(class_names)

    def summariseAP(ap):
        prs = {t : pr for t, pr in zip(thresholds, ap)}
        mAP = {t : pr.mAP for t, pr in prs.items()}

        return struct(
            pr30 = mAP[30],
            pr50 = mAP[50],
            pr75 = mAP[75],

            mAP = mAP,
            AP = mean([ap for k, ap in mAP.items() if k >= 50])
        )

    return struct (
        total   = summariseAP(info.total),
        classes = {name : summariseAP(ap) for name, ap in zip(class_names, info.classes)}
    )



def summarize_test(name, results, classes, epoch, log):

    class_names = [c['name']['name'] for c in classes]

    summary = compute_AP(results, class_names)
    total, class_aps = summary.total, summary.classes

    mAP_strs ='mAP@30: {:.2f}, 50: {:.2f}, 75: {:.2f}'.format(total.mAP[30], total.mAP[50], total.mAP[75])

    train_summary = summarize_train_stats(pluck('train_stats', results), classes, log)
    print(name + ' epoch: {} AP: {:.2f} mAP@[0.3-0.95]: [{}] {}'.format(epoch, total.AP * 100, mAP_strs, train_summary))

    log.scalars(name, struct(AP = total.AP * 100.0, mAP30 = total.mAP[30] * 100.0, mAP50 = total.mAP[50] * 100.0, mAP75 = total.mAP[75] * 100.0))

    if len(classes) > 1:
        aps = {**class_aps, 'total':total}

        log.scalars("mAP50", {name : ap.mAP[50] * 100.0 for name, ap in aps.items()})
        log.scalars("mAP75", {name : ap.mAP[75] * 100.0 for name, ap in aps.items()})

        log.scalars("AP", {name : ap.AP * 100.0 for name, ap in aps.items()})


    # log.pr_curve("pr@50", summary.curves[0])

    return total.AP
