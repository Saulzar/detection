import torch
import copy
import torch.optim as optim

import time
import os
import json

from json.decoder import JSONDecodeError

from remote.dataset import decode_dataset, split_tagged, tagged, decode_image
from remote.connection import connect

from detection.models import models
from tools.model import io

from tools import Struct
from arguments import parameters
from tools.parameters import default_parameters

from detection.loss import total_bce

import trainer
import evaluate
import math





def ready(*xs):
    return all(v is not None for v in xs)


class Reset(Exception):
    def __init__(self, env):
        self.env = env


class NotFound(Exception):
    def __init__(self, filename):
        self.filename = filename



def initialise(config, dataset, args):
    data_root = config['root']

    model_args = Struct(num_classes = len(dataset.classes), input_channels = 3)

    # state_dict, creation_params, start_epoch, best = io.load(output_path)
    # model, encoder = io.create(models, creation_params, model_args)
    # model.load_state_dict(state_dict)

    model, encoder = io.create(models, Struct (model='fcn'), model_args)
    parameters = model.parameter_groups(args.lr, args.fine_tuning)

    optimizer = optim.SGD(parameters, lr=args.lr, momentum=args.momentum)

    best = 0.0
    best_model = copy.deepcopy(model)
    epoch = 0

    output_path = os.path.join(data_root, args.log, "model.pth")

    return Struct(**locals())


def encode_box(box):
    lower, upper =  box[:2].tolist(), box[2:].tolist()
    return {'lower':lower, 'upper':upper}


def detect_request(env, file, nms_params, device):
    path = os.path.join(env.data_root, file)

    if not os.path.isfile(path):
        raise NotFound(file)

    image = env.dataset.load_testing(path, env.args)
    boxes, labels, confs = evaluate.evaluate_image(env.model, image, env.encoder, nms_params, device)

    n = len(boxes)
    assert n == len(labels) and n == len(confs)

    def detection(args):
        box, label, conf = args
        shape = tagged('BoxShape', encode_box(box.cpu()))

        return {
            'annotation' : {'shape': shape, 'label':label.item()},
            'confidence' : conf.item()
        }

    return list(map(detection, zip(boxes, labels, confs)))



def log_lerp(range, t):
    begin, end = range
    return math.exp(math.log(begin) * (1 - t) + math.log(end) * t)

def adjust_learning_rate(lr, optimizer):
    for param_group in optimizer.param_groups:
        modified = lr * param_group['modifier'] if 'modifier' in param_group else lr
        param_group['lr'] = modified




def train(conn):

    args = default_parameters(parameters).merge(Struct(
        batch_size = 4,
        epoch_size = 512,
        num_workers = 4,
        image_size = 440,
        lr = 1.0,
        momentum = 0.5,
        down_scale = 1
    ))

    print(args)

    env = None
    device = torch.cuda.current_device()


    def send_command(command, data):
        str = json.dumps(tagged(command, data))
        conn.send(str)


    def process_command(str):
        nonlocal env, device

        try:
            tag, data = split_tagged(json.loads(str))
            print("recieved command: " + tag)

            if tag == 'TrainerDataset':

                config, dataset = decode_dataset(data)
                env = initialise(config, dataset, args)

                raise Reset(env)

            elif tag == 'TrainerUpdate':
                file, image_data = data

                image = decode_image(image_data, env.config)
                category = image_data['category']
                print ("updating '" + file + "' in " + category)

                env.dataset.update_image(file, image, category)


            elif tag == 'TrainerDetect':
                clientId, image, nms_prefs = data

                nms_params = {
                    'nms_threshold'     :   nms_prefs['nms'],
                    'class_threshold'   :   nms_prefs['threshold'],
                    'max_detections'    :   nms_prefs['detections']
                }

                if env is not None:
                    result = detect_request(env, image, nms_params, device)
                    send_command('TrainerDetections', (clientId, image, result))

                else:
                    send_command('TrainerReqError', [clientId, "model not available yet"])

            else:
                send_command('TrainerError', "unknown command: " + tag)


        except (JSONDecodeError) as err:
            send_command('TrainerError', repr(err))
            return None


    def poll_command():
        if conn.poll():
            cmd = conn.recv()
            process_command(cmd)

    def train_update(n, total):
        lr = log_lerp((args.lr, args.lr * 0.1), n / total)
        adjust_learning_rate(lr, env.optimizer)
        poll_command()

    def test_update(n, total):
        poll_command()

    def training_cycle():
        model = env.model.to(device)

        if len(env.dataset.train_images) > 0:
            stats = trainer.train(model, env.dataset.sample_train(args, env.encoder),
                        evaluate.eval_train(total_bce, device), env.optimizer, hook=train_update)
            evaluate.summarize_train("train", stats, env.epoch)

        score = 0
        if len(env.dataset.test_images) > 0:
            stats = trainer.test(model, env.dataset.test(args), evaluate.eval_test(env.encoder, device), hook=test_update)
            score = evaluate.summarize_test("test", stats, env.epoch)

        if score >= env.best:
            io.save(env.output_path, model, env.model_args, env.epoch, score)
            env.best = score

        env.epoch = env.epoch + 1


    while(True):
        try:
            if env is not None:
                training_cycle()
            poll_command()

        except Reset as reset:
            env   = reset.env

def run_main():
    p, conn = connect('ws://localhost:2160')

    try:
        train(conn)
    except (KeyboardInterrupt, SystemExit):
        p.terminate()




if __name__ == '__main__':
    run_main()
