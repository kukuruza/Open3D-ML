import numpy as np
import torch
import torch.nn as nn
import torch.utils.dlpack
import open3d.core as o3c

from sklearn.neighbors import KDTree

from .base_model import BaseModel
from ...utils import MODEL
from ..modules.losses import filter_valid_label
from ...datasets.augment import SemsegAugmentation
from ...datasets.utils import DataProcessing
from ..utils.pointnet.pointnet2_utils import furthest_point_sample_v2


def model_fn_decorator(criterion):
    from collections import namedtuple
    ModelReturn = namedtuple("ModelReturn", ['pred', 'loss', 'acc'])

    def model_fn(model, data, epoch=0, eval=False):
        with torch.set_grad_enabled(not eval):
            coord, feat, label, offset = data
            pred = model([coord, feat, offset])
            print("pred = ", pred)
            loss = criterion(pred, label)
            _, classes = torch.max(pred, 1)
            acc = (classes == label).float().sum() / label.numel()
            return ModelReturn(pred, loss, {
                "acc": acc.item(),
                'loss': loss.item()
            })

    return model_fn


class PointTransformer(BaseModel):

    def __init__(self,
                 name="PointTransformer",
                 blocks=[2, 2, 2, 2, 2],
                 c=6,
                 num_classes=13,
                 voxel_size=0.04,
                 max_voxels=80000,
                 batcher='ConcatBatcher',
                 **kwargs):
        super(PointTransformer, self).__init__(name=name,
                                               blocks=blocks,
                                               c=c,
                                               num_classes=num_classes,
                                               voxel_size=voxel_size,
                                               max_voxels=max_voxels,
                                               batcher=batcher,
                                               **kwargs)
        self.c = c
        self.in_planes, planes = c, [32, 64, 128, 256, 512]
        fpn_planes, fpnhead_planes, share_planes = 128, 64, 8
        stride, nsample = [1, 4, 4, 4, 4], [8, 16, 16, 16, 16]
        block = Bottleneck
        self.enc1 = self._make_enc(block,
                                   planes[0],
                                   blocks[0],
                                   share_planes,
                                   stride=stride[0],
                                   nsample=nsample[0])  # N/1
        self.enc2 = self._make_enc(block,
                                   planes[1],
                                   blocks[1],
                                   share_planes,
                                   stride=stride[1],
                                   nsample=nsample[1])  # N/4
        self.enc3 = self._make_enc(block,
                                   planes[2],
                                   blocks[2],
                                   share_planes,
                                   stride=stride[2],
                                   nsample=nsample[2])  # N/16
        self.enc4 = self._make_enc(block,
                                   planes[3],
                                   blocks[3],
                                   share_planes,
                                   stride=stride[3],
                                   nsample=nsample[3])  # N/64
        self.enc5 = self._make_enc(block,
                                   planes[4],
                                   blocks[4],
                                   share_planes,
                                   stride=stride[4],
                                   nsample=nsample[4])  # N/256
        self.dec5 = self._make_dec(block,
                                   planes[4],
                                   2,
                                   share_planes,
                                   nsample=nsample[4],
                                   is_head=True)  # transform p5
        self.dec4 = self._make_dec(block,
                                   planes[3],
                                   2,
                                   share_planes,
                                   nsample=nsample[3])  # fusion p5 and p4
        self.dec3 = self._make_dec(block,
                                   planes[2],
                                   2,
                                   share_planes,
                                   nsample=nsample[2])  # fusion p4 and p3
        self.dec2 = self._make_dec(block,
                                   planes[1],
                                   2,
                                   share_planes,
                                   nsample=nsample[1])  # fusion p3 and p2
        self.dec1 = self._make_dec(block,
                                   planes[0],
                                   2,
                                   share_planes,
                                   nsample=nsample[0])  # fusion p2 and p1
        self.cls = nn.Sequential(nn.Linear(planes[0], planes[0]),
                                 nn.BatchNorm1d(planes[0]),
                                 nn.ReLU(inplace=True),
                                 nn.Linear(planes[0], num_classes))

    def _make_enc(self,
                  block,
                  planes,
                  blocks,
                  share_planes=8,
                  stride=1,
                  nsample=16):
        layers = []
        layers.append(
            TransitionDown(self.in_planes, planes * block.expansion, stride,
                           nsample))
        self.in_planes = planes * block.expansion
        for _ in range(1, blocks):
            layers.append(
                block(self.in_planes,
                      self.in_planes,
                      share_planes,
                      nsample=nsample))
        return nn.Sequential(*layers)

    def _make_dec(self,
                  block,
                  planes,
                  blocks,
                  share_planes=8,
                  nsample=16,
                  is_head=False):
        layers = []
        layers.append(
            TransitionUp(self.in_planes,
                         None if is_head else planes * block.expansion))
        self.in_planes = planes * block.expansion
        for _ in range(1, blocks):
            layers.append(
                block(self.in_planes,
                      self.in_planes,
                      share_planes,
                      nsample=nsample))
        return nn.Sequential(*layers)

    def forward(self, batch):
        point_0, feat_0, row_splits_0 = batch.point, batch.feat, batch.row_splits  # (n, 3), (n, c), (b)

        feat_0 = point_0 if self.c == 3 else torch.cat(
            (point_0, feat_0), 1)  # maybe use feat for c == 3
        point_1, feat_1, row_splits_1 = self.enc1(
            [point_0, feat_0, row_splits_0])
        point_2, feat_2, row_splits_2 = self.enc2(
            [point_1, feat_1, row_splits_1])
        point_3, feat_3, row_splits_3 = self.enc3(
            [point_2, feat_2, row_splits_2])
        point_4, feat_4, row_splits_4 = self.enc4(
            [point_3, feat_3, row_splits_3])
        point_5, feat_5, row_splits_5 = self.enc5(
            [point_4, feat_4, row_splits_4])

        feat_5 = self.dec5[1:]([
            point_5, self.dec5[0]([point_5, feat_5, row_splits_5]), row_splits_5
        ])[1]
        feat_4 = self.dec4[1:]([
            point_4, self.dec4[0]([point_4, feat_4, row_splits_4],
                                  [point_5, feat_5, row_splits_5]), row_splits_4
        ])[1]
        feat_3 = self.dec3[1:]([
            point_3, self.dec3[0]([point_3, feat_3, row_splits_3],
                                  [point_4, feat_4, row_splits_4]), row_splits_3
        ])[1]
        feat_2 = self.dec2[1:]([
            point_2, self.dec2[0]([point_2, feat_2, row_splits_2],
                                  [point_3, feat_3, row_splits_3]), row_splits_2
        ])[1]
        feat_1 = self.dec1[1:]([
            point_1, self.dec1[0]([point_1, feat_1, row_splits_1],
                                  [point_2, feat_2, row_splits_2]), row_splits_1
        ])[1]
        feat = self.cls(feat_1)
        return feat

    def preprocess(self, data, attr):
        cfg = self.cfg
        points = np.array(data['point'], dtype=np.float32)

        if 'label' not in data or data['label'] is None:
            labels = np.zeros((points.shape[0],), dtype=np.int32)
        else:
            labels = np.array(data['label'], dtype=np.int32).reshape((-1,))

        if 'feat' not in data or data['feat'] is None:
            feat = None
        else:
            feat = np.array(data['feat'], dtype=np.float32)

        data = dict()

        if (cfg.voxel_size):
            points_min = np.min(points, 0)
            points -= points_min

            if (feat is None):
                sub_points, sub_labels = DataProcessing.grid_subsampling(
                    points, labels=labels, grid_size=cfg.voxel_size)
                sub_feat = None
            else:
                sub_points, sub_feat, sub_labels = DataProcessing.grid_subsampling(
                    points,
                    features=feat,
                    labels=labels,
                    grid_size=cfg.voxel_size)
        else:
            sub_points, sub_feat, sub_labels = points, feat, labels

        search_tree = KDTree(sub_points)

        data['point'] = sub_points
        data['feat'] = sub_feat
        data['label'] = sub_labels
        data['search_tree'] = search_tree

        if attr['split'] in ["test", "testing"]:
            proj_inds = np.squeeze(
                search_tree.query(points, return_distance=False))
            proj_inds = proj_inds.astype(np.int32)
            data['proj_inds'] = proj_inds

        return data

    def transform(self, data, attr):
        cfg = self.cfg
        points = data['point']
        feat = data['feat']
        labels = data['label']

        # if attr['split'] in ['training', 'train']:
        #     points, feat, labels = self.augmenter.augment(
        #         points, feat, labels, self.cfg.get('augment', None))

        if cfg.max_voxels and data['label'].shape[0] > cfg.max_voxels:
            init_idx = np.random.randint(
                labels.shape[0]
            ) if 'train' in attr['split'] else labels.shape[0] // 2
            crop_idx = np.argsort(
                np.sum(np.square(points - points[init_idx]),
                       1))[:cfg.max_voxels]
            if feat is not None:
                points, feat, labels = points[crop_idx], feat[crop_idx], labels[
                    crop_idx]
            else:
                points, labels = points[crop_idx], labels[crop_idx]

        points_min, points_max = np.min(points, 0), np.max(points, 0)
        points -= (points_min + points_max) / 2.0

        data['point'] = torch.from_numpy(points).to(torch.float32)
        if feat is not None:
            data['feat'] = torch.from_numpy(feat).to(torch.float32) / 255.0
        data['label'] = torch.from_numpy(labels).to(torch.int64)

        return data

    def inference_begin(self):
        pass

    def inference_preprocess(self):
        pass

    def inference_end(self):
        pass

    def get_loss(self, Loss, results, inputs, device):
        cfg = self.cfg
        labels = inputs['data'].label

        scores, labels = filter_valid_label(results, labels, cfg.num_classes,
                                            cfg.ignored_label_inds, device)

        loss = Loss.weighted_CrossEntropyLoss(scores, labels)

        return loss, labels, scores

    def get_optimizer(self, cfg_pipeline):
        optimizer = torch.optim.SGD(self.parameters(),
                                    lr=cfg_pipeline.adam_lr,
                                    momentum=cfg_pipeline.momentum,
                                    weight_decay=cfg_pipeline.weight_decay)
        # optimizer = torch.optim.Adam(model.parameters(), lr=args.base_lr, weight_decay=args.weight_decay)
        # optimizer = torch.optim.AdamW(model.parameters(), lr=args.base_lr, weight_decay=args.weight_decay)
        # scheduler = lr_scheduler.StepLR(optimizer, step_size=args.step_epoch, gamma=args.multiplier)
        # scheduler = lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs)
        # scheduler = lr_scheduler.ExponentialLR(optimizer, gamma=0.95)
        scheduler = torch.optim.lr_scheduler.MultiStepLR(
            optimizer,
            milestones=[
                int(cfg_pipeline.max_epoch * 0.6),
                int(cfg_pipeline.max_epoch * 0.8)
            ],
            gamma=0.1)

        return optimizer, scheduler


