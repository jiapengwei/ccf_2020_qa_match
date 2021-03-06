# -*- coding: utf-8 -*-
# @Date    : 2020/12/4
# @Author  : mingming.xu
# @Email   : xv44586@gmail.com
# @File    : pair-data-augment-constrastive-learning.py
"""
借鉴无监督中借助数据增强来做对比学习，采用query-reply 对为基本样本形式，通过互换query/reply 的位置构造新样本，做对比学习

线下结果：提升不明显
"""
import os
import numpy as np
from tqdm import tqdm

from toolkit4nlp.utils import *
from toolkit4nlp.models import *
from toolkit4nlp.tokenizers import *
from toolkit4nlp.backend import *
from toolkit4nlp.layers import *
from toolkit4nlp.optimizers import *

path = '/home/mingming.xu/datasets/NLP/ccf_qa_match/'

maxlen = 128
batch_size = 32
epochs = 10

config_path = '/home/mingming.xu/pretrain/NLP/nezha_base_wwm/bert_config.json'
checkpoint_path = '/home/mingming.xu/pretrain/NLP/nezha_base_wwm/model.ckpt'
dict_path = '/home/mingming.xu/pretrain/NLP/nezha_base_wwm/vocab.txt'

# 建立分词器
token_dict, keep_tokens = load_vocab(dict_path,
                                     simplified=True,
                                     startswith=['[PAD]', '[UNK]', '[MASK]', '[CLS]', '[SEP]'])

tokenizer = Tokenizer(token_dict, do_lower_case=True)


def load_data(train_test='train'):
    D = {}
    with open(os.path.join(path, train_test, train_test + '.query.tsv')) as f:
        for l in f:
            span = l.strip().split('\t')
            D[span[0]] = {'query': span[1], 'reply': []}

    with open(os.path.join(path, train_test, train_test + '.reply.tsv')) as f:
        for l in f:
            span = l.strip().split('\t')
            if len(span) == 4:
                q_id, r_id, r, label = span
            else:
                label = None
                q_id, r_id, r = span
            D[q_id]['reply'].append([r_id, r, label])
    d = []
    for k, v in D.items():
        q_id = k
        q = v['query']
        reply = v['reply']

        for r in reply:
            r_id, rc, label = r

            d.append([q_id, q, r_id, rc, label])
    return d


train_data = load_data('train')
test_data = load_data('test')


def can_padding(token_id):
    if token_id in (tokenizer._token_mask_id, tokenizer._token_end_id, tokenizer._token_start_id):
        return False
    return True


class data_generator(DataGenerator):
    def random_padding(self, token_ids):
        rands = np.random.random(len(token_ids))
        new_tokens = []
        for p, token in zip(rands, token_ids):
            if p < 0.1 and can_padding(token):
                new_tokens.append(tokenizer._token_pad_id)
            else:
                new_tokens.append(token)
        return new_tokens

    def __iter__(self, shuffle=False):
        batch_token_ids, batch_segment_ids, batch_labels = [], [], []
        for is_end, (q_id, q, r_id, r, label) in self.get_sample(shuffle):
            label = float(label) if label is not None else None

            if shuffle:
                token_ids_1, segment_ids_1 = tokenizer.encode(q, r, maxlen=maxlen)
                token_ids_1 = self.random_padding(token_ids_1)
                token_ids_2, segment_ids_2 = tokenizer.encode(r, q, maxlen=maxlen)
                token_ids_2 = self.random_padding(token_ids_2)
                batch_token_ids.extend([token_ids_1, token_ids_2])
                batch_segment_ids.extend([segment_ids_1, segment_ids_2])
                batch_labels.extend([[label], [label]])

            else:
                token_ids, segment_ids = tokenizer.encode(q, r, maxlen=maxlen)
                batch_token_ids.append(token_ids)
                batch_segment_ids.append(segment_ids)
                batch_labels.append([label])

            if is_end or len(batch_token_ids) == self.batch_size * 2:
                batch_token_ids = pad_sequences(batch_token_ids)
                batch_segment_ids = pad_sequences(batch_segment_ids)
                batch_labels = pad_sequences(batch_labels)

                yield [batch_token_ids, batch_segment_ids], batch_labels

                batch_token_ids, batch_segment_ids, batch_labels = [], [], []


# shuffle
np.random.shuffle(train_data)
n = int(len(train_data) * 0.8)
valid_data, train_data = train_data[n:], train_data[:n]
train_generator = data_generator(data=train_data, batch_size=batch_size)
valid_generator = data_generator(data=valid_data, batch_size=batch_size)
test_generator = data_generator(data=test_data, batch_size=batch_size)
print(len(train_data), len(valid_data))


