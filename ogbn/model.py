import math
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F


class Transformer(nn.Module):
    def __init__(self, n_channels, att_drop=0., act='none', num_heads=1):
        super(Transformer, self).__init__()
        self.n_channels = n_channels
        self.num_heads = num_heads
        assert self.n_channels % (self.num_heads * 4) == 0

        self.query = nn.Linear(self.n_channels, self.n_channels//4)
        self.key   = nn.Linear(self.n_channels, self.n_channels//4)
        self.value = nn.Linear(self.n_channels, self.n_channels)

        self.gamma = nn.Parameter(torch.tensor([0.]))
        self.att_drop = nn.Dropout(att_drop)
        if act == 'sigmoid':
            self.act = torch.nn.Sigmoid()
        elif act == 'relu':
            self.act = torch.nn.ReLU()
        elif act == 'leaky_relu':
            self.act = torch.nn.LeakyReLU(0.2)
        elif act == 'none':
            self.act = lambda x: x
        else:
            assert 0, f'Unrecognized activation function {act} for class Transformer'

    def reset_parameters(self):

        def xavier_uniform_(tensor, gain=1.):
            fan_in, fan_out = tensor.size()[-2:]
            std = gain * math.sqrt(2.0 / float(fan_in + fan_out))
            a = math.sqrt(3.0) * std  # Calculate uniform bounds from standard deviation
            return torch.nn.init._no_grad_uniform_(tensor, -a, a)

        gain = nn.init.calculate_gain("relu")
        xavier_uniform_(self.query.weight, gain=gain)
        xavier_uniform_(self.key.weight, gain=gain)
        xavier_uniform_(self.value.weight, gain=gain)
        nn.init.zeros_(self.query.bias)
        nn.init.zeros_(self.key.bias)
        nn.init.zeros_(self.value.bias)

    def forward(self, x, mask=None):
        B, M, C = x.size() # batchsize, num_metapaths, channels
        H = self.num_heads
        if mask is not None:
            assert mask.size() == torch.Size((B, M))

        f = self.query(x).view(B, M, H, -1).permute(0,2,1,3) # [B, H, M, -1]
        g = self.key(x).view(B, M, H, -1).permute(0,2,3,1)   # [B, H, -1, M]
        h = self.value(x).view(B, M, H, -1).permute(0,2,1,3) # [B, H, M, -1]

        beta = F.softmax(self.act(f @ g / math.sqrt(f.size(-1))), dim=-1) # [B, H, M, M(normalized)]
        beta = self.att_drop(beta)
        if mask is not None:
            beta = beta * mask.view(B, 1, 1, M)
            beta = beta / (beta.sum(-1, keepdim=True) + 1e-12)

        o = self.gamma * (beta @ h) # [B, H, M, -1]
        return o.permute(0,2,1,3).reshape((B, M, C)) + x


class Conv1d1x1(nn.Module):
    def __init__(self, cin, cout, groups, bias=True, cformat='channel-first'):
        super(Conv1d1x1, self).__init__()
        self.cin = cin
        self.cout = cout
        self.groups = groups
        self.cformat = cformat
        if not bias:
            self.bias = None
        if self.groups == 1: # different keypoints share same kernel
            self.W = nn.Parameter(torch.randn(self.cin, self.cout))
            if bias:
                self.bias = nn.Parameter(torch.zeros(1, self.cout))
        else:
            self.W = nn.Parameter(torch.randn(self.groups, self.cin, self.cout))
            if bias:
                self.bias = nn.Parameter(torch.zeros(self.groups, self.cout))

    def reset_parameters(self):

        def xavier_uniform_(tensor, gain=1.):
            fan_in, fan_out = tensor.size()[-2:]
            std = gain * math.sqrt(2.0 / float(fan_in + fan_out))
            a = math.sqrt(3.0) * std  # Calculate uniform bounds from standard deviation
            return torch.nn.init._no_grad_uniform_(tensor, -a, a)

        gain = nn.init.calculate_gain("relu")
        xavier_uniform_(self.W, gain=gain)
        if self.bias is not None:
            nn.init.zeros_(self.bias)

    def forward(self, x):
        if self.groups == 1:
            if self.cformat == 'channel-first':
                return torch.einsum('bcm,mn->bcn', x, self.W) + self.bias
            elif self.cformat == 'channel-last':
                return torch.einsum('bmc,mn->bnc', x, self.W) + self.bias.T
            else:
                assert False
        else:
            if self.cformat == 'channel-first':
                return torch.einsum('bcm,cmn->bcn', x, self.W) + self.bias
            elif self.cformat == 'channel-last':
                return torch.einsum('bmc,cmn->bnc', x, self.W) + self.bias.T
            else:
                assert False


class L2Norm(nn.Module):

    def __init__(self, dim):
        super(L2Norm, self).__init__()
        self.dim = dim

    def forward(self, x):
        return F.normalize(x, p=2, dim=self.dim)


class SeHGNN_mag(nn.Module):
    def __init__(self, dataset, data_size, nfeat, hidden, nclass,
                 num_feats, num_label_feats, tgt_key,
                 dropout, input_drop, att_drop, label_drop,
                 n_layers_1, n_layers_2, n_layers_3,
                 act, residual=False, bns=False, label_bns=False,
                 label_residual=True):
        super(SeHGNN_mag, self).__init__()
        self.dataset = dataset
        self.residual = residual
        self.tgt_key = tgt_key
        self.label_residual = label_residual

        if any([v != nfeat for k, v in data_size.items()]):
            self.embedings = nn.ParameterDict({})
            for k, v in data_size.items():
                if v != nfeat:
                    self.embedings[k] = nn.Parameter(
                        torch.Tensor(v, nfeat).uniform_(-0.5, 0.5))
        else:
            self.embedings = None

        self.feat_project_layers = nn.Sequential(
            Conv1d1x1(nfeat, hidden, num_feats, bias=True, cformat='channel-first'),
            nn.LayerNorm([num_feats, hidden]),
            nn.PReLU(),
            nn.Dropout(dropout),
            Conv1d1x1(hidden, hidden, num_feats, bias=True, cformat='channel-first'),
            nn.LayerNorm([num_feats, hidden]),
            nn.PReLU(),
            nn.Dropout(dropout),
        )
        if num_label_feats > 0:
            self.label_feat_project_layers = nn.Sequential(
                Conv1d1x1(nclass, hidden, num_label_feats, bias=True, cformat='channel-first'),
                nn.LayerNorm([num_label_feats, hidden]),
                nn.PReLU(),
                nn.Dropout(dropout),
                Conv1d1x1(hidden, hidden, num_label_feats, bias=True, cformat='channel-first'),
                nn.LayerNorm([num_label_feats, hidden]),
                nn.PReLU(),
                nn.Dropout(dropout),
            )
        else:
            self.label_feat_project_layers = None

        self.semantic_aggr_layers = Transformer(hidden, att_drop, act)
        if self.dataset != 'products':
            self.concat_project_layer = nn.Linear((num_feats + num_label_feats) * hidden, hidden)

        if self.residual:
            self.res_fc = nn.Linear(nfeat, hidden, bias=False)

        def add_nonlinear_layers(nfeats, dropout, bns=False):
            if bns:
                return [
                    nn.BatchNorm1d(hidden),
                    nn.PReLU(),
                    nn.Dropout(dropout)
                ]
            else:
                return [
                    nn.PReLU(),
                    nn.Dropout(dropout)
                ]

        lr_output_layers = [
            [nn.Linear(hidden, hidden, bias=not bns)] + add_nonlinear_layers(hidden, dropout, bns)
            for _ in range(n_layers_2-1)]
        self.lr_output = nn.Sequential(*(
            [ele for li in lr_output_layers for ele in li] + [
            nn.Linear(hidden, nclass, bias=False),
            nn.BatchNorm1d(nclass)]))

        if self.label_residual:
            label_fc_layers = [
                [nn.Linear(hidden, hidden, bias=not bns)] + add_nonlinear_layers(hidden, dropout, bns)
                for _ in range(n_layers_3-2)]
            self.label_fc = nn.Sequential(*(
                [nn.Linear(nclass, hidden, bias=not bns)] + add_nonlinear_layers(hidden, dropout, bns) \
                + [ele for li in label_fc_layers for ele in li] + [nn.Linear(hidden, nclass, bias=True)]))
            self.label_drop = nn.Dropout(label_drop)

        self.prelu = nn.PReLU()
        self.dropout = nn.Dropout(dropout)
        self.input_drop = nn.Dropout(input_drop)

        self.reset_parameters()

    def reset_parameters(self):
        gain = nn.init.calculate_gain("relu")

        for layer in self.feat_project_layers:
            if isinstance(layer, Conv1d1x1):
                layer.reset_parameters()
        if self.label_feat_project_layers is not None:
            for layer in self.label_feat_project_layers:
                if isinstance(layer, Conv1d1x1):
                    layer.reset_parameters()

        if self.dataset != 'products':
            nn.init.xavier_uniform_(self.concat_project_layer.weight, gain=gain)
            nn.init.zeros_(self.concat_project_layer.bias)

        if self.residual:
            nn.init.xavier_uniform_(self.res_fc.weight, gain=gain)

        for layer in self.lr_output:
            if isinstance(layer, nn.Linear):
                nn.init.xavier_uniform_(layer.weight, gain=gain)
                if layer.bias is not None:
                    nn.init.zeros_(layer.bias)

        if self.label_residual:
            for layer in self.label_fc:
                if isinstance(layer, nn.Linear):
                    nn.init.xavier_uniform_(layer.weight, gain=gain)
                    if layer.bias is not None:
                        nn.init.zeros_(layer.bias)

    def forward(self, feats_dict, layer_feats_dict, label_emb):
        if self.embedings is not None:
            for k, v in feats_dict.items():
                if k in self.embedings:
                    feats_dict[k] = v @ self.embedings[k]

        tgt_feat = self.input_drop(feats_dict[self.tgt_key])
        B = num_node = tgt_feat.size(0)
        x = self.input_drop(torch.stack(list(feats_dict.values()), dim=1))
        x = self.feat_project_layers(x)

        if self.label_feat_project_layers is not None:
            label_feats = self.input_drop(torch.stack(list(layer_feats_dict.values()), dim=1))
            label_feats = self.label_feat_project_layers(label_feats)
            x = torch.cat((x, label_feats), dim=1)

        x = self.semantic_aggr_layers(x)
        if self.dataset == 'products':
            x = x[:,:,0].contiguous()
        else:
            x = self.concat_project_layer(x.reshape(B, -1))

        if self.residual:
            x = x + self.res_fc(tgt_feat)
        x = self.dropout(self.prelu(x))
        x = self.lr_output(x)
        if self.label_residual:
            x = x + self.label_fc(self.label_drop(label_emb))
        return x