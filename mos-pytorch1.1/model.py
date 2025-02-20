import math
import torch
import torch.nn as nn
import torch.nn.functional as F

from embed_regularize import embedded_dropout
from locked_dropout import MyLockedDropout as LockedDropout
from weight_drop import WeightDrop


class RNNModel(nn.Module):
    """Container module with an encoder, a recurrent module, and a decoder."""

    # TODO: add here mask for epoch flag & mask properties
    def __init__(self, rnn_type, ntoken, ninp, nhid, nhidlast, nlayers,
                 dropout=0.5, dropouth=0.5, dropouti=0.5, dropoute=0.1, wdrop=0,
                 tie_weights=False, ldropout=0.5, n_experts=10):
        super(RNNModel, self).__init__()
        self.use_dropout = True
        self.lockdrop = LockedDropout()
        self.encoder = nn.Embedding(ntoken, ninp)

        self.rnns = [torch.nn.LSTM(ninp if l == 0 else nhid, nhid if l != nlayers - 1 else nhidlast, 1, dropout=0) for l
                     in range(nlayers)]
        if wdrop:
            # note: DEACTIVATING variational to weight drop... 1/11/20
            self.rnns = [WeightDrop(rnn, ['weight_hh_l0'], dropout=wdrop if self.use_dropout else 0) for rnn in
                         self.rnns]
        self.rnns = torch.nn.ModuleList(self.rnns)

        self.prior = nn.Linear(nhidlast, n_experts, bias=False)
        self.latent = nn.Sequential(nn.Linear(nhidlast, n_experts * ninp), nn.Tanh())
        self.decoder = nn.Linear(ninp, ntoken)

        # Optionally tie weights as in:
        # "Using the Output Embedding to Improve Language Models" (Press & Wolf 2016)
        # https://arxiv.org/abs/1608.05859
        # and
        # "Tying Word Vectors and Word Classifiers: A Loss Framework for Language Modeling" (Inan et al. 2016)
        # https://arxiv.org/abs/1611.01462
        if tie_weights:
            # if nhid != ninp:
            #    raise ValueError('When using the tied flag, nhid must be equal to emsize')
            self.decoder.weight = self.encoder.weight

        self.init_weights()

        self.rnn_type = rnn_type
        self.ninp = ninp
        self.nhid = nhid
        self.nhidlast = nhidlast
        self.nlayers = nlayers
        self.dropout = dropout
        self.dropouti = dropouti
        self.dropouth = dropouth
        self.dropoute = dropoute
        self.ldropout = ldropout
        self.dropoutl = ldropout
        self.n_experts = n_experts
        self.ntoken = ntoken

        # mc_eval - to notice the model not to multiple the masks of the dropout
        self.mc_eval = False

        size = 0
        for p in self.parameters():
            size += p.nelement()
        print('param size: {}'.format(size))

    def init_weights(self):
        initrange = 0.1
        self.encoder.weight.data.uniform_(-initrange, initrange)
        self.decoder.bias.data.fill_(0)
        self.decoder.weight.data.uniform_(-initrange, initrange)

    def forward(self, input, hidden, return_h=False, return_prob=False):
        batch_size = input.size(1)

        # usedp = False if we are at normal eval
        emb = embedded_dropout(self.encoder, input, dropout=self.dropoute, usedp=(self.training and self.use_dropout),
                               mc_eval=self.mc_eval)
        # emb = self.idrop(emb)

        emb = self.lockdrop(emb, dropout=self.dropouti if self.use_dropout else 0
                            , mc_eval=self.mc_eval)

        raw_output = emb
        new_hidden = []
        # raw_output, hidden = self.rnn(emb, hidden)
        raw_outputs = []
        outputs = []
        for l, rnn in enumerate(self.rnns):
            current_input = raw_output
            rnn.mc_eval = self.mc_eval  # note: setting the mc_eval to the current forward state - to div/not in (1-p)
            raw_output, new_h = rnn(raw_output, hidden[l])
            new_hidden.append(new_h)
            raw_outputs.append(raw_output)
            if l != self.nlayers - 1:
                # self.hdrop(raw_output)
                raw_output = self.lockdrop(raw_output, dropout=self.dropouth if self.use_dropout else 0
                                           , mc_eval=self.mc_eval)
                outputs.append(raw_output)
        hidden = new_hidden

        output = self.lockdrop(raw_output, dropout=self.dropout if self.use_dropout else 0
                               , mc_eval=self.mc_eval)
        outputs.append(output)  # this i G

        latent = self.latent(output)  # this is H (tanh(W1 * G)
        latent = self.lockdrop(latent, dropout=self.dropoutl if self.use_dropout else 0
                               , mc_eval=self.mc_eval)
        logit = self.decoder(latent.view(-1, self.ninp))  # this is the logit = W2 * H

        prior_logit = self.prior(output).contiguous().view(-1, self.n_experts)  # W3 * G
        prior = nn.functional.softmax(prior_logit, -1)  # softmax ( W3 * G )

        prob = nn.functional.softmax(logit.view(-1, self.ntoken), -1).view(-1, self.n_experts, self.ntoken)  # N x M
        prob = (prob * prior.unsqueeze(2).expand_as(prob)).sum(1)

        if return_prob:
            model_output = prob
        else:
            log_prob = torch.log(prob.add_(1e-8))
            model_output = log_prob

        model_output = model_output.view(-1, batch_size, self.ntoken)

        if return_h:
            return model_output, hidden, raw_outputs, outputs
        return model_output, hidden

    def init_hidden(self, bsz):
        weight = next(self.parameters()).data
        return [(weight.new(1, bsz, self.nhid if l != self.nlayers - 1 else self.nhidlast).zero_(),
                 weight.new(1, bsz, self.nhid if l != self.nlayers - 1 else self.nhidlast).zero_())
                for l in range(self.nlayers)]


if __name__ == '__main__':
    model = RNNModel('LSTM', 10, 12, 12, 12, 2)
    input = torch.LongTensor(13, 9).random_(0, 10)
    hidden = model.init_hidden(9)
    model(input, hidden)
    print(model)

    # input = Variable(torch.LongTensor(13, 9).random_(0, 10))
    # hidden = model.init_hidden(9)
    # print(model.sample(input, hidden, 5, 6, 1, 2, sample_latent=True).size())
