# -*- coding: utf-8 -*-
"""SPNet v1 model for Keras.
"""
import numpy as np
import sys

from keras.models import Model

from ..config import ModelConfig
from ..losses import pose_regression_loss
from ..layers import *
from ..utils import *
from .blocks import *

from .common import residual
from .common import downscaling
from .common import upscaling
from .common import add_tensorlist
from .common import concat_tensorlist
from .common import set_trainable_layers
from .common import copy_replica_layers


def prediction_branch(x, cfg, pred_activate=True, replica=None,
        forward_maps=True, name=None):

    num_pred = cfg.num_joints

    num_features = K.int_shape(x)[-1]

    x = relu(x, name=appstr(name, '_act1'))
    pred_maps = conv2d(x, num_pred, (1, 1), name=appstr(name, '_conv1'))

    if replica:
        replica = conv2d(x, num_pred, (1, 1),
                name=appstr(name, '_conv1_replica'))

    if forward_maps:
        x = conv2d(x, num_pred, (1, 1), name=appstr(name, '_fw_maps'))
        x = concatenate([x, pred_maps])
    else:
        x = pred_maps

    if pred_activate:
        x = relu(x, name=appstr(name, '_act2'))
    x = conv2d(x, num_features, (1, 1), name=appstr(name, '_conv2'))

    return x, pred_maps, replica


def action_prediction_early_fusion(xa, af, cfg, name=None):

    num_actions = cfg.num_actions

    num_features = max(cfg.num_pose_features, cfg.num_visual_features)
    num_pose_features = cfg.num_pose_features
    num_visual_features = cfg.num_visual_features

    shortname = name[0:7] if name is not None else None

    action = []

    """Apply individual softmax per dataset (set of actions)."""
    def _individual_action_prediction(hlist, name=None):
        for i in range(len(hlist)):
            x = global_max_min_pooling(hlist[i])
            x = Activation('softmax', name=appstr(name, '%d' % i))(x)
            action.append(x)

    """Generic prediction block for both pose and apperance features."""
    def _prediction(x, name=None, shortname=None):
        num_features = K.int_shape(x)[-1]

        ident = x
        x = BatchNormalization(name=appstr(name, '_bn1'))(x)
        x = relu(x, name=appstr(name, '_act1'))
        x1 = conv2d(x, num_features, (3, 3), name=appstr(name, '_conv1'))

        x = max_min_pooling(x1, (2, 2))
        x = BatchNormalization(name=appstr(name, '_bn2'))(x)
        x = relu(x, name=appstr(name, '_act2'))
        hlist = []
        for i in range(len(num_actions)):
            nact = num_actions[i]
            h = conv2d(x, nact, (3, 3), name=appstr(name, '_conv2h%d' % i))
            hlist.append(h)

        _individual_action_prediction(hlist, name=shortname)
        h = concat_tensorlist(hlist)

        x = UpSampling2D((2, 2))(h)
        x = relu(x, name=appstr(name, '_act3'))
        x = conv2d(x, num_features, (3, 3), name=appstr(name, '_conv3'))
        x = add([ident, x1, x])

        return x

    """Define padding strategy."""
    num_frames = 8 
    num_joints = 20
    time_stride = 2 if num_frames >= 16 else 1
    get_pad = lambda div, n: int(div*np.ceil(n / div) - n)
    joints_pad = get_pad(4, num_joints)
    frames_pad = get_pad(2 * time_stride, num_frames)
    top_pad = frames_pad // 2
    bottom_pad = (frames_pad + 1) // 2
    left_pad = joints_pad // 2
    right_pad = (joints_pad + 1) // 2
    
    """Appearance features."""
    x = conv2d(af, num_visual_features, (1, 1), name=appstr(name, '_v_conv0'))
    def get_tensor(x):
        return tf.convert_to_tensor(np.random.rand(1, 8, 10, 160).astype(np.float32))
    x1 = Lambda(get_tensor)(x)
    print("shape of pose features:")
    print(x1.shape)
    if top_pad + bottom_pad + left_pad + right_pad > 0:
        x = ZeroPadding2D(((top_pad, bottom_pad), (left_pad, right_pad)))(x)
    x2 = maxpooling2d(x, (2, 2), strides=(time_stride, 2))
    print("shape of appearance features:")
    print(x2.shape)
    """Feature fusion."""
    fusion = [x1, x2]
    if xa is not None:
        fusion.append(xa)
    x = x2
    #x = concat_tensorlist(fusion)
    # x = add_tensorlist(fusion)
    x = residual(x, (3, 3), out_size=num_features, convtype='normal',
            features_div=4, name=appstr(name, '_r2'))

    xa = _prediction(x, name=appstr(name, '_pred'),
            shortname=appstr(shortname, '_a'))
    print("early fusion returns: action, xa:")
    print(action)
    print(xa)
    return action, xa


