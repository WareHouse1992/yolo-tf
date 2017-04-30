"""
Copyright (C) 2017, 申瑞珉 (Ruimin Shen)

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Lesser General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

import configparser
import os
import re
import math
import numpy as np
import pandas as pd
import tensorflow as tf
import utils
import yolo.inference as inference


def calc_cell_xy(cell_height, cell_width, dtype=np.float32):
    cell_base = np.zeros([cell_height, cell_width, 2], dtype=dtype)
    for y in range(cell_height):
        for x in range(cell_width):
            cell_base[y, x, :] = [x, y]
    return cell_base


class Model(object):
    def __init__(self, net, scope, classes, boxes_per_cell):
        _, self.cell_height, self.cell_width, _ = tf.get_default_graph().get_tensor_by_name(scope + '/conv:0').get_shape().as_list()
        cells = self.cell_height * self.cell_width
        with tf.name_scope('labels'):
            end = cells * classes
            self.prob = tf.reshape(net[:, :end], [-1, cells, 1, classes], name='prob')
            output = tf.reshape(net[:, end:], [-1, cells, boxes_per_cell, 5], name='output')
            end = 1
            self.iou = tf.identity(output[:, :, :, end], name='iou')
            start = end
            end += 2
            self.offset_xy = tf.identity(output[:, :, :, start:end], name='offset_xy')
            wh01_sqrt_base = tf.identity(output[:, :, :, end:], name='wh01_sqrt_base')
            wh01 = tf.identity(wh01_sqrt_base ** 2, name='wh01')
            wh01_sqrt = tf.abs(wh01_sqrt_base, name='wh01_sqrt')
            self.coords = tf.concat([self.offset_xy, wh01_sqrt], -1, name='coords')
            self.wh = tf.identity(wh01 * [self.cell_width, self.cell_height], name='wh')
            _wh = self.wh / 2
            self.offset_xy_min = tf.identity(self.offset_xy - _wh, name='offset_xy_min')
            self.offset_xy_max = tf.identity(self.offset_xy + _wh, name='offset_xy_max')
            self.areas = tf.identity(self.wh[:, :, :, 0] * self.wh[:, :, :, 1], name='areas')
        with tf.name_scope('detection'):
            cell_xy = calc_cell_xy(self.cell_height, self.cell_width).reshape([1, cells, 1, 2])
            self.xy = tf.identity(cell_xy + self.offset_xy, name='xy')
            self.xy_min = tf.identity(cell_xy + self.offset_xy_min, name='xy_min')
            self.xy_max = tf.identity(cell_xy + self.offset_xy_max, name='xy_max')
            self.conf = tf.identity(tf.expand_dims(self.iou, -1) * self.prob, name='conf')
        with tf.name_scope('regularizer'):
            self.regularizer = tf.reduce_sum([tf.nn.l2_loss(v) for v in utils.match_trainable_variables(r'[_\w\d]+\/fc\d*\/weights')], name='regularizer')
        self.classes = classes
        self.boxes_per_cell = boxes_per_cell


class Objectives(dict):
    def __init__(self, model, mask, prob, coords, offset_xy_min, offset_xy_max, areas):
        self.model = model
        self.mask = mask
        self.prob = prob
        self.coords = coords
        self.offset_xy_min = offset_xy_min
        self.offset_xy_max = offset_xy_max
        self.areas = areas
        with tf.name_scope('iou'):
            _offset_xy_min = tf.maximum(model.offset_xy_min, self.offset_xy_min) 
            _offset_xy_max = tf.minimum(model.offset_xy_max, self.offset_xy_max)
            _wh = tf.maximum(_offset_xy_max - _offset_xy_min, 0.0)
            _areas = _wh[:, :, :, 0] * _wh[:, :, :, 1]
            areas = tf.maximum(self.areas + model.areas - _areas, 1e-10)
            iou = tf.truediv(_areas, areas, name='iou')
        with tf.name_scope('mask'):
            max_iou = tf.reduce_max(iou, 2, True, name='max_iou')
            mask_max_iou = tf.to_float(tf.equal(iou, max_iou, name='mask_max_iou'))
            mask_best = tf.identity(self.mask * mask_max_iou, name='mask_best')
            mask_normal = tf.identity(1 - mask_best, name='mask_normal')
        iou_diff = tf.identity(model.iou - iou, name='iou_diff')
        with tf.name_scope('objectives'):
            self['prob'] = tf.nn.l2_loss(tf.expand_dims(self.mask, -1) * model.prob - self.prob, name='prob')
            self['iou_best'] = tf.nn.l2_loss(mask_best * iou_diff, name='mask_best')
            self['iou_normal'] = tf.nn.l2_loss(mask_normal * iou_diff, name='mask_normal')
            self['coords'] = tf.nn.l2_loss(tf.expand_dims(mask_best, -1) * (model.coords - self.coords), name='coords')


class Builder(object):
    def __init__(self, args, config):
        section = __name__.split('.')[-1]
        self.args = args
        self.config = config
        with open(os.path.expanduser(os.path.expandvars(config.get(section, 'names'))), 'r') as f:
            self.names = [line.strip() for line in f]
        self.boxes_per_cell = config.getint(section, 'boxes_per_cell')
        self.inference = getattr(inference, config.get(section, 'inference'))
    
    def __call__(self, data, training=False):
        _scope, net = self.inference(data, len(self.names), self.boxes_per_cell, training=training)
        with tf.name_scope('model'):
            self.model = Model(net, _scope, len(self.names), self.boxes_per_cell)
    
    def loss(self, labels):
        section = __name__.split('.')[-1]
        with tf.name_scope('objectives'):
            self.objectives = Objectives(self.model, *labels)
            with tf.variable_scope('hparam'):
                self.hparam = dict([(key, tf.Variable(float(s), name='hparam_' + key, trainable=False)) for key, s in self.config.items(section + '_hparam')])
                self.hparam_regularizer = tf.Variable(self.config.getfloat(section, 'hparam'), name='hparam_regularizer', trainable=False)
            with tf.name_scope('loss_objectives'):
                loss_objectives = tf.reduce_sum([self.objectives[key] * self.hparam[key] for key in self.objectives], name='loss_objectives')
            loss_regularizer = tf.identity(self.hparam_regularizer * self.model.regularizer, name='loss_regularizer')
            loss = tf.identity(loss_objectives + loss_regularizer, name='loss')
        return loss
