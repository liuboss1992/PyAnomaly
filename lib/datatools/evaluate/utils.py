import numpy as np
import pickle
import torch
from .gtloader import GroundTruthLoader
from scipy.ndimage import gaussian_filter1d

class RecordResult(object):
    def __init__(self, fpr=None, tpr=None, thresholds=None, auc=-np.inf, dataset=None, loss_file=None):
        self.fpr = fpr
        self.tpr = tpr
        self.thresholds = thresholds
        self.auc = auc
        self.dataset = dataset
        self.loss_file = loss_file

    def __lt__(self, other):
        return self.auc < other.auc

    def __gt__(self, other):
        return self.auc > other.auc

    def __str__(self):
        return 'dataset = {}, loss file = {}, auc = {}'.format(self.dataset, self.loss_file, self.auc)
    
    def get_threshold(self):
        diff_list = list()
        num_points = len(self.fpr)
        for i in range(num_points):
            temp = self.tpr[i] - self.fpr[i]
            diff_list.append(temp)
        index = diff_list.index(max(diff_list))

        return self.thresholds[index]


def log10(t):
    """
    Calculates the base-10 log of each element in t.
    @param t: The tensor from which to calculate the base-10 log.
    @return: A tensor with the base-10 log of each element in t.
    """

    numerator = torch.log(t)
    denominator = torch.log(torch.FloatTensor([10.])).cuda()
    return numerator / denominator



def psnr_error(gen_frames, gt_frames, max_val_hat=1.0):
    """
    Computes the Peak Signal to Noise Ratio error between the generated images and the ground
    truth images.
    @param gen_frames: A tensor of shape [batch_size, height, width, 3]. The frames generated by the
                       generator model.
    @param gt_frames: A tensor of shape [batch_size, height, width, 3]. The ground-truth frames for
                      each frame in gen_frames.
    @return: A scalar tensor. The mean Peak Signal to Noise Ratio error over each frame in the
             batch.
    """
    shape = list(gen_frames.shape)
    num_pixels = (shape[1] * shape[2] * shape[3])
    gt_frames = (gt_frames + 1.0) / 2.0
    gen_frames = (gen_frames + 1.0) / 2.0
    square_diff = (gt_frames - gen_frames)**2

    batch_errors = 10 * log10(max_val_hat ** 2 / ((1. / num_pixels) * torch.sum(square_diff, [1, 2, 3])))
    return torch.mean(batch_errors)

def load_pickle_results(loss_file, cfg):
    with open(loss_file, 'rb') as reader:
        # results {
        #   'dataset': the name of dataset
        #   'psnr': the psnr of each testing videos,
        #   'flow': [], 
        #   'names': [], 
        #   'diff_mask': [], 
        #   'score': the score of each testing videos
        #   'num_videos': the number of the videos
        # }

        # psnr_records['psnr'] is np.array, shape(#videos)
        # psnr_records[0] is np.array   ------>     01.avi
        # psnr_records[1] is np.array   ------>     02.avi
        #               ......
        # psnr_records[n] is np.array   ------>     xx.avi

        results = pickle.load(reader)

    dataset = results['dataset']
    psnr_records = results['psnr']
    score = results['score']
    num_videos = results['num_videos']
    # import ipdb; ipdb.set_trace()
    if cfg.DATASET.smooth.guassian:
        new_score = []
        for index, item in enumerate(score):
            temp = gaussian_filter1d(score[index], cfg.DATASET.smooth.guassian_sigma)
            new_score.append(temp)
        print('Smooth the score')
    # score = np.array(new_score)
    assert dataset == cfg.DATASET.name, f'The dataset are not match, Result:{dataset}, cfg:{cfg.DATASET.name}'

    # load ground truth
    gt_loader = GroundTruthLoader(cfg)
    # gt = gt_loader(dataset=dataset)
    gt = gt_loader()

    assert num_videos == len(gt), f'the number of saved videos does not match the ground truth, {num_videos} != {len(gt)}' 

    return dataset, psnr_records, score, gt, num_videos


def simple_diff(frame_true, frame_hat, flow_true, flow_hat, aggregation=False):
    """
    """
    assert frame_true.shape == frame_hat.shape
    assert flow_true.shape == flow_hat.shape

    frame_true = frame_true.squeeze(0).detach()
    frame_hat = frame_hat.squeeze(0).detach()
    flow_true = flow_true.squeeze(0).detach()
    flow_hat = flow_hat.squeeze(0).detach()

    loss_appe = (frame_true-frame_hat)**2
    loss_flow = (flow_true-flow_hat)**2

    if aggregation:
        loss_appe = torch.mean(loss_appe)
        loss_flow = torch.mean(loss_flow)

    return loss_appe, loss_flow

def find_max_patch(diff_map_appe, diff_map_flow, kernel_size=16, stride=1, aggregation=True):
    max_pool = torch.nn.MaxPool2d(kernel_size=kernel_size, stride=stride)
    max_patch_appe = max_pool(diff_map_appe)
    max_patch_flow = max_pool(diff_map_flow)
    # import ipdb; ipdb.set_trace()
    if aggregation:
        max_patch_appe = torch.mean(max_patch_appe) # torch.mean(max_patch_appe, [1,2,3]) will calc the mean based on the N, 
        max_patch_flow = torch.mean(max_patch_flow)

    return max_patch_appe, max_patch_flow

def calc_w(w_dict):
    wf = 0.0
    wi = 0.0
    n = 0
    for key in w_dict.keys():
        n += w_dict[key][0]
        wf += w_dict[key][1]
        wi += w_dict[key][2]
    # import ipdb; ipdb.set_trace()
    wf = torch.div(1.0, torch.div(wf, n))
    wi = torch.div(1.0, torch.div(wi, n))

    return wf, wi

def amc_normal_score(wf, sf, wi, si, lambada_s=0.2):
    final_score = torch.log(wf * sf) + lambada_s * torch.log(wi*si)

    return final_score

def amc_score(frame, frame_hat, flow, flow_hat, wf, wi, kernel_size=16, stride=1, lambada_s=0.2):
    '''
    wf, wi is different from videos
    '''
    loss_appe, loss_flow = simple_diff(frame, frame_hat, flow, flow_hat)
    max_patch_appe, max_patch_flow = find_max_patch(loss_appe, loss_flow, kernel_size=kernel_size, stride=stride)
    final_score = amc_normal_score(wf, max_patch_appe, wi, max_patch_flow, lambada_s=lambada_s)

    return final_score

def oc_score(raw_data):
    object_score = np.empty(shape=(raw_data.shape[0],),dtype=np.float32)
    for index, dummy_objects in enumerate(raw_data):
        # temp = np.max(-dummy_objects)
        temp = np.max(dummy_objects)
        object_score[index] = temp
    
    frame_score = np.max(object_score)

    return frame_score


def reconstruction_loss(x_hat, x):
    '''
    The input is the video clip, and we use the RL as the score.
    RL := Reconstruction Loss
    '''
    x_hat = x_hat.squeeze(0).detach()
    x = x.squeeze(0).detach()
    rl = torch.sqrt(torch.pow((x_hat - x), 2))
    h_dim = len(rl.shape) - 2
    w_dim = len(rl.shape) - 1
    rl = torch.mean(rl, (h_dim, w_dim))
    return rl