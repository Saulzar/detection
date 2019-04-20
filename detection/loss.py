
import torch
import torch.nn.functional as F
from torch.autograd import Variable

from tools import tensor, struct, show_shapes


# makes a one_hot vector from class labels
def one_hot(label, num_classes):
    t = label.new(label.size(0), num_classes).zero_()
    return t.scatter_(1, label.unsqueeze(1), 1)

# makes a one_hot vector from class labels with an 'ignored' case as 0 (which is trimmed)
def one_hot_with_ignored(label, num_classes):
    return one_hot(label, num_classes + 1)[:, 1:]



def all_eq(xs):
    return all(map(lambda x: x == xs[0], xs))




# def focal_loss_softmax(class_target, class_pred, gamma=2, alpha=0.25, eps = 1e-6):
#     #ce = F.cross_entropy(class_pred, class_target, size_average = False)

#     p = F.softmax(class_pred, 1).clamp(eps, 1 - eps)
#     p = p.gather(1, class_target.unsqueeze(1))

#     errs = -(1 - p).pow(gamma) * p.log()

#     return errs.sum()


def focal_loss_label(target_labels, pred, class_weights, gamma=2, eps=1e-6):
    num_classes = pred.size(1)
    target = one_hot_with_ignored(target_labels.detach(), num_classes).float()

    alpha = class_weights[target_labels].unsqueeze(1)
    return focal_loss_bce(target, pred, alpha, gamma=gamma, eps=eps)


def focal_loss_bce(target, pred, alpha, gamma=2, eps=1e-6):
    target_inv = 1 - target

    p_t = target * pred + target_inv * (1 - pred)
    a_t = target * alpha      + target_inv * (1 - alpha)

    p_t = p_t.clamp(min=eps, max=1-eps)

    errs = -a_t * (1 - p_t).pow(gamma) * p_t.log()
    return errs




# def mask_valid(target, prediction):

#     size_of = lambda t: (t.size(0), t.size(1))
#     sizes = list(map(size_of, [target.location, target.classification, prediction.location, prediction.classification]))
#     assert all_eq (sizes), "total_loss: number of target and prediction differ, " + str(sizes)

#     num_classes = prediction.classification.size(2)

#     pos_mask = (target.classification > 0).unsqueeze(2).expand_as(prediction.location)
#     valid_mask = target.classification >= 0
#     prediction_mask = valid_mask.unsqueeze(2).expand_as(prediction.classification)

#     target = struct(
#         location = target.location[pos_mask], 
#         classification   = target.classification[valid_mask])

#     prediction = struct(
#         location = prediction.location[pos_mask],
#         classification   = prediction.classification[prediction_mask].view(-1, num_classes))

#     return (target, prediction)

# def focal_loss(target, prediction, balance=10, gamma=2, alpha=0.25, eps=1e-6, averaging = False):

#     batch = target.location.size(0)
#     n = prediction.location.size(0) + 1    

#     target, prediction = mask_valid(target, prediction)
    
#     class_loss = focal_loss_bce(target.classification, prediction.classification, gamma=gamma, alpha=alpha).sum()
#     loc_loss = F.smooth_l1_loss(prediction.location, target.location, reduction='sum')

#     return struct(classification = class_loss / (batch * balance), location = loc_loss / batch)

def overlap_focal_loss(target, prediction, class_weights, balance=4, gamma=2, eps=1e-6):
    assert false, "not implemented"


def batch_focal_loss(target, prediction, class_weights, balance=4, gamma=2, eps=1e-6):
    batch = target.location.size(0)
    num_classes = prediction.classification.size(2)

    class_weights = prediction.classification.new([0.0, *class_weights])

    neg_mask = (target.classification == 0).unsqueeze(2).expand_as(prediction.location)
    invalid_mask = (target.classification < 0).unsqueeze(2).expand_as(prediction.classification)
    
    class_loss = focal_loss_label(target.classification.clamp(min = 0).view(-1), 
        prediction.classification.view(-1, num_classes), class_weights=class_weights, gamma=gamma)

    loc_loss = F.smooth_l1_loss(prediction.location.view(-1), target.location.view(-1), reduction='none')

    class_loss = class_loss.view_as(prediction.classification).masked_fill_(invalid_mask, 0)
    loc_loss = loc_loss.view_as(prediction.location).masked_fill_(neg_mask, 0)

    class_loss = class_loss.view(batch, -1).sum(1) / balance
    loc_loss = loc_loss.view(batch, -1).sum(1) 

    parts = struct(classification = class_loss.sum(), location = loc_loss.sum())
    batch = class_loss + loc_loss

    return struct(total = batch.sum(), parts = parts, batch = batch)


# def weighted_focal_loss(target, prediction, balance=4, gamma=2, alpha=0.25, eps=1e-6, averaging = False):

#     batch = target.location.size(0)
#     n = prediction.location.size(0) + 1    

#     neg_mask = (target.classification == 0).unsqueeze(2).expand_as(prediction.location)
#     loc_loss = F.smooth_l1_loss(prediction.location.view(-1), target.location.view(-1), reduction='none')

#     loc_loss = loc_loss.view_as(prediction.location).masked_fill_(neg_mask, 0)


#     return struct(classification = class_loss / balance, location = loc_loss)