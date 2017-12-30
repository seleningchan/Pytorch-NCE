#!/usr/bin/env python

import sys
import time
from datetime import datetime
import math

import torch
import torch.optim as optim

import data
from model import RNNModel
from nce import NCELoss
from utils import process_data, build_unigram_noise, setup_parser
from index_gru import IndexGRU
from index_linear import IndexLinear

parser = setup_parser()
args = parser.parse_args()
print(args)

# Initialize tensor-board summary writer
if args.tb_name:
    from tensorboard import SummaryWriter
    exp_name = '{} {}'.format(
        datetime.now().strftime('%B%d %H:%M:%S'),
        args.tb_name,
    )
    writer = SummaryWriter('runs/{}'.format(
        exp_name,
    ))

# Set the random seed manually for reproducibility.
torch.manual_seed(args.seed)
if torch.cuda.is_available():
    if not args.cuda:
        print("WARNING: You have a CUDA device, so you should probably run with --cuda")
    else:
        torch.cuda.manual_seed(args.seed)

#################################################################
# Load data
#################################################################
corpus = data.Corpus(
    path=args.data,
    dict_path=args.dict,
    batch_size=args.batch_size,
    shuffle=True,
    pin_memory=args.cuda,
)

eval_batch_size = args.batch_size
################################################################## Build the criterion and model
#################################################################

ntoken = len(corpus.train.dataset.dictionary)
print('Vocabulary size is {}'.format(ntoken))

# noise for soise sampling in NCE
noise = build_unigram_noise(
    torch.FloatTensor(corpus.train.dataset.dictionary.idx2count)
)

index_module = IndexLinear(args.nhid, ntoken)
#index_module = IndexGRU(ntoken, nhidden, nhidden, 0.5)
criterion = NCELoss(
    index_module=index_module,
    noise=noise,
    noise_ratio=args.noise_ratio,
    norm_term=args.norm_term,
)
criterion.nce_mode(args.nce)

model = RNNModel(
    ntoken, args.emsize, args.nhid, args.nlayers,
    criterion=criterion, dropout=args.dropout,
)
if args.cuda:
    model.cuda()
print(model)
#################################################################
# Training code
#################################################################


def train(model, data_source, lr=1.0, weight_decay=1e-5, momentum=0.9):
    optimizer = optim.SGD(
        params=model.parameters(),
        lr=lr,
        momentum=momentum,
        weight_decay=weight_decay
    )
    # Turn on training mode which enables dropout.
    model.train()
    model.criterion.nce_mode(args.nce)
    total_loss = 0
    for num_batch, data_batch in enumerate(corpus.train):
        optimizer.zero_grad()
        data, target, length = process_data(data_batch, cuda=args.cuda)
        loss = model(data, target, length)
        loss.backward()

        # `clip_grad_norm` helps prevent the exploding gradient problem in RNNs / LSTMs.
        torch.nn.utils.clip_grad_norm(model.parameters(), args.clip)
        optimizer.step()

        total_loss += loss.data[0]

        if num_batch % args.log_interval == 0 and num_batch > 0:
            if args.prof:
                break
            cur_loss = total_loss / args.log_interval
            print('| epoch {:3d} | {:5d}/{:5d} batches'
                  ' | lr {:02.2f} | '
                  'loss {:5.2f} | ppl {:8.2f}'.format(
                      epoch, num_batch, len(corpus.train), lr,
                      cur_loss, math.exp(cur_loss)))
            total_loss = 0
            print('-' * 87)

def evaluate(model, data_source, cuda=args.cuda):
    # Turn on evaluation mode which disables dropout.
    model.eval()
    model.criterion.disable_nce()
    eval_loss = 0
    total_length = 0

    data_source.batch_size = eval_batch_size
    for data_batch in data_source:
        data, target, length = process_data(data_batch, cuda=cuda, eval=True)

        loss = model(data, target, length)
        cur_length = length.sum()
        eval_loss += loss.data[0] * cur_length
        total_length += cur_length

    return math.exp(eval_loss/total_length)


if __name__ == '__main__':

    lr = args.lr
    best_val_ppl = None

    if args.train:
        # At any point you can hit Ctrl + C to break out of training early.
        try:
            # Loop over epochs.
            for epoch in range(1, args.epochs + 1):
                epoch_start_time = time.time()
                train(model, corpus.train, lr=lr, weight_decay=args.weight_decay)
                if args.prof:
                    break
                val_ppl = evaluate(model, corpus.valid)
                if args.tb_name:
                    writer.add_scalar('valid_PPL', val_ppl, epoch)
                print('-' * 89)
                print('| end of epoch {:3d} | time: {:5.2f}s |'
                    'valid ppl {:8.2f}'.format(epoch,
                                                (time.time() - epoch_start_time),
                                                val_ppl))
                print('-' * 89)
                with open(args.save+'.epoch_{}'.format(epoch), 'wb') as f:
                    torch.save(model, f)
                # Save the model if the validation loss is the best we've seen so far.
                if not best_val_ppl or val_ppl < best_val_ppl:
                    with open(args.save, 'wb') as f:
                        torch.save(model, f)
                    best_val_ppl = val_ppl
                else:
                    # Anneal the learning rate if no improvement has been seen in the
                    # validation dataset.
                    lr /= args.lr_decay
        except KeyboardInterrupt:
            print('-' * 89)
            print('Exiting from training early')

    else:
        # Load the best saved model.
        with open(args.save, 'rb') as f:
            model = torch.load(f)

    # Run on test data.
    test_ppl = evaluate(model, corpus.test)
    print('=' * 89)
    print('| End of training | test ppl {:8.2f}'.format(test_ppl))
    print('=' * 89)
    sys.stdout.flush()

    if args.tb_name:
        writer.close()