MODEL._register_module(PointTransformer, 'torch')


class Transformer(nn.Module):

    def __init__(self, in_planes, out_planes, share_planes=8, nsample=16):
        super().__init__()
        self.mid_planes = mid_planes = out_planes // 1
        self.out_planes = out_planes
        self.share_planes = share_planes
        self.nsample = nsample

        self.linear_q = nn.Linear(in_planes, mid_planes)
        self.linear_k = nn.Linear(in_planes, mid_planes)
        self.linear_v = nn.Linear(in_planes, out_planes)
        self.linear_p = nn.Sequential(
            nn.Linear(3, 3),
            nn.BatchNorm1d(3),
            nn.ReLU(inplace=True),
            nn.Linear(3, out_planes),
        )
        self.linear_w = nn.Sequential(
            nn.BatchNorm1d(mid_planes),
            nn.ReLU(inplace=True),
            nn.Linear(mid_planes, mid_planes // share_planes),
            nn.BatchNorm1d(mid_planes // share_planes),
            nn.ReLU(inplace=True),
            nn.Linear(out_planes // share_planes,
                      out_planes // share_planes),  # Verify
        )
        self.softmax = nn.Softmax(dim=1)

    def forward(self, pxo) -> torch.Tensor:
        point, feat, row_splits = pxo  # (n, 3), (n, c), (b)
        feat_q, feat_k, feat_v = self.linear_q(feat), self.linear_k(
            feat), self.linear_v(feat)  # (n, c)
        feat_k = queryandgroup(self.nsample,
                               point,
                               point,
                               feat_k,
                               None,
                               row_splits,
                               row_splits,
                               use_xyz=True)  # (n, nsample, 3+c)
        feat_v = queryandgroup(self.nsample,
                               point,
                               point,
                               feat_v,
                               None,
                               row_splits,
                               row_splits,
                               use_xyz=False)  # (n, nsample, c)
        point_r, feat_k = feat_k[:, :, 0:3], feat_k[:, :, 3:]

        for i, layer in enumerate(self.linear_p):
            point_r = layer(point_r.transpose(1, 2).contiguous()).transpose(
                1, 2).contiguous() if i == 1 else layer(
                    point_r)  # (n, nsample, c)

        w = feat_k - feat_q.unsqueeze(1) + point_r.view(
            point_r.shape[0], point_r.shape[1], self.out_planes //
            self.mid_planes, self.mid_planes).sum(2)  # (n, nsample, c)

        for i, layer in enumerate(self.linear_w):
            w = layer(w.transpose(1, 2).contiguous()).transpose(
                1, 2).contiguous() if i % 3 == 0 else layer(w)

        w = self.softmax(w)  # (n, nsample, c)
        n, nsample, c = feat_v.shape
        s = self.share_planes
        feat = ((feat_v + point_r).view(n, nsample, s, c // s) *
                w.unsqueeze(2)).sum(1).view(n, c)
        #x = pointops.aggregation(x_v, w)
        return feat


class TransitionDown(nn.Module):

    def __init__(self, in_planes, out_planes, stride=1, nsample=16):
        super().__init__()
        self.stride, self.nsample = stride, nsample
        if stride != 1:
            self.linear = nn.Linear(3 + in_planes, out_planes, bias=False)
            self.pool = nn.MaxPool1d(nsample)
        else:
            self.linear = nn.Linear(in_planes, out_planes, bias=False)
        self.bn = nn.BatchNorm1d(out_planes)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, pxo):
        point, feat, row_splits = pxo  # (n, 3), (n, c), (b)
        p, x, o = pxo  # (n, 3), (n, c), (b)
        if self.stride != 1:
            new_row_splits = [0]
            count = 0
            for i in range(1, row_splits.shape[0]):
                count += (row_splits[i].item() -
                          row_splits[i - 1].item()) // self.stride
                new_row_splits.append(count)

            new_row_splits = torch.LongTensor(new_row_splits).to(
                row_splits.device)
            idx = furthest_point_sample_v2(point, row_splits,
                                           new_row_splits)  # (m)
            new_point = point[idx.long(), :]  # (m, 3)
            feat = queryandgroup(self.nsample,
                                 point,
                                 new_point,
                                 feat,
                                 None,
                                 row_splits,
                                 new_row_splits,
                                 use_xyz=True)  # (m, 3+c, nsample)
            feat = self.relu(
                self.bn(self.linear(feat).transpose(
                    1, 2).contiguous()))  # (m, c, nsample)
            feat = self.pool(feat).squeeze(-1)  # (m, c)
            point, row_splits = new_point, new_row_splits
        else:
            feat = self.relu(self.bn(self.linear(feat)))  # (n, c)
        return [point, feat, row_splits]


class TransitionUp(nn.Module):

    def __init__(self, in_planes, out_planes=None):
        super().__init__()
        if out_planes is None:
            self.linear1 = nn.Sequential(nn.Linear(2 * in_planes, in_planes),
                                         nn.BatchNorm1d(in_planes),
                                         nn.ReLU(inplace=True))
            self.linear2 = nn.Sequential(nn.Linear(in_planes, in_planes),
                                         nn.ReLU(inplace=True))
        else:
            self.linear1 = nn.Sequential(nn.Linear(out_planes, out_planes),
                                         nn.BatchNorm1d(out_planes),
                                         nn.ReLU(inplace=True))
            self.linear2 = nn.Sequential(nn.Linear(in_planes, out_planes),
                                         nn.BatchNorm1d(out_planes),
                                         nn.ReLU(inplace=True))

    def forward(self, pxo1, pxo2=None):
        if pxo2 is None:
            _, feat, row_splits = pxo1  # (n, 3), (n, c), (b)
            feat_tmp = []
            for i in range(0, row_splits.shape[0] - 1):
                start_i, end_i, count = row_splits[i], row_splits[
                    i + 1], row_splits[i + 1] - row_splits[i]
                feat_b = feat[start_i:end_i, :]
                feat_b = torch.cat(
                    (feat_b, self.linear2(feat_b.sum(0, True) / count).repeat(
                        count, 1)), 1)
                feat_tmp.append(feat_b)
            feat = torch.cat(feat_tmp, 0)
            feat = self.linear1(feat)
        else:
            point_1, feat_1, row_splits_1 = pxo1
            point_2, feat_2, row_splits_2 = pxo2
            feat = self.linear1(feat_1) + interpolation(
                point_2, point_1, self.linear2(feat_2), row_splits_2,
                row_splits_1)
        return feat


class Bottleneck(nn.Module):
    expansion = 1

    def __init__(self, in_planes, planes, share_planes=8, nsample=16):
        super(Bottleneck, self).__init__()
        self.linear1 = nn.Linear(in_planes, planes, bias=False)
        self.bn1 = nn.BatchNorm1d(planes)
        self.transformer2 = Transformer(planes, planes, share_planes, nsample)
        self.bn2 = nn.BatchNorm1d(planes)
        self.linear3 = nn.Linear(planes, planes * self.expansion, bias=False)
        self.bn3 = nn.BatchNorm1d(planes * self.expansion)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, pxo):
        point, feat, row_splits = pxo  # (n, 3), (n, c), (b)
        identity = feat
        feat = self.relu(self.bn1(self.linear1(feat)))
        feat = self.relu(self.bn2(self.transformer2([point, feat, row_splits])))
        feat = self.bn3(self.linear3(feat))
        feat += identity
        feat = self.relu(feat)
        return [point, feat, row_splits]


def queryandgroup(nsample,
                  points,
                  queries,
                  feat,
                  idx,
                  points_row_splits,
                  queries_row_splits,
                  use_xyz=True):
    """
    input: xyz: (n, 3), new_xyz: (m, 3), feat: (n, c), idx: (m, nsample), offset: (b), new_offset: (b)
    output: new_feat: (m, c+3, nsample), grouped_idx: (m, nsample)
    """
    assert points.is_contiguous() and queries.is_contiguous(
    ) and feat.is_contiguous()
    if queries is None:
        queries = points
    if idx is None:
        idx = knn_batch(points,
                        queries,
                        k=nsample,
                        points_row_splits=points_row_splits,
                        queries_row_splits=queries_row_splits,
                        return_distances=False)
        # idx = []
        # for i in range(0, ans.neighbors_row_splits):
        #     start = ans.neighbors_row_splits[i]
        #     end = ans.neighbors_row_splits[i+1]
        #     if (end - start) < nsample:
        #         idx += ans.neighbors_index[start:end]
        #     else:
        #         idx += ans.neighbors_index[start:end]
        # exit(0)

        # TODO : pad idx if num_points < nsample

    n, m, c = points.shape[0], queries.shape[0], feat.shape[1]
    grouped_xyz = points[idx.view(-1).long(), :].view(m, nsample,
                                                      3)  # (m, nsample, 3)
    #grouped_xyz = grouping(xyz, idx) # (m, nsample, 3)
    grouped_xyz -= queries.unsqueeze(1)  # (m, nsample, 3)
    grouped_feat = feat[idx.view(-1).long(), :].view(m, nsample,
                                                     c)  # (m, nsample, c)
    #grouped_feat = grouping(feat, idx) # (m, nsample, c)

    if use_xyz:
        return torch.cat((grouped_xyz, grouped_feat), -1)  # (m, nsample, 3+c)
    else:
        return grouped_feat


def knn_batch(points,
              queries,
              k,
              points_row_splits,
              queries_row_splits,
              return_distances=True):
    assert points_row_splits.shape[0] == queries_row_splits.shape[
        0], "KNN(points and queries must have same batch size)"

    points = o3c.Tensor.from_dlpack(torch.utils.dlpack.to_dlpack(points))
    queries = o3c.Tensor.from_dlpack(torch.utils.dlpack.to_dlpack(queries))
    idxs = []
    dists = []

    for i in range(0, points_row_splits.shape[0] - 1):
        curr_points = points[points_row_splits[i]:points_row_splits[i + 1]]
        nns = o3c.nns.NearestNeighborSearch(curr_points)
        nns.knn_index()
        idx, dist = nns.knn_search(
            queries[queries_row_splits[i]:queries_row_splits[i + 1]], k)
        idx += points_row_splits[i]
        idxs.append(torch.utils.dlpack.from_dlpack(idx.to_dlpack()))
        dists.append(torch.utils.dlpack.from_dlpack(dist.to_dlpack()))

    if return_distances:
        return torch.cat(idxs, 0), torch.cat(dists, 0)
    else:
        return torch.cat(idxs, 0)


def interpolation(points,
                  queries,
                  feat,
                  points_row_splits,
                  queries_row_splits,
                  k=3):
    """
    input: xyz: (m, 3), new_xyz: (n, 3), feat: (m, c), offset: (b), new_offset: (b)
    output: (n, c)
    """
    assert points.is_contiguous() and queries.is_contiguous(
    ) and feat.is_contiguous()
    idx, dist = knn_batch(points,
                          queries,
                          k=k,
                          points_row_splits=points_row_splits,
                          queries_row_splits=queries_row_splits,
                          return_distances=True)  # (n, 3), (n, 3)

    # TODO : pad idx if num_points < nsample

    idx, dist = idx.reshape(-1, 3), dist.reshape(-1, 3)

    dist_recip = 1.0 / (dist + 1e-8)  # (n, 3)
    norm = torch.sum(dist_recip, dim=1, keepdim=True)
    weight = dist_recip / norm  # (n, 3)

    new_feat = torch.FloatTensor(queries.shape[0],
                                 feat.shape[1]).zero_().to(feat.device)
    for i in range(k):
        new_feat += feat[idx[:, i].long(), :] * weight[:, i].unsqueeze(-1)
    return new_feat


if __name__ == "__main__":
    import numpy as np, time
    import torch.optim as optim
    import random
    manual_seed = 123
    random.seed(manual_seed)
    np.random.seed(manual_seed)
    torch.manual_seed(manual_seed)
    num = 40960
    # num = 100000
    n, c, k = 4 * num, 6, 13
    input = torch.randn(n, c) * 10
    offset = torch.IntTensor([0, num, num * 2, num * 3,
                              num * 4]).to(torch.int64)
    label = torch.from_numpy(np.random.randint(0, k, size=(n)))
    # model = pointtransformer_seg26(c=c, k=k)
    model = PointTransformerSeg(Bottleneck, [2, 2, 2, 2, 2], c=c, k=k)
    print(model)
    optimizer = optim.Adam(model.parameters(), lr=1e-2)
    model_fn = model_fn_decorator(nn.CrossEntropyLoss())
    coord, feat = input[:, 0:3].contiguous(), input[:, 3:6].contiguous()
    for _ in range(10):
        t0 = time.time()
        optimizer.zero_grad()
        _, loss, _ = model_fn(model, (coord, feat, label, offset))
        t1 = time.time()
        loss.backward()
        optimizer.step()
        #torch.cuda.empty_cache()
        t2 = time.time()
        print("loss-{:.2f}-time-{:.2f}-l1-{:.2f}-l2-{:.2f}".format(
            loss.item(), t2 - t0, t1 - t0, t2 - t1))