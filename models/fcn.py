import sys
import math

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable

import itertools
import torchvision.models as m

import models.pretrained as pretrained
from detection import box

from models.common import Conv, Cascade, UpCascade, Residual, Parallel,  \
            DecodeAdd, Decode, init_weights, basic_block, reduce_features, replace_batchnorms
import tools.model.io as io

import torch.nn.init as init
from tools import Struct
from tools.parameters import param


def image_size(inputs):

    if torch.is_tensor(inputs):
        assert(inputs.dim() == 3)
        inputs = inputs.size(1), inputs.size(0)

    assert (len(inputs) == 2)
    return inputs


class Encoder:
    def __init__(self, start_layer, box_sizes, nms_params=box.nms_defaults, match_thresholds=(0.4, 0.5)):
        self.anchor_cache = {}

        self.box_sizes = box_sizes
        self.start_layer = start_layer

        self.nms_params = nms_params
        self.match_thresholds = match_thresholds


    def anchors(self, input_size):
        def layer_size(i):
            scale = 2 ** i
            return (max(1, math.ceil(input_size[0] / scale)), max(1, math.ceil(input_size[1] / scale)))

        if not (input_size in self.anchor_cache):
            layer_dims = [layer_size(self.start_layer + i) for i in range(0, len(self.box_sizes))]
            self.anchor_cache[input_size] = box.make_anchors(self.box_sizes, layer_dims, input_size)

        return self.anchor_cache[input_size]


    def encode(self, inputs, boxes, labels):
        inputs = image_size(inputs)
        return box.encode(boxes, labels, self.anchors(inputs), self.match_thresholds)


    def decode(self, inputs, loc_pred, class_pred):
        assert loc_pred.dim() == 2 and class_pred.dim() == 2

        inputs = image_size(inputs)
        anchor_boxes = self.anchors(inputs).type_as(loc_pred)

        return box.decode_nms(loc_pred, class_pred, anchor_boxes, **self.nms_params)


    def decode_batch(self, inputs, loc_pred, class_pred):
        assert loc_pred.dim() == 3 and class_pred.dim() == 3

        if torch.is_tensor(inputs):
            assert(inputs.dim() == 4)
            inputs = inputs.size(2), inputs.size(1)

        assert len(inputs) == 2
        return [self.decode(inputs, l, c) for l, c in zip(loc_pred, class_pred)]


def init_weights(module):
    def f(m):
        b = -math.log((1 - 0.01)/0.01)
        if isinstance(m, nn.Conv2d) or isinstance(m, nn.ConvTranspose2d) or isinstance(m, nn.Linear):
            init.normal_(m.weight, std=0.01)
            # if not (m.bias is None):
            #     init.constant(m.bias, b)
    module.apply(f)


class FCN(nn.Module):

    def __init__(self, trained, extra, box_sizes, features=32, num_classes=2):
        super().__init__()

        self.encoder = pretrained.make_cascade(trained + extra)
        self.box_sizes = box_sizes

        encoded_sizes = pretrained.encoder_sizes(self.encoder)
        self.reduce = Parallel([Conv(size, features, 1) for size in encoded_sizes])

        def make_decoder():
            decoder = Residual(basic_block(features, features))
            return Decode(features, decoder)

        self.decoder = UpCascade([make_decoder() for i in encoded_sizes])

        assert len(trained + extra) == len(box_sizes), "layers and box sizes differ in length"

        def output(n):
            return nn.Sequential(
                Residual(basic_block(features, features)),
                Residual(basic_block(features, features)),
                Conv(features, n, bias=True))

        self.num_classes = num_classes

        self.classifiers = Parallel([output(len(boxes) * self.num_classes) for boxes in self.box_sizes])
        self.localisers = Parallel([output(len(boxes) * 4) for boxes in self.box_sizes])


        self.trained_modules = nn.ModuleList(trained)
        self.new_modules = nn.ModuleList(extra + [self.reduce, self.decoder, self.classifiers, self.localisers])

        init_weights(self.new_modules)
        #replace_batchnorms(self, 16)



    def forward(self, input):
        layers = self.decoder(self.reduce(self.encoder(input)))
        def join(layers, n):

            def permute(layer):
              out = layer.permute(0, 2, 3, 1).contiguous()
              return out.view(out.size(0), -1, n)

            return torch.cat(list(map(permute, layers)), 1)

        conf = torch.sigmoid(join(self.classifiers(layers), self.num_classes))
        loc = join(self.localisers(layers), 4)

        return (loc, conf)

    def parameter_groups(self, lr, fine_tuning=0.1):
        return [
            {'params': self.trained_modules.parameters(), 'lr':lr, 'modifier': fine_tuning},
            {'params': self.new_modules.parameters(), 'lr':lr, 'modifier': 1.0}
        ]

box_parameters = Struct (
    pos_match = param (0.5, help = "lower iou threshold matching positive anchor boxes in training"),
    neg_match = param (0.4,  help = "upper iou threshold matching negative anchor boxes in training"),

    nms_threshold    = param (0.5, help = "overlap threshold (iou) used in nms to filter duplicates"),
    class_threshold  = param (0.05, help = 'hard threshold used to filter negative boxes'),
    max_detections    = param (100,  help = 'maximum number of detections (for efficiency) in testing')
)

parameters = Struct(
        base_name       = param ("resnet18", help = "name of pretrained resnet to use"),
        features    = param (64, help = "fixed size features in new conv layers"),
        first   = param (3, help = "first layer of anchor boxes, anchor size = 2^(n + 2)"),
        last    = param (7, help = "last layer of anchor boxes")
    )

def extra_layer(inp, features):
    return nn.Sequential(
        *([Conv(inp, features, 1)] if inp != features else []),
        Residual(basic_block(features, features)),
        Residual(basic_block(features, features)),
        Conv(features, features, stride=2)
    )

def split_at(xs, n):
    return xs[:n], xs[n:]


def anchor_sizes(start, end):

    aspects = [1/2, 1, 2]
    scales = [1, pow(2, 1/3), pow(2, 2/3)]

    return [box.anchor_sizes(2 ** (i + 2), aspects, scales) for i in range(start, end + 1)]


def extend_layers(layers, start, end, features=32):
    features_in = pretrained.layer_sizes(layers)[-1]

    num_extra = max(0, end + 1 - len(layers))
    extra_layers = [extra_layer(features_in if i == 0 else features, features) for i in range(0, num_extra)]

    initial, rest =  layers[:start + 1], layers[start + 1:end + 1:]
    return [nn.Sequential(*initial), *rest], [*extra_layers]


def create_fcn(args, num_classes=2, input_channels=3):
    assert input_channels == 3

    backbone, extra = extend_layers(pretrained.get_layers(args.base_name), args.first, args.last, features=args.features)
    box_sizes = anchor_sizes(args.first, args.last)

    nms_params = {
        'nms_threshold'  : args.nms_threshold,
        'class_threshold': args.class_threshold,
        'max_detections'  : args.max_detections
    }


    return FCN(backbone, extra, box_sizes, num_classes=num_classes, features=args.features), \
           Encoder(args.first, box_sizes,
                match_thresholds = (args.neg_match, args.pos_match),
                nms_params = nms_params
            )

models = {
    'fcn' : Struct(create=create_fcn, parameters=parameters + box_parameters)
  }


if __name__ == '__main__':

    _, *cmd_args = sys.argv
    args = io.parse_params(models, cmd_args)

    model, _ = create_fcn(args, 2, 3)

    x = Variable(torch.FloatTensor(4, 3, 600, 600))
    out = model.cuda()(x.cuda())

    [print(y.size()) for y in out]