class ContrastiveLoss(Loss):
    """loss: 相似度的交叉熵。
    """

    def __init__(self, alpha=1., T=1., **kwargs):
        super(ContrastiveLoss, self).__init__(**kwargs)
        self.alpha = alpha  # 权重weight
        self.T = T  # 平滑温度

    def compute_loss(self, inputs, mask=None):
        loss = self.compute_loss_of_similarity(inputs, mask)
        loss = loss * self.alpha
        self.add_metric(loss, name='similarity_loss')
        return loss

    def compute_loss_of_similarity(self, inputs, mask=None):
        y_pred = inputs
        y_true = self.get_labels_of_similarity(y_pred)  # 构建标签
        y_pred = K.l2_normalize(y_pred, axis=1)  # 句向量归一化
        similarities = K.dot(y_pred, K.transpose(y_pred))  # 相似度矩阵
        similarities = similarities - K.eye(K.shape(y_pred)[0]) * 1e12  # 排除对角线
        similarities = similarities / self.T  # scale
        loss = K.categorical_crossentropy(
            y_true, similarities, from_logits=True
        )
        return loss

    def get_labels_of_similarity(self, y_pred):
        idxs = K.arange(0, K.shape(y_pred)[0])
        idxs_1 = idxs[None, :]
        idxs_2 = (idxs + 1 - idxs % 2 * 2)[:, None]
        labels = K.equal(idxs_1, idxs_2)
        labels = K.cast(labels, K.floatx())
        return labels


# 加载预训练模型
bert = build_transformer_model(
    config_path=config_path,
    checkpoint_path=checkpoint_path,
    model='nezha',
    keep_tokens=keep_tokens,
    num_hidden_layers=10,  #
)
output = Lambda(lambda x: x[:, 0])(bert.output)

cons_output = ContrastiveLoss(alpha=1, T=0.1)(output)

output = Dropout(0.1)(output)
output = Dense(2)(output)
clf_output = Activation('softmax', name='clf')(output)

model = keras.models.Model(bert.input, clf_output)
model.summary()

train_model = keras.models.Model(bert.input, [cons_output, clf_output])
optimizer = extend_with_weight_decay(Adam)
optimizer = extend_with_piecewise_linear_lr(optimizer)
opt = optimizer(learning_rate=1e-5, weight_decay_rate=0.1, exclude_from_weight_decay=['Norm', 'bias'],
                lr_schedule={int(len(train_generator) * 0.1 * epochs): 1, len(train_generator) * epochs: 0}
                )

train_model.compile(
    loss=[None, 'sparse_categorical_crossentropy'],
    optimizer=opt,
)


def evaluate(data):
    P, R, TP = 0., 0., 0.
    for x_true, y_true in tqdm(data):
        y_pred = model.predict(x_true).argmax(axis=1)
        #         y_pred = np.round(y_pred)
        y_true = y_true[:, 0]

        R += y_pred.sum()
        P += y_true.sum()
        TP += ((y_pred + y_true) > 1).sum()

    print(P, R, TP)
    pre = TP / R
    rec = TP / P

    return 2 * (pre * rec) / (pre + rec)


class Evaluator(keras.callbacks.Callback):
    """评估与保存
    """

    def __init__(self, save_path):
        self.best_val_f1 = 0.
        self.save_path = save_path

    def on_epoch_end(self, epoch, logs=None):
        val_f1 = evaluate(valid_generator)
        if val_f1 > self.best_val_f1:
            self.best_val_f1 = val_f1
            model.save_weights(self.save_path)
        print(
                u'val_f1: %.5f, best_val_f1: %.5f\n' %
                (val_f1, self.best_val_f1)
        )


def predict_to_file(path='pair_submission.tsv', data=test_generator):
    preds = []
    for x, _ in tqdm(test_generator):
        pred = model.predict(x)[:, 0]
        pred = np.round(pred)
        pred = pred.astype(int)
        preds.extend(pred)

    ret = []
    for d, p in zip(test_data, preds):
        q_id, _, r_id, _, _ = d
        ret.append([str(q_id), str(r_id), str(p)])

    with open(path, 'w', encoding='utf8') as f:
        for l in ret:
            f.write('\t'.join(l) + '\n')


if __name__ == '__main__':
    save_path = 'best_parimatch_ag_cl_model.weights'
    evaluator = Evaluator(save_path)
    train_model.fit_generator(
        train_generator.generator(),
        steps_per_epoch=len(train_generator),
        epochs=epochs,
        callbacks=[evaluator],
    )

    model.load_weights(save_path)
    predict_to_file('pair_ag_cl_submission.tsv')