def prediction_block(heatmap, xp, xa, zp, outlist, cfg, do_action, name=None):

    dim = cfg.dim
    kernel_size = cfg.kernel_size
    xmin = cfg.xmin
    ymin = cfg.ymin
    sam_alpha = cfg.sam_alpha
    num_features = K.int_shape(xp)[-1]
    replica = cfg.pose_replica and do_action
    dbg_decoupled_pose = cfg.dbg_decoupled_pose and do_action
    dbg_decoupled_h = cfg.dbg_decoupled_h and do_action
    new_heatmap = tf.image.resize(heatmap[0], [xp.shape[2].value, xp.shape[2].value])
    """Visual features (for action only)."""
    action = []
    if do_action:
        if 'act_cnt' not in globals():
            global act_cnt
            act_cnt = 0
        act_cnt += 1
        act_name = 'act%d' % act_cnt
        act_h = Lambda(lambda x:new_heatmap)(heatmap)
        #act_h = tf.convert_to_tensor(np.random.rand(1, 8, xp.shape[2].value, xp.shape[2].value, 20).astype(np.float32))
        print("shape of heatmap:")
        print(act_h.shape)
        af = kronecker_prod(act_h, zp, name=appstr(act_name, '_kron'))
        print("shape after kronecker_prod:")
        print(af.shape)
        action, xa = action_prediction_early_fusion(xa, af, cfg,
                name=appstr(act_name, '_action'))
    if do_action:
        outlist[1] += action

    sys.stdout.flush()

    return xp, xa


def downscaling_pyramid(heatmap, lp, la, lzp, outlist, cfg, do_action, name=None):

    assert len(lp) == len(la), \
            'Pose and action must have the same number of levels!'
    xp = lp[0]
    xa = la[0]
    if lzp[0] is None:
        lzp[0] = xp

    for i in range(1, len(lp)):
        num_features = K.int_shape(xp)[-1] + cfg.growth

        xp = downscaling(xp, cfg, out_size=num_features,
                name=appstr(name, '_du%d' % i))

        if lzp[i] is None:
            lzp[i] = xp

        if lp[i] is not None:
            xp = add([xp, lp[i]])

        if xa is not None and do_action:
            xa = residual(xa, (3, 3), name=appstr(name, '_du%d_action_r0' % i))
            if la[i] is not None:
                xa = add([xa, la[i]])

        xp, xa = prediction_block(heatmap, xp, xa, lzp[i], outlist, cfg, do_action,
                name=appstr(name, '_pb%d' % i))

        lp[i] = xp # lateral pose connection
        la[i] = xa # lateral action connection


def upscaling_pyramid(heatmap, lp, la, lzp, outlist, cfg, do_action, name=None):

    assert len(lp) == len(la), \
            'Pose and action must have the same number of levels!'
    xp = lp[-1]
    xa = la[-1]
    if lzp[0] is None:
        lzp[0] = xp

    for i in range(len(lp)-1)[::-1]:
        num_features = K.int_shape(xp)[-1] - cfg.growth

        xp = upscaling(xp, cfg, out_size=num_features,
                name=appstr(name, '_uu%d' % i))

        if lzp[i] is None:
            lzp[i] = xp

        if lp[i] is not None:
            xp = add([xp, lp[i]])

        if xa is not None and do_action:
            xa = residual(xa, (3, 3), name=appstr(name, '_uu%d_action_r0' % i))
            if la[i] is not None:
                xa = add([xa, la[i]])

        xp, xa = prediction_block(heatmap, xp, xa, lzp[i], outlist, cfg, do_action,
                name=appstr(name, '_pb%d' % i))
        print("return value from prediction block in upscaling pyramid: xp, xa")
        print(xp)
        print()
        print(xa)
        lp[i] = xp # lateral pose connection
        la[i] = xa # lateral action connection


def entry_flow(x, cfg):

    growth = cfg.growth
    image_div = cfg.image_div
    downsampling_type = cfg.downsampling_type

    assert (image_div & (image_div - 1) == 0) and image_div >= 4, \
            'Invalid image_div ({}).'.format(image_div)
    assert downsampling_type in ['maxpooling', 'conv'], \
            'Invalid downsampling_type ({}).'.format(downsampling_type)

    x = conv2d(x, 64, (7, 7), strides=(2, 2), name='conv1')
    x = residual(x, (3, 3), out_size=growth, convtype='normal', name='res0')
    x = maxpooling2d(x, (3, 3), strides=(2, 2))

    x = residual(x, (3, 3), out_size=2*growth, convtype='normal', name='res1')
    x = residual(x, (3, 3), out_size=2*growth, convtype='normal', name='res2')

    num_features = 2*growth
    res_cnt = 2
    div_factor = 4
    s1 = (2, 2) if downsampling_type == 'conv' else (1, 1)

    while div_factor < image_div:
        num_features += growth
        if downsampling_type == 'maxpooling':
            x = maxpooling2d(x, (2, 2), strides=(2, 2))

        x = residual(x, (3, 3), out_size=num_features, strides=s1,
                convtype='normal', name='res%d' % (res_cnt + 1))
        x = residual(x, (3, 3), out_size=num_features,
                convtype='normal', name='res%d' % (res_cnt + 2))
        res_cnt += 2
        div_factor *= 2

    return x


def build(cfg, stop_grad_stem=False):
    """Sequential Pyramid Networks for 3D human pose estimation and
    action recognition.
    """
    assert type(cfg) == ModelConfig, \
            'type(cfg) ({}) is not ModelConfig'.format(type(cfg))

    input_shape = cfg.input_shape
    assert len(input_shape) in [3, 4], \
            'Invalid input_shape ({})'.format(input_shape)

    inp = Input(shape=input_shape)
    input_heatmap = Input(shape=(8, 56, 56, 20))
    outlist = [] # Holds [[poses], [dbg1], [action1], [actions2], ...]
    for i in range(len(cfg.num_actions) + 1 + 2*cfg.dbg_decoupled_pose):
        outlist.append([])

    if len(input_shape) == 3:
        num_rows, num_cols, _ = input_shape
    else:
        num_frames, num_rows, num_cols, _ = input_shape

    cfg.xmin = 1 / (2 * num_cols)
    cfg.ymin = 1 / (2 * num_rows)

    x = entry_flow(inp, cfg)
    if stop_grad_stem:
        x = Lambda(lambda x: K.stop_gradient(x))(x)

    lp = []
    la = []
    lzp = []
    for i in range(cfg.num_levels):
        lp.append(None)
        la.append(None)
        lzp.append(None)

    lp[0] = x
    for pyr in range(cfg.num_pyramids):

        do_action = (pyr + 1) in cfg.action_pyramids

        if pyr % 2 == 0: # Even pyramids (0, 2, ...)
            downscaling_pyramid(input_heatmap, lp, la, lzp, outlist, cfg, do_action,
                    name='dp%d' % (pyr+1))

        else: # Odd pyramids (1, 3, ...)
            upscaling_pyramid(input_heatmap, lp, la, lzp, outlist, cfg, do_action,
                    name='up%d' % (pyr+1))

    outputs = []
    for o in outlist:
        outputs += o

    model = Model(inputs=[inp, input_heatmap], outputs=outputs, name='SPNet')

    return model


def get_num_predictions(num_pyramids, num_levels):
    return num_pyramids * (num_levels - 1)


def split_model(full_model, cfg, interlaced=False, model_names=[None, None]):

    num_pose_pred = get_num_predictions(cfg.num_pyramids, cfg.num_levels)
    num_act_pred = get_num_predictions(len(cfg.action_pyramids), cfg.num_levels)

    if interlaced:
        out_p = []
        out_a = []

        idx = 0
        for i in range(num_act_pred):
            out_a.append(full_model.outputs[idx])
            
        modelp = Model(full_model.input, out_p, name=model_names[0])
        modela = Model(full_model.input, out_a, name=model_names[1])

    else:
        modela = Model(full_model.input, full_model.outputs[:num_pose_pred],
                name=model_names[0])
        modelp = Model(full_model.input, full_model.outputs[num_pose_pred:],
                name=model_names[1])

    return [modelp, modela]


def compile_split_models(full_model, cfg, optimizer,
        pose_trainable=False,
        copy_replica=False,
        ar_loss_weights=0.01,
        interlaced=False,
        verbose=0):

    if copy_replica:
        copy_replica_layers(full_model)

    """Split the model into pose estination and action recognition parts."""
    models = split_model(full_model, cfg, interlaced=interlaced,
            model_names=['Pose', 'Action'])

    pose_loss = pose_regression_loss('l1l2bincross', 0.01)
    action_loss = 'categorical_crossentropy'

    set_trainable_layers(full_model, 'action', None, pose_trainable)
    loss_weights_pe = len(models[0].outputs) * [1.0]
    loss_weights_ar = len(models[1].outputs) * [ar_loss_weights]

    models[0].compile(loss=pose_loss, optimizer=optimizer,
            loss_weights=loss_weights_pe)
    models[1].compile(loss=action_loss, optimizer=optimizer,
            loss_weights=loss_weights_ar)

    def print_layer(layer, prefix=''):
        c = FAIL if layer.trainable else OKGREEN
        printc(c, prefix + '%s\t| ' % (layer.name))
        try:
            nparam = np.sum([np.prod(K.int_shape(p))
                for p in layer._trainable_weights])
            printcn(c, prefix + '%s\t| %s\t| %d' % (str(type(layer)),
                str(layer.output_shape), nparam))
        except:
            print('')

    if verbose:
        for i in range(2):
            printcn(HEADER, 'Model %s trainable layers:' % models[i].name)
            for m in models[i].layers:
                print_layer(m)
                if type(m) == TimeDistributed:
                    print_layer(m.layer, prefix='td:\t')
                elif type(m) == Model:
                    for n in m.layers:
                        print_layer(n, prefix='>> \t')

    return models

