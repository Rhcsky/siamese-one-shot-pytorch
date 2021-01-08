import os
from glob import glob

import torch
import torch.optim as optim
from tqdm import tqdm

from data_loader import get_train_validation_loader, get_test_loader
from model import SiameseNet
from utils import AverageMeter


class Trainer(object):
    """
    Trainer encapsulates all the logic necessary for training
    the Siamese Network model.

    All hyperparameters are provided by the user in the
    config file.
    """

    def __init__(self, config):
        """
        Construct a new Trainer instance.

        Args
        ----
        - config: object containing command line arguments.
        """
        self.config = config

        # path params
        self.model_dir = os.path.join(config.model_dir, config.num_model)
        self.logs_dir = os.path.join(config.logs_dir, config.num_model)
        self.device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")

    def train(self):
        # Dataloader
        train_loader, valid_loader = get_train_validation_loader(self.config.data_dir, self.config.batch_size,
                                                                 self.config.num_train,
                                                                 self.config.augment, self.config.way,
                                                                 self.config.valid_trials,
                                                                 self.config.shuffle, self.config.seed,
                                                                 self.config.num_workers, self.config.pin_memory)

        # Model, Optimizer, criterion
        model = SiameseNet()
        optimizer = optim.Adam(model.parameters(), lr=3e-4, weight_decay=6e-5)
        criterion = torch.nn.BCEWithLogitsLoss()
        if self.config.use_gpu:
            model.cuda()

        # Load check point
        if self.config.resume:
            start_epoch, best_epoch, best_valid_acc, model_state, optim_state = self.load_checkpoint(best=False)
            model.load_state_dict(model_state)
            optimizer.load_state_dict(optim_state)
        else:
            best_epoch = 0
            start_epoch = 0
            best_valid_acc = 0

        # create train and validation log files
        train_file = open(os.path.join(self.logs_dir, 'train.csv'), 'w')
        valid_file = open(os.path.join(self.logs_dir, 'valid.csv'), 'w')

        counter = 0
        num_train = len(train_loader)
        num_valid = len(valid_loader)
        print(
            f"[*] Train on {len(train_loader.dataset)} sample pairs, validate on {valid_loader.dataset.trials} trials")

        # Train & Validation
        main_pbar = tqdm(range(0, self.config.epochs), initial=start_epoch, position=0,
                         total=self.config.epochs, ncols=100, desc="Process")
        for epoch in main_pbar:
            train_losses = AverageMeter()

            # TRAIN
            model.train()
            train_pbar = tqdm(enumerate(train_loader), total=num_train, desc="Train", ncols=100, position=1,
                              leave=False)
            for i, (x1, x2, y) in train_pbar:
                if self.config.use_gpu:
                    x1, x2, y = x1.to(self.device), x2.to(self.device), y.to(self.device)
                out = model(x1, x2)
                loss = criterion(out, y.unsqueeze(1))

                # compute gradients and update
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

                # store batch statistics
                batch_size = x1.shape[0]
                train_losses.update(loss.item(), batch_size)

                # log loss
                train_file.write(f'{(epoch * len(train_loader)) + i},{train_losses.val}\n')
                train_pbar.set_postfix_str(f"loss: {train_losses.val:0.3f}")

            # VALIDATION
            model.eval()
            correct = 0
            valid_acc = 0
            valid_pbar = tqdm(enumerate(valid_loader), total=num_valid, desc="Valid", ncols=100, position=1,
                              leave=False)
            with torch.no_grad():
                for i, (x1, x2, y) in valid_pbar:
                    if self.config.use_gpu:
                        x1, x2 = x1.to(self.device), x2.to(self.device)

                    # compute log probabilities
                    out = model(x1, x2)
                    log_probas = torch.sigmoid(out)

                    # get index of max log prob
                    pred = log_probas.data.max(0)[1][0]
                    if pred == 0:
                        correct += 1

                    print('*' * 40)
                    print(pred, y, correct)
                    print('*' * 40)

                    # compute acc and log
                    valid_acc = correct / num_valid
                    valid_file.write(f'{epoch},{valid_acc}\n')
                    valid_pbar.set_postfix_str(f"accuracy: {valid_acc:0.3f}")

            # check for improvement
            if valid_acc > best_valid_acc:
                is_best = True
                best_valid_acc = valid_acc
                best_epoch = epoch
                counter = 0
            else:
                is_best = False
                counter += 1

            # checkpoint the model
            if counter > self.config.train_patience:
                print("[!] No improvement in a while, stopping training.")
                return

            if is_best or epoch % 5 == 0 or epoch == self.config.epochs:
                self.save_checkpoint(
                    {
                        'epoch': epoch + 1,
                        'model_state': model.state_dict(),
                        'optim_state': optimizer.state_dict(),
                        'best_valid_acc': best_valid_acc,
                        'best_epoch': best_epoch,
                    }, is_best
                )

            main_pbar.set_postfix_str(f"best acc: {best_valid_acc} best epoch: {best_epoch} ")

            tqdm.write(f"[{epoch}] train loss: {train_losses.avg:.3f} - valid loss: {log} - valid acc: {valid_acc}")

        # release resources
        train_file.close()
        valid_file.close()

    def test(self):

        # Load best model
        model = SiameseNet()
        _, _, _, model_state, _ = self.load_checkpoint(best=self.config.best)
        model.load_state_dict(model_state)
        if self.config.use_gpu:
            model.cuda()

        test_loader = get_test_loader(self.config.data_dir, self.config.way, self.config.test_trials,
                                      self.config.seed, self.config.num_workers, self.config.pin_memory)
        num_test = test_loader.dataset.trials
        correct = 0

        pbar = tqdm(enumerate(test_loader), total=num_test, desc="Test")
        for i, (x1, x2, y) in pbar:
            if self.config.use_gpu:
                x1, x2 = x1.cuda(), x2.cuda()

            # compute log probabilities
            out = model(x1, x2)
            log_probas = torch.sigmoid(out)

            # get index of max log prob
            pred = log_probas.data.max(0)[1][0]
            if pred == 0:
                correct += 1
            pbar.set_postfix_str(f"accuracy: {correct} / {num_test}")

        test_acc = (100. * correct) / num_test
        print(f"Test Acc: {correct}/{num_test} ({test_acc:.2f}%)")

    def save_checkpoint(self, state, is_best):

        if is_best:
            filename = 'best_model.tar'
        else:
            filename = f'model_ckpt_{state["epoch"]}.tar'

        model_path = os.path.join(self.model_dir, filename)
        torch.save(state, model_path)

    def load_checkpoint(self, best):
        print(f"[*] Loading model Num.{self.config.num_model}...", end="")

        model_path = sorted(glob(self.model_dir + '/model_ckpt_*'), key=len)[-1]

        if best:
            filename = 'best_model.tar'
            model_path = os.path.join(self.model_dir, filename)

        ckpt = torch.load(model_path)

        if best:
            print(
                f"Loaded {filename} checkpoint @ epoch {ckpt['epoch']} with best valid acc of {ckpt['best_valid_acc']:.3f}")
        else:
            print(f"Loaded {os.path.basename(model_path)} checkpoint @ epoch {ckpt['epoch']}")

        return ckpt['epoch'], ckpt['best_epoch'], ckpt['best_valid_acc'], ckpt['model_state'], ckpt['optim_state']
