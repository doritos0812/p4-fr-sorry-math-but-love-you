import os
import argparse
import random
import time
from tqdm import tqdm
import yaml
import shutil
from psutil import virtual_memory
import multiprocessing
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torchvision import transforms
from torch.cuda.amp import autocast, GradScaler
import albumentations as A
from albumentations.pytorch import ToTensorV2
import wandb
from checkpoint import (
    default_checkpoint,
    load_checkpoint,
    save_checkpoint,
    init_tensorboard,
    write_tensorboard,
    write_wandb # added
)
from flags import Flags
from utils import (
    set_seed, 
    print_system_envs, 
    get_optimizer, 
    get_network, 
    id_to_string
    )
from dataset import dataset_loader, START, PAD,load_vocab
from scheduler import CircularLRBeta, CustomCosineAnnealingWarmUpRestarts
from criterion import get_criterion
from metrics import word_error_rate, sentence_acc, final_metric
os.environ["WANDB_LOG_MODEL"] = "true"
os.environ["WANDB_WATCH"] = "all"


def train_one_epoch(
    data_loader, 
    model, 
    epoch_text, 
    criterion, 
    optimizer, 
    lr_scheduler, 
    teacher_forcing_ratio, 
    max_grad_norm, 
    device, 
    scaler
    ):
    torch.set_grad_enabled(True)
    model.train()

    losses = []
    grad_norms = []
    correct_symbols = 0
    total_symbols = 0
    wer = 0
    num_wer = 0
    sent_acc = 0
    num_sent_acc = 0

    with tqdm(desc=f"{epoch_text} Train", total=len(data_loader.dataset), dynamic_ncols=True, leave=False) as pbar:
        for d in data_loader:
            input = d["image"].to(device).float()

            curr_batch_size = len(input)
            expected = d["truth"]["encoded"].to(device)

            expected[expected==-1] = data_loader.dataset.token_to_id[PAD]

            with autocast():
                output = model(input, expected, True, teacher_forcing_ratio)

                decoded_values = output.transpose(1, 2)
                _, sequence = torch.topk(decoded_values, 1, dim=1)
                sequence = sequence.squeeze(1)

                loss = criterion(decoded_values, expected[:, 1:])

            optim_params = [p for param_group in optimizer.param_groups for p in param_group["params"]]
            optimizer.zero_grad()
            scaler.scale(loss).backward()
            scaler.unscale_(optimizer)

            grad_norm = nn.utils.clip_grad_norm_(
                optim_params, max_norm=max_grad_norm
            )
            grad_norms.append(grad_norm)

            # cycle
            scaler.step(optimizer)
            scaler.update()

            losses.append(loss.item())

            expected[expected == data_loader.dataset.token_to_id[PAD]] = -1
            expected_str = id_to_string(expected, data_loader,do_eval=1)
            sequence_str = id_to_string(sequence, data_loader,do_eval=1)
            wer += word_error_rate(sequence_str,expected_str)
            num_wer += 1
            sent_acc += sentence_acc(sequence_str,expected_str)
            num_sent_acc += 1
            correct_symbols += torch.sum(sequence == expected[:, 1:], dim=(0, 1)).item()
            total_symbols += torch.sum(expected[:, 1:] != -1, dim=(0, 1)).item()

            pbar.update(curr_batch_size)
            lr_scheduler.step()

    expected = id_to_string(expected, data_loader)
    sequence = id_to_string(sequence, data_loader)
    # print("-" * 10 + "GT train")
    # print(*expected[:3], sep="\n")
    # print("-" * 10 + "PR train")
    # print(*sequence[:3], sep="\n")
    # print(f"{lr_scheduler.get_lr()[0]}\n")

    result = {
        "loss": np.mean(losses),
        "correct_symbols": correct_symbols,
        "total_symbols": total_symbols,
        "wer": wer,
        "num_wer":num_wer,
        "sent_acc": sent_acc,
        "num_sent_acc":num_sent_acc
    }

    try:
        result["grad_norm"] = np.mean([tensor.cpu() for tensor in grad_norms])
    except:
        result["grad_norm"] = np.mean(grad_norms)

    return result

def valid_one_epoch(
    data_loader, 
    model, 
    epoch_text, 
    criterion, 
    device, 
    teacher_forcing_ratio
    ):
    model.eval()

    losses = []
    correct_symbols = 0
    total_symbols = 0
    wer = 0
    num_wer = 0
    sent_acc = 0
    num_sent_acc = 0

    with tqdm(desc=f"{epoch_text} Validation", total=len(data_loader.dataset), dynamic_ncols=True, leave=False) as pbar:
        for d in data_loader:
            input = d["image"].to(device).float()

            curr_batch_size = len(input)
            expected = d["truth"]["encoded"].to(device)

            expected[expected==-1] = data_loader.dataset.token_to_id[PAD]
            with autocast():
                output = model(input, expected, False, teacher_forcing_ratio)

                decoded_values = output.transpose(1, 2)
                _, sequence = torch.topk(decoded_values, 1, dim=1)
                sequence = sequence.squeeze(1)

                loss = criterion(decoded_values, expected[:, 1:])

            losses.append(loss.item())
            
            expected[expected == data_loader.dataset.token_to_id[PAD]] = -1
            expected_str = id_to_string(expected, data_loader,do_eval=1)
            sequence_str = id_to_string(sequence, data_loader,do_eval=1)
            wer += word_error_rate(sequence_str,expected_str)
            num_wer += 1
            sent_acc += sentence_acc(sequence_str,expected_str)
            num_sent_acc += 1
            correct_symbols += torch.sum(sequence == expected[:, 1:], dim=(0, 1)).item()
            total_symbols += torch.sum(expected[:, 1:] != -1, dim=(0, 1)).item()

            pbar.update(curr_batch_size)

    expected = id_to_string(expected, data_loader)
    sequence = id_to_string(sequence, data_loader)
    # print("-" * 10 + "GT valid")
    # print(*expected[:3], sep="\n")
    # print("-" * 10 + "PR valid")
    # print(*sequence[:3], sep="\n")

    result = {
        "loss": np.mean(losses),
        "correct_symbols": correct_symbols,
        "total_symbols": total_symbols,
        "wer": wer,
        "num_wer":num_wer,
        "sent_acc": sent_acc,
        "num_sent_acc":num_sent_acc
    }
    return result

def run_epoch(
    data_loader,
    model,
    epoch_text,
    criterion,
    optimizer,
    lr_scheduler,
    teacher_forcing_ratio,
    max_grad_norm,
    device,
    train=True,
):
    # Disables autograd during validation mode
    torch.set_grad_enabled(train)
    if train:
        model.train()
    else:
        model.eval()

    losses = []
    grad_norms = []
    correct_symbols = 0
    total_symbols = 0
    wer = 0
    num_wer = 0
    sent_acc = 0
    num_sent_acc = 0

    with tqdm(
        desc="{} ({})".format(epoch_text, "Train" if train else "Validation"),
        total=len(data_loader.dataset),
        dynamic_ncols=True,
        leave=False,
    ) as pbar:
        for d in data_loader:
            input = d["image"].to(device)

            # The last batch may not be a full batch
            curr_batch_size = len(input)
            expected = d["truth"]["encoded"].to(device)

            # Replace -1 with the PAD token
            expected[expected == -1] = data_loader.dataset.token_to_id[PAD]

            output = model(input, expected, train, teacher_forcing_ratio)
            
            decoded_values = output.transpose(1, 2)
            _, sequence = torch.topk(decoded_values, 1, dim=1)
            sequence = sequence.squeeze(1)
            
            loss = criterion(decoded_values, expected[:, 1:])

            if train:
                optim_params = [
                    p
                    for param_group in optimizer.param_groups
                    for p in param_group["params"]
                ]
                optimizer.zero_grad()
                loss.backward()
                # Clip gradients, it returns the total norm of all parameters
                grad_norm = nn.utils.clip_grad_norm_(
                    optim_params, max_norm=max_grad_norm
                )
                grad_norms.append(grad_norm)

                # cycle
                lr_scheduler.step()
                optimizer.step()

                # log lr
                wandb.log({"learning_rate": lr_scheduler.get_lr()})

            losses.append(loss.item())
            
            expected[expected == data_loader.dataset.token_to_id[PAD]] = -1
            expected_str = id_to_string(expected, data_loader,do_eval=1)
            sequence_str = id_to_string(sequence, data_loader,do_eval=1)
            wer += word_error_rate(sequence_str,expected_str)
            num_wer += 1
            sent_acc += sentence_acc(sequence_str,expected_str)
            num_sent_acc += 1
            correct_symbols += torch.sum(sequence == expected[:, 1:], dim=(0, 1)).item()
            total_symbols += torch.sum(expected[:, 1:] != -1, dim=(0, 1)).item()

            pbar.update(curr_batch_size)

    expected = id_to_string(expected, data_loader)
    sequence = id_to_string(sequence, data_loader)
    print("-" * 10 + "GT ({})".format("train" if train else "valid"))
    print(*expected[:3], sep="\n")
    print("-" * 10 + "PR ({})".format("train" if train else "valid"))
    print(*sequence[:3], sep="\n")

    result = {
        "loss": np.mean(losses),
        "correct_symbols": correct_symbols,
        "total_symbols": total_symbols,
        "wer": wer,
        "num_wer":num_wer,
        "sent_acc": sent_acc,
        "num_sent_acc":num_sent_acc
    }
    if train:
        try:
            result["grad_norm"] = np.mean([tensor.cpu() for tensor in grad_norms])
        except:
            result["grad_norm"] = np.mean(grad_norms)

    return result

