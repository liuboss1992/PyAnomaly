'''
this is the trainer of the 'Future Frame Prediction for Anomaly Detection - A New Baseline CVPR2018'
'''
import sys
sys.path.append('../')
import warnings
warnings.filterwarnings('ignore')
import os
import pickle
import math
import torch
import time
import numpy as np
from PIL import Image
import torchvision.transforms.functional as tf
from torch.utils.data import DataLoader
torch.autograd.set_detect_anomaly(True)

from lib.core.utils import AverageMeter, flow_batch_estimate
from lib.datatools.evaluate.utils import psnr_error
from lib.core.engine.default_engine import DefaultTrainer, DefaultInference

class Trainer(DefaultTrainer):
    '''
    The trainer method of the ano pred method
    '''
    def __init__(self, *defaults, **kwargs):
        '''
        Args:
            defaults(tuple): the default will have:
                0->model:{'Generator':net_g, 'Driscriminator':net_d, 'FlowNet':net_flow}
                1->train_dataloader: the dataloader   
                2->val_dataloader: the dataloader     
                3->optimizer:{'optimizer_g':op_g, 'optimizer_d'}
                4->loss_function: {'g_adverserial_loss':.., 'd_adverserial_loss':..., 'gradient_loss':.., 'opticalflow_loss':.., 'intentsity_loss':.. }
                5->logger: the logger of the whole training process
                6->config: the config object of the whole process

            kwargs(dict): the default will have:
                verbose(str):
                parallel(bool): True-> data parallel
                pertrain(bool): True-> use the pretarin model
                extra param:
                    test_dataset_keys: the dataset keys of each video
                    test_dataset_dict: the dataset dict of whole test videos
        '''
        # print('in AnoPredTrainer')
        # logger & config
        self._hooks = []
        self._register_hooks(kwargs['hooks'])
        self.logger = defaults[5]
        self.config = defaults[6]

        model = defaults[0]
        # basic things
        if kwargs['parallel']:
            self.G = self.data_parallel(model['Generator'])
            self.D = self.data_parallel(model['Discriminator'])
            self.F = self.data_parallel(model['FlowNet'])
            # self.G = model['Generator'].to(torch.device('cuda:0'))
            # self.D = model['Discriminator'].to(torch.device('cuda:1'))
            # self.F = model['FlowNet'].cuda()
        else:
            self.G = model['Generator'].cuda()
            self.D = model['Discriminator'].cuda()
            self.F = model['FlowNet'].cuda() # lite flownet
        
        self.F.eval()
        
        if kwargs['pretrain']:
            self.load_pretrain()

        self.train_dataloader = defaults[1]
        self._train_loader_iter = iter(self.train_dataloader)

        self.val_dataloader = defaults[2]
        self._val_loader_iter = iter(self.val_dataloader)

        # get the optimizer
        optimizer = defaults[3]
        self.optim_G = optimizer['optimizer_g']
        self.optim_D = optimizer['optimizer_d']

        # get the loss_fucntion
        loss_function = defaults[4]
        # self.g_adv_loss = loss_function['g_adverserial_loss']
        # self.d_adv_loss = loss_function['d_adverserial_loss']
        self.gan_loss = loss_function['gan_loss']
        self.gd_loss = loss_function['gradient_loss']
        self.int_loss = loss_function['intentsity_loss']
        self.op_loss = loss_function['opticalflow_loss']

        # basic meter
        self.batch_time =  AverageMeter()
        self.data_time = AverageMeter()
        self.loss_meter_G = AverageMeter()
        self.loss_meter_D = AverageMeter()
        # self.psnr = AverageMeter()

        # others
        self.verbose = kwargs['verbose']
        self.accuarcy = 0.0  # to store the accuracy varies from epoch to epoch
        self.config_name = kwargs['config_name']
        self.kwargs = kwargs
        # self.total_steps = len(self.train_dataloader)
        self.result_path = ''
        self.log_step = self.config.TRAIN.log_step # how many the steps, we will show the information
        self.eval_step = self.config.TRAIN.eval_step 
        self.save_step = self.config.TRAIN.save_step # save the model whatever the acc of the model
        self.max_steps = self.config.TRAIN.max_steps
        # self.testing_data_folder = self.config.DATASET.test_path
        self.test_dataset_keys = kwargs['test_dataset_keys']
        self.test_dataset_dict = kwargs['test_dataset_dict']

        self.evaluate_function = kwargs['evaluate_function']
        
        # hypyer-parameters of loss 
        # self.lam_adv = kwargs['lamada_adv']
        # self.lam_int = kwargs['lamada_int']
        # self.lam_op = kwargs['lamada_op']
        # self.lam_gd = kwargs['lamada_gd']
        self.loss_lamada = kwargs['loss_lamada']

        # the lr scheduler
        scheduler_dict = kwargs['lr_scheduler_dict']
        self.lr_g = scheduler_dict['optimizer_g_scheduler']
        self.lr_d = scheduler_dict['optimizer_d_scheduler']

        if self.config.RESUME.flag:
            self.resume()
        
        if self.config.FINETUNE.flag:
            self.fine_tune()
    

    def train(self,current_step):
        start = time.time()
        self.G.train()
        self.D.train()
        writer = self.kwargs['writer_dict']['writer']
        global_steps = self.kwargs['writer_dict']['global_steps_{}'.format(self.kwargs['model_type'])]

        # for step, data in enumerate(self.train_dataloader):
        data = next(self._train_loader_iter)  # the core for dataloader
        self.data_time.update(time.time() - start)
        target = data[:, -1, :, :].cuda() # t+1 frame 
        input = data[:, :-1, ] # 0~t frame
        input_last = input[:,-1,].cuda() # t frame
        input = input.view(input.shape[0], -1, input.shape[-2], input.shape[-1]).cuda()
        # True Process =================Start===================
        #---------update optim_G ---------
        self.set_requires_grad(self.D, False)
        G_output = self.G(input)
        pred_flow_esti_tensor = torch.cat([input_last, G_output],1)
        gt_flow_esti_tensor = torch.cat([input_last, target], 1)
        _, flow_gt = flow_batch_estimate(self.F, gt_flow_esti_tensor)
        _, flow_pred = flow_batch_estimate(self.F, pred_flow_esti_tensor)

        # loss_g_adv = self.g_adv_loss(self.D(G_output))
        loss_g_adv = self.gan_loss(self.D(G_output), True)
        loss_op = self.op_loss(flow_pred, flow_gt)
        loss_int = self.int_loss(G_output, target)
        loss_gd = self.gd_loss(G_output, target)
        loss_g_all = self.loss_lamada['intentsity_loss'] * loss_int + self.loss_lamada['gradient_loss'] * loss_gd + self.loss_lamada['opticalflow_loss'] * loss_op + self.loss_lamada['gan_loss'] * loss_g_adv
        # import ipdb; ipdb.set_trace()
        self.optim_G.zero_grad()
        # loss_g_all.sum().backward()
        # loss_g_all.backward(retain_graph=True)
        loss_g_all.backward()
        self.optim_G.step()
        # record
        self.loss_meter_G.update(loss_g_all.detach())
        # train_psnr = psnr_error(G_output, target)
        # self.psnr.update(train_psnr.detach())
        if self.config.TRAIN.adversarial.scheduler.use:
            self.lr_g.step()
        #---------update optim_D ---------------
        self.set_requires_grad(self.D, True)
        self.optim_D.zero_grad()
        # G_output = self.G(input)
        temp_t = self.D(target)
        temp_g = self.D(G_output.detach())
        loss_d_1 = self.gan_loss(temp_t, True)
        loss_d_2 = self.gan_loss(temp_g, False)
        loss_d = (loss_d_1 + loss_d_2) * 0.5
        # loss_d.sum().backward()
        loss_d.backward()

        self.optim_D.step()
        if self.config.TRAIN.adversarial.scheduler.use:
            self.lr_d.step()
        self.loss_meter_D.update(loss_d.detach())
        # ======================End==================

        self.batch_time.update(time.time() - start)

        if (current_step % self.log_step == 0):
            msg = 'Step: [{0}/{1}]\t' \
                'Type: {cae_type}\t' \
                'Time: {batch_time.val:.3f}s ({batch_time.avg:.3f}s)\t' \
                'Speed: {speed:.1f} samples/s\t' \
                'Data: {data_time.val:.3f}s ({data_time.avg:.3f}s)\t' \
                'Loss_G: {losses_G.val:.5f} ({losses_G.avg:.5f})\t'   \
                'Loss_D:{losses_D.val:.5f}({losses_D.avg:.5f})'.format(current_step, self.max_steps, cae_type=self.kwargs['model_type'], batch_time=self.batch_time, speed=self.config.TRAIN.batch_size/self.batch_time.val, data_time=self.data_time,losses_G=self.loss_meter_G, losses_D=self.loss_meter_D)
            self.logger.info(msg)
        writer.add_scalar('Train_loss_G', self.loss_meter_G.val, global_steps)
        writer.add_scalar('Train_loss_D', self.loss_meter_D.val, global_steps)
        global_steps += 1 
        # reset start
        start = time.time()
        # del data, input, input_last, loss_d, loss_g_all, target
        # torch.cuda.empty_cache()
        
        self.saved_model = {'G':self.G, 'D':self.D}
        self.saved_optimizer = {'optim_G': self.optim_G, 'optim_D': self.optim_D}
        self.saved_loss = {'loss_G':self.loss_meter_G.val, 'loss_D':self.loss_meter_D.val}
        self.kwargs['writer_dict']['global_steps_{}'.format(self.kwargs['model_type'])] = global_steps
    
    def mini_eval(self, current_step):
        if current_step % 10 != 0 or current_step == 0:
            return
        temp_meter_frame = AverageMeter()
        temp_meter_flow = AverageMeter()
        self.G.eval()
        self.D.eval()
        for data in self.val_dataloader:
            vaild_target = data[:,-1,].cuda()
            vaild_input = data[:,:-1]
            vaild_input_last = vaild_input[:,-1].cuda()
            vaild_input = vaild_input.view(data.shape[0],-1,data.shape[-2], data.shape[-1]).cuda()
            vaild_output = self.G(vaild_input)
            flow_gt, _ = flow_batch_estimate(self.F, torch.cat([vaild_input_last, vaild_target], 1))
            flow_pred, _ = flow_batch_estimate(self.F, torch.cat([vaild_input_last, vaild_output], 1))
            vaild_frame_psnr = psnr_error(vaild_output.detach(), vaild_target)
            vaild_flow_psnr = psnr_error(flow_pred, flow_gt)
            temp_meter_frame.update(vaild_frame_psnr.detach())
            temp_meter_flow.update(vaild_flow_psnr.detach())
        self.logger.info(f'&^*_*^& ==> Step:{current_step}/{self.max_steps} the PSNR is {temp_meter_frame.avg:.3f}, the flow PSNR is {temp_meter_flow.avg:.3f}')

    

class Inference(DefaultInference):
    '''
    The inference of the ano pred
    '''
    def __init__(self, *defaults,**kwargs):
        '''
         Args:
            defaults(tuple): the default will have:
                0->model: the model of the experiment
                1->model_path: the path of the model path
                2->val_dataloader: the dataloader to inference
                3->logger: the logger of the whole process
                4->config: the config object of the whole process
            kwargs(dict): the default will have:
                verbose(str):
                parallel(bool): True-> data parallel
                pertrain(bool): True-> use the pretarin model
                mode(str): 'dataset' -> the data will use the dataloder to pass in(dicard, becasue we will use the dataset to get all I need)
        '''
        self._hooks = []
        self._register_hooks(kwargs['hooks'])
        self.logger = defaults[3]
        self.config = defaults[4]
        self.model_path = defaults[1]

        save_model = torch.load(self.model_path)
        
        model = defaults[0]
        if kwargs['parallel']:
            self.G = self.data_parallel(model['Generator']).load_state_dict(save_model['G'])
            self.D = self.data_parallel(model['Discriminator']).load_state_dict(save_model['D'])
            # self.G = model['Generator'].to(torch.device('cuda:0'))
            # self.D = model['Discriminator'].to(torch.device('cuda:1'))
            self.F = self.data_parallel(model['FlowNet'])
        else:
            # import ipdb; ipdb.set_trace()
            self.G = model['Generator'].cuda()
            self.G.load_state_dict(save_model['G'])
            self.D = model['Discriminator'].cuda()
            self.D.load_state_dict(save_model['D'])
            self.F = model['FlowNet'].cuda()
        
        # self.load()

        self.verbose = kwargs['verbose']
        self.kwargs = kwargs
        self.config_name = kwargs['config_name']
        # self.mode = kwargs['mode']

        self.test_dataset_keys = kwargs['test_dataset_keys']
        self.test_dataset_dict = kwargs['test_dataset_dict']

        self.metric = 0.0
    
    def inference(self):
        for h in self._hooks:
            h.inference()