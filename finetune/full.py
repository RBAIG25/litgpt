# Copyright Lightning AI. Licensed under the Apache License 2.0, see LICENSE file.
import dataclasses
import math
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import torch
from torch.utils.data import DataLoader

import lightning as L
from lightning.fabric.loggers import CSVLogger
from lightning.fabric.strategies import FSDPStrategy
from torchmetrics import RunningMean

# support running without installing as a package
wd = Path(__file__).parent.parent.resolve()
sys.path.append(str(wd))

from generate.base import generate
from lit_gpt.args import EvalArgs, IOArgs, TrainArgs
from lit_gpt.model import GPT, Block, Config
from lit_gpt.tokenizer import Tokenizer
from lit_gpt.data import Alpaca, LitDataModule, apply_prompt_template
from lit_gpt.utils import (
    CLI,
    check_valid_checkpoint_dir,
    chunked_cross_entropy,
    get_default_supported_precision,
    load_checkpoint,
    num_parameters,
    CycleIterator,
)


def setup(
    precision: Optional[str] = None,
    devices: int = 1,
    resume: Union[bool, Path] = False,
    seed: int = 1337,
    data: Optional[LitDataModule] = None,
    io: IOArgs = IOArgs(
        checkpoint_dir=Path("checkpoints/stabilityai/stablelm-base-alpha-3b"),
        out_dir=Path("out/full"),
    ),
    train: TrainArgs = TrainArgs(
        save_interval=1000,
        log_interval=1,
        global_batch_size=64,
        micro_batch_size=1,
        lr_warmup_steps=100,
        epochs=5,
        learning_rate=3e-3,
        max_seq_length=None,
    ),
    eval: EvalArgs = EvalArgs(interval=600, max_new_tokens=100, max_iters=100),
) -> None:

    print(locals())
    
    if data is None:
        data = Alpaca()

    precision = precision or get_default_supported_precision(training=True)

    if devices > 1:
        strategy = FSDPStrategy(
            auto_wrap_policy={Block},
            activation_checkpointing_policy={Block},
            state_dict_type="full",
            limit_all_gathers=True,
            cpu_offload=False,
        )
    else:
        strategy = "auto"

    logger = CSVLogger(io.out_dir.parent, io.out_dir.name, flush_logs_every_n_steps=train.log_interval)
    fabric = L.Fabric(devices=devices, strategy=strategy, precision=precision, loggers=logger)
    fabric.launch(main, devices, resume, seed, Config.from_name(name=io.checkpoint_dir.name), data, io, train, eval)


def main(
    fabric: L.Fabric,
    devices: int,
    resume: Union[bool, Path],
    seed: int,
    config: Config,
    data: LitDataModule,
    io: IOArgs,
    train: TrainArgs,
    eval: EvalArgs,
) -> None:
    validate_args(io, train, eval)
    check_valid_checkpoint_dir(io.checkpoint_dir)

    tokenizer = Tokenizer(io.checkpoint_dir)
    train_dataloader, val_dataloader = get_dataloaders(fabric, data, tokenizer, train)
    steps_per_epoch = len(train_dataloader) // train.gradient_accumulation_iters(devices)
    lr_max_steps = min(train.epochs * steps_per_epoch, (train.max_steps or float("inf")))

    fabric.seed_everything(seed)  # same seed for every process to init model (FSDP)

    if fabric.global_rank == 0:
        os.makedirs(io.out_dir, exist_ok=True)

    checkpoint_path = io.checkpoint_dir / "lit_model.pth"
    fabric.print(f"Loading model {str(checkpoint_path)!r} with {config.__dict__}")
    with fabric.init_module(empty_init=(devices > 1)):
        model = GPT(config)

    fabric.print(f"Number of trainable parameters: {num_parameters(model, requires_grad=True):,}")

    model = fabric.setup(model)
    optimizer = torch.optim.AdamW(
        model.parameters(), lr=train.learning_rate, weight_decay=train.weight_decay, betas=(train.beta1, train.beta2)
    )
    optimizer = fabric.setup_optimizers(optimizer)
    scheduler = get_lr_scheduler(optimizer, warmup_steps=train.lr_warmup_steps, max_steps=lr_max_steps)
    state = {"model": model, "optimizer": optimizer, "scheduler": scheduler, "iter_num": 0, "step_count": 0}

    if resume is True:
        resume = max(io.out_dir.glob("*.pth"), key=(lambda p: int(p.name.split("-")[1])))
    if resume:
        fabric.print(f"Resuming training from {resume}")
        fabric.load(resume, state)
    else:
        load_checkpoint(fabric, state["model"], checkpoint_path)

    train_time = time.perf_counter()
    fit(fabric, state, train_dataloader, val_dataloader, devices, resume, io, train, eval)
    fabric.print(f"Training time: {(time.perf_counter()-train_time):.2f}s")
    if fabric.device.type == "cuda":
        fabric.print(f"Memory used: {torch.cuda.max_memory_allocated() / 1e9:.02f} GB")

    # Save the final checkpoint at the end of training
    fabric.save(io.out_dir / "lit_model_finetuned.pth", {"model": state["model"]})