def get_train_transforms(height, width):
    return A.Compose([
        A.Resize(height, width),
        A.Compose([
            A.HorizontalFlip(p=1),
            A.VerticalFlip(p=1)
        ],p=0.5),
        ToTensorV2(p = 1.0),
    ],p=1.0)

def get_valid_transforms(height, width):
    return A.Compose([
        A.Resize(height, width),
        ToTensorV2(p = 1.0)
        ])

def main(config_file):
    """
    Train math formula recognition model
    """
    options = Flags(config_file).get()
    
    # set random seed
    set_seed(seed=options.seed)

    
    
    is_cuda = torch.cuda.is_available()
    hardware = "cuda" if is_cuda else "cpu"
    device = torch.device(hardware)
    print("--------------------------------")
    print("Running {} on device {}\n".format(options.network, device))

    # Print system environments
    print_system_envs() 

    # Load checkpoint and print result
    checkpoint = (
        load_checkpoint(options.checkpoint, cuda=is_cuda)
        if options.checkpoint != ""
        else default_checkpoint
    )

    model_checkpoint = checkpoint["model"]
    if model_checkpoint:
        print(
            "[+] Checkpoint\n",
            "Resuming from epoch : {}\n".format(checkpoint["epoch"]),
            "Train Symbol Accuracy : {:.5f}\n".format(checkpoint["train_symbol_accuracy"][-1]),
            "Train Sentence Accuracy : {:.5f}\n".format(checkpoint["train_sentence_accuracy"][-1]),
            "Train WER : {:.5f}\n".format(checkpoint["train_wer"][-1]),
            "Train Loss : {:.5f}\n".format(checkpoint["train_losses"][-1]),
            "Validation Symbol Accuracy : {:.5f}\n".format(
                checkpoint["validation_symbol_accuracy"][-1]
            ),
            "Validation Sentence Accuracy : {:.5f}\n".format(
                checkpoint["validation_sentence_accuracy"][-1]
            ),
            "Validation WER : {:.5f}\n".format(
                checkpoint["validation_wer"][-1]
            ),
            "Validation Loss : {:.5f}\n".format(checkpoint["validation_losses"][-1]),
        )

    # Get data
    # transformed = transforms.Compose(
    #     [
    #         # Resize so all images have the same size
    #         transforms.Resize((options.input_size.height, options.input_size.width)),
    #         transforms.ToTensor(),
    #     ]
    # )


    train_data_loader, validation_data_loader, train_dataset, valid_dataset = dataset_loader(options, train_transform=get_train_transforms(options.input_size.height, options.input_size.width), valid_transform=get_valid_transforms(options.input_size.height, options.input_size.width))
    # train_data_loader, validation_data_loader, train_dataset, valid_dataset = dataset_loader(options, transformed, transformed)
    print(
        "[+] Data\n",
        "The number of train samples : {}\n".format(len(train_dataset)),
        "The number of validation samples : {}\n".format(len(valid_dataset)),
        "The number of classes : {}\n".format(len(train_dataset.token_to_id)),
    )

    # define model
    model = get_network(
        options.network,
        options,
        model_checkpoint,
        device,
        train_dataset,
    )
    model.train()

    # define loss
    # criterion = model.criterion.to(device)
    criterion = get_criterion(type=options.criterion).to(device)

    # define optimizer
    enc_params_to_optimise = [
        param for param in model.encoder.parameters() if param.requires_grad
    ]
    dec_params_to_optimise = [
        param for param in model.decoder.parameters() if param.requires_grad
    ]
    params_to_optimise = [*enc_params_to_optimise, *dec_params_to_optimise]
    print(
        "[+] Network\n",
        "Type: {}\n".format(options.network),
        "Encoder parameters: {}\n".format(
            sum(p.numel() for p in enc_params_to_optimise),
        ),
        "Decoder parameters: {} \n".format(
            sum(p.numel() for p in dec_params_to_optimise),
        ),
    )

    # Get optimizer and optimizer
    if options.scheduler.scheduler == "CustomCosine":
        optimizer = get_optimizer(
            options.optimizer.optimizer,
            params_to_optimise,
            lr=0,
            weight_decay=options.optimizer.weight_decay,
        )
        optimizer_state = checkpoint.get("optimizer")
        if optimizer_state:
            optimizer.load_state_dict(optimizer_state)
        lr_scheduler = CustomCosineAnnealingWarmUpRestarts(optimizer, T_0=options.num_epochs, T_mult=1, eta_max=options.optimizer.lr, T_up=2, gamma=1.)
    else:
        optimizer = get_optimizer(
            options.optimizer.optimizer,
            params_to_optimise,
            lr=options.optimizer.lr,
            weight_decay=options.optimizer.weight_decay,
        )
        optimizer_state = checkpoint.get("optimizer")
        if optimizer_state:
            optimizer.load_state_dict(optimizer_state)
        if options.scheduler.scheduler == "ReduceLROnPlateau":
            lr_scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=options.schduler.patience)
        elif options.scheduler.scheduler == "StepLR":
            lr_scheduler = lr_scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=options.optimizer.lr_epochs, gamma=options.optimizer.lr_factor)
        elif options.scheduler.scheduler == "Cycle":
            for param_group in optimizer.param_groups:
                param_group["initial_lr"] = options.optimizer.lr
            cycle = len(train_data_loader) * options.num_epochs
            lr_scheduler = CircularLRBeta(
                optimizer, options.optimizer.lr, 10, 10, cycle, [0.95, 0.85]
            )


    # Log for W&B
    wandb.config.update(dict(options._asdict())) # logging to W&B

    # Log for tensorboard
    if not os.path.exists(options.prefix):
        os.makedirs(options.prefix)
    log_file = open(os.path.join(options.prefix, "log.txt"), "w")
    shutil.copy(config_file, os.path.join(options.prefix, "train_config.yaml"))
    if options.print_epochs is None:
        options.print_epochs = options.num_epochs
    writer = init_tensorboard(name=options.prefix.strip("-"))
    start_epoch = checkpoint["epoch"]
    train_symbol_accuracy = checkpoint["train_symbol_accuracy"]
    train_sentence_accuracy = checkpoint["train_sentence_accuracy"]
    train_wer = checkpoint["train_wer"]
    train_losses = checkpoint["train_losses"]
    validation_symbol_accuracy = checkpoint["validation_symbol_accuracy"]
    validation_sentence_accuracy = checkpoint["validation_sentence_accuracy"]
    validation_wer=checkpoint["validation_wer"]
    validation_losses = checkpoint["validation_losses"]
    learning_rates = checkpoint["lr"]
    grad_norms = checkpoint["grad_norm"]

    scaler = GradScaler()

    best_sentence_acc = 0.0

    # Train
    for epoch in range(options.num_epochs):
        start_time = time.time()

        epoch_text = "[{current:>{pad}}/{end}] Epoch {epoch}".format(
            current=epoch + 1,
            end=options.num_epochs,
            epoch=start_epoch + epoch + 1,
            pad=len(str(options.num_epochs)),
        )

        # Train
        # train_result = run_epoch(
        #     train_data_loader,
        #     model,
        #     epoch_text,
        #     criterion,
        #     optimizer,
        #     lr_scheduler,
        #     options.teacher_forcing_ratio,
        #     options.max_grad_norm,
        #     device,
        #     train=True,
        # )

        train_result = train_one_epoch(train_data_loader, model, epoch_text, criterion, optimizer, lr_scheduler, options.teacher_forcing_ratio, options.max_grad_norm, device, scaler)

        train_losses.append(train_result["loss"])
        grad_norms.append(train_result["grad_norm"])
        train_epoch_symbol_accuracy = (
            train_result["correct_symbols"] / train_result["total_symbols"]
        )
        train_symbol_accuracy.append(train_epoch_symbol_accuracy)
        train_epoch_sentence_accuracy = (
                train_result["sent_acc"] / train_result["num_sent_acc"]
        )

        train_sentence_accuracy.append(train_epoch_sentence_accuracy)
        train_epoch_wer = (
                train_result["wer"] / train_result["num_wer"]
        )
        train_wer.append(train_epoch_wer)
        ### 추가추가
        train_epoch_score = final_metric(sentence_acc=train_epoch_sentence_accuracy, word_error_rate=train_epoch_wer)
        epoch_lr = lr_scheduler.get_lr()  # cycle

        # Validation
        # validation_result = run_epoch(
        #     validation_data_loader,
        #     model,
        #     epoch_text,
        #     criterion,
        #     optimizer,
        #     lr_scheduler,
        #     options.teacher_forcing_ratio,
        #     options.max_grad_norm,
        #     device,
        #     train=False,
        # )

        validation_result = valid_one_epoch(validation_data_loader, model, epoch_text, criterion, device, teacher_forcing_ratio=options.teacher_forcing_ratio)

        validation_losses.append(validation_result["loss"])
        validation_epoch_symbol_accuracy = (
            validation_result["correct_symbols"] / validation_result["total_symbols"]
        )
        validation_symbol_accuracy.append(validation_epoch_symbol_accuracy)

        validation_epoch_sentence_accuracy = (
            validation_result["sent_acc"] / validation_result["num_sent_acc"]
        )
        validation_sentence_accuracy.append(validation_epoch_sentence_accuracy)
        validation_epoch_wer = (
                validation_result["wer"] / validation_result["num_wer"]
        )
        validation_wer.append(validation_epoch_wer)
        # 추가추가
        validation_epoch_score = final_metric(sentence_acc=validation_epoch_sentence_accuracy, word_error_rate=validation_epoch_wer)

        # Save checkpoint
        # make config
        with open(config_file, 'r') as f:
            option_dict = yaml.safe_load(f)
        if best_sentence_acc < 0.9*validation_epoch_sentence_accuracy + 0.1*(1-validation_epoch_wer):
            save_checkpoint(
                {
                    "epoch": start_epoch + epoch + 1,
                    "train_losses": train_losses,
                    "train_symbol_accuracy": train_symbol_accuracy,
                    "train_sentence_accuracy": train_sentence_accuracy,
                    "train_wer":train_wer,
                    "validation_losses": validation_losses,
                    "validation_symbol_accuracy": validation_symbol_accuracy,
                    "validation_sentence_accuracy":validation_sentence_accuracy,
                    "validation_wer":validation_wer,
                    "lr": epoch_lr,
                    "grad_norm": grad_norms,
                    "model": model.state_dict(),
                    "optimizer": optimizer.state_dict(),
                    "configs": option_dict,
                    "token_to_id":train_data_loader.dataset.token_to_id,
                    "id_to_token":train_data_loader.dataset.id_to_token,
                    "network": options.network
                },
                prefix=options.prefix,
            )
            best_sentence_acc = 0.9*validation_epoch_sentence_accuracy + 0.1*(1-validation_epoch_wer)
            print(f"best sentence acc: {best_sentence_acc}")
            print("model is saved")

        # Summary
        elapsed_time = time.time() - start_time
        elapsed_time = time.strftime("%H:%M:%S", time.gmtime(elapsed_time))
        if epoch % options.print_epochs == 0 or epoch == options.num_epochs - 1:
            output_string = (
                "{epoch_text}: "
                "Train Symbol Accuracy = {train_symbol_accuracy:.5f}, "
                "Train Sentence Accuracy = {train_sentence_accuracy:.5f}, "
                "Train WER = {train_wer:.5f}, "
                "Train Loss = {train_loss:.5f}, "
                "Validation Symbol Accuracy = {validation_symbol_accuracy:.5f}, "
                "Validation Sentence Accuracy = {validation_sentence_accuracy:.5f}, "
                "Validation WER = {validation_wer:.5f}, "
                "Validation Loss = {validation_loss:.5f}, "
                "lr = {lr} "
                "(time elapsed {time})"
            ).format(
                epoch_text=epoch_text,
                train_symbol_accuracy=train_epoch_symbol_accuracy,
                train_sentence_accuracy=train_epoch_sentence_accuracy,
                train_wer=train_epoch_wer,
                train_loss=train_result["loss"],
                validation_symbol_accuracy=validation_epoch_symbol_accuracy,
                validation_sentence_accuracy=validation_epoch_sentence_accuracy,
                validation_wer=validation_epoch_wer,
                validation_loss=validation_result["loss"],
                lr=epoch_lr,
                time=elapsed_time,
            )
            print(output_string)
            log_file.write(output_string + "\n")

            write_tensorboard(
                writer=writer,
                epoch=start_epoch+epoch +1,
                grad_norm=train_result["grad_norm"],
                train_loss=train_result["loss"],
                train_symbol_accuracy=train_epoch_symbol_accuracy,
                train_sentence_accuracy=train_epoch_sentence_accuracy,
                train_wer=train_epoch_wer,
                validation_loss=validation_result["loss"],
                validation_symbol_accuracy=validation_epoch_symbol_accuracy,
                validation_sentence_accuracy=validation_epoch_sentence_accuracy,
                validation_wer=validation_epoch_wer,
                model=model
            )
            write_wandb(
                epoch=start_epoch+epoch +1,
                grad_norm=train_result["grad_norm"],
                train_loss=train_result["loss"],
                train_symbol_accuracy=train_epoch_symbol_accuracy,
                train_sentence_accuracy=train_epoch_sentence_accuracy,
                train_wer=train_epoch_wer,
                train_score=train_epoch_score,
                validation_loss=validation_result["loss"],
                validation_symbol_accuracy=validation_epoch_symbol_accuracy,
                validation_sentence_accuracy=validation_epoch_sentence_accuracy,
                validation_wer=validation_epoch_wer,
                validation_score=validation_epoch_sentence_accuracy*0.9 + ((1-validation_epoch_wer)*0.1)
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--project_name',
        default='SATRN',
        help='W&B에 표시될 프로젝트명. 모델명으로 통일!'
    )
    parser.add_argument(
        '--exp_name',
        default='baseline-resume-ep21-iloveslowfood',
        help='실험명(SATRN-베이스라인, SARTN-Loss변경 등)'
    )
    parser.add_argument(
        "-c",
        "--config_file",
        dest="config_file",
        default="./configs/SATRN.yaml",
        type=str,
        help="Path of configuration file",
    )
    parser = parser.parse_args()

    # initilaize W&B
    run = wandb.init(project=parser.project_name, name=parser.exp_name)

    # train
    main(parser.config_file)

    # fishe W&B
    run.finish()

    