def fit(
    fabric: L.Fabric,
    state: Dict,
    train_dataloader: DataLoader,
    val_dataloader: DataLoader,
    devices: int,
    resume: Union[bool, Path],
    io: IOArgs,
    train: TrainArgs,
    eval: EvalArgs,
) -> None:
    model = state["model"]
    optimizer = state["optimizer"]
    scheduler = state["scheduler"]
    tokenizer = Tokenizer(io.checkpoint_dir)
    longest_seq_length, longest_seq_ix = get_longest_seq_length(train_dataloader.dataset)
    model.max_seq_length = min(longest_seq_length, train.max_seq_length or float("inf"))
    fabric.print(
        f"The longest sequence length in the train data is {longest_seq_length}, the model's maximum sequence length is"
        f" {model.max_seq_length} and context length is {model.config.block_size}"
    )

    validate(fabric, model, val_dataloader, tokenizer, dataclasses.replace(eval, max_iters=2))  # sanity check
    initial_iter = state["iter_num"]
    train_iterator = CycleIterator(train_dataloader)

    # resume data loader state by fast-forwarding through all seen batches
    if resume:
        resume_t0 = time.perf_counter()
        for resume_iter in range(initial_iter):
            next(train_iterator)
            if resume_iter % 1000 == 0:
                fabric.print(f"Resuming dataset: {resume_iter} / {initial_iter}")
        fabric.barrier()
        fabric.print(
            f"Resuming data loader finished. Took {time.perf_counter() - resume_t0:.1f} seconds to reach iteration"
            f" {initial_iter}."
        )

    running_loss = RunningMean(window=train.gradient_accumulation_iters(devices), sync_on_compute=False).to(
        fabric.device
    )
    fabric.barrier()

    while state["step_count"] < (train.max_steps or float("inf")) and train_iterator.epoch < train.epochs:
        state["iter_num"] += 1
        iter_t0 = time.perf_counter()
        batch = next(train_iterator)
        input_ids, targets = batch["input_ids"], batch["labels"]

        is_accumulating = state["iter_num"] % train.gradient_accumulation_iters(devices) != 0
        with fabric.no_backward_sync(model, enabled=is_accumulating):
            logits = model(input_ids)
            # shift the targets such that output n predicts token n+1
            loss = chunked_cross_entropy(logits[..., :-1, :], targets[..., 1:])
            fabric.backward(loss / train.gradient_accumulation_iters(devices))

        running_loss.update(loss.detach())

        if not is_accumulating:
            optimizer.step()
            optimizer.zero_grad()
            scheduler.step()
            state["step_count"] += 1

        if state["iter_num"] % train.log_interval == 0:
            loss = running_loss.compute().item()  # expensive device-to-host synchronization
            t1 = time.perf_counter()
            metrics = {
                "loss": loss,
                "iter": state["iter_num"],
                "step": state["step_count"],
                "iter_time": t1 - iter_t0,
                "tokens": state["iter_num"] * train.micro_batch_size * model.config.block_size,
                "total_tokens": (
                    state["iter_num"] * train.micro_batch_size * model.config.block_size * fabric.world_size
                ),
                # TODO: log learning rate
            }
            fabric.print(
                f"iter {metrics['iter']} | step {metrics['step']}: loss {metrics['loss']:.4f}, iter time:"
                f" {metrics['iter_time'] * 1000:.2f} ms{' (optimizer.step)' if not is_accumulating else ''}"
            )
            fabric.log_dict(metrics, step=state["iter_num"])

        if not is_accumulating and state["step_count"] % eval.interval == 0:
            t0 = time.perf_counter()
            val_loss = validate(fabric, model, val_dataloader, tokenizer, eval)
            t1 = time.perf_counter() - t0
            fabric.print(f"iter {state['iter_num']}: val loss {val_loss.item():.4f}, val time: {t1 * 1000:.2f} ms")
            metrics = {"val_loss": val_loss, "val_ppl": math.exp(val_loss)}
            fabric.log_dict(metrics, step=state["iter_num"])
            fabric.barrier()
        if not is_accumulating and state["step_count"] % train.save_interval == 0:
            checkpoint_path = io.out_dir / f"step-{state['step_count']:06d}.pth"
            fabric.print(f"Saving checkpoint to {str(checkpoint_path)!r}")
            fabric.save(checkpoint_path, state)


# FSDP has issues with `inference_mode`
@torch.no_grad()
def validate(
    fabric: L.Fabric, model: GPT, val_dataloader: DataLoader, tokenizer: Tokenizer, eval: EvalArgs
) -> torch.Tensor:
    fabric.print("Validating ...")
    model.eval()
    losses = torch.zeros(eval.max_iters)
    val_iterator = iter(val_dataloader)
    for k in range(eval.max_iters):
        batch = next(val_iterator)
        input_ids, targets = batch["input_ids"], batch["labels"]
        logits = model(input_ids)
        losses[k] = chunked_cross_entropy(logits[..., :-1, :], targets[..., 1:], chunk_size=0)
    val_loss = losses.mean()

    # produce an example:
    # TODO: Use a sample from the dataset to avoid mismatching keys for prompt templates
    instruction = "Recommend a movie for me to watch during the weekend and explain the reason."
    fabric.print(instruction)
    prompt = apply_prompt_template(val_dataloader.dataset.prompt_template, {"instruction": instruction})
    encoded = tokenizer.encode(prompt, device=fabric.device)
    with fabric.init_tensor():
        # do not set `max_seq_length=max_returned_token` because memory is not a concern here
        model.set_kv_cache(batch_size=1)
    output = generate(
        model, encoded, max_returned_tokens=len(encoded) + eval.max_new_tokens, temperature=0.8, eos_id=tokenizer.eos_id
    )
    model.clear_kv_cache()
    output = tokenizer.decode(output)
    fabric.print(output)

    model.train()
    return val_loss


def get_lr_scheduler(optimizer, warmup_steps: int, max_steps: int):
    # linear warmup followed by cosine annealing
    scheduler1 = torch.optim.lr_scheduler.LambdaLR(optimizer, lambda step: step / warmup_steps)
    scheduler2 = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=(max_steps - warmup_steps))
    return torch.optim.lr_scheduler.SequentialLR(optimizer, [scheduler1, scheduler2], milestones=[warmup_steps])


def get_dataloaders(fabric: L.Fabric, data: LitDataModule, tokenizer: Tokenizer, train: TrainArgs) -> Tuple[DataLoader, DataLoader]:
    data.connect(tokenizer=tokenizer, batch_size=train.micro_batch_size, max_seq_length=train.max_seq_length)
    with fabric.rank_zero_first():
        data.prepare_data()
    data.setup()
    train_dataloader = data.train_dataloader()
    val_dataloader = data.val_dataloader()
    train_dataloader, val_dataloader = fabric.setup_dataloaders(train_dataloader, val_dataloader)
    return train_dataloader, val_dataloader


def get_longest_seq_length(data: List[Dict]) -> Tuple[int, int]:
    # find out the minimum max_seq_length required during fine-tuning (saves memory!)
    lengths = [len(d["input_ids"]) for d in data]
    longest_seq_length = max(lengths)
    longest_seq_ix = lengths.index(longest_seq_length)
    return longest_seq_length, longest_seq_ix


def validate_args(io: IOArgs, train: TrainArgs, eval: EvalArgs) -> None:
    issues = []
    unsupported = [(train, ["max_tokens", "max_norm"])]
    for args, names in unsupported:
        for name in names:
            if getattr(args, name) is not None:
                issues.append(f"{__file__} doesn't support the {name!r} argument. This is set in {args}")
    required = [
        (io, ["checkpoint_dir"]),
        (train, ["epochs"]),
        (eval, ["max_new_tokens"]),
    ]
    for args, names in required:
        for name in names:
            if getattr(args, name) is None:
                issues.append(f"{__file__} requires the {name!r} argument. This is set in {args}")
    if issues:
        raise ValueError("\n".join(issues))


if __name__ == "__main__":
    torch.set_float32_matmul_precision("high")

    CLI(setup)
