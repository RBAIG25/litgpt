# Copyright Lightning AI. Licensed under the Apache License 2.0, see LICENSE file.
"""Implementation derived from https://github.com/tloen/alpaca-lora"""
import os
from functools import partial

import sys
from pathlib import Path
from typing import Optional, Union, List

import torch
from torch.utils.data import random_split, DataLoader
from lightning import LightningDataModule
from lit_gpt.datasets.alpaca import prompt_template
from lit_gpt.datasets.base import SFTDataset, sft_collate_fn

# support running without installing as a package
wd = Path(__file__).parent.parent.resolve()
sys.path.append(str(wd))

from lit_gpt.tokenizer import Tokenizer


class LIMA(LightningDataModule):
    """LIMA data module for supervised finetuning.

    Provides train- and val-dataloaders. The batches return keys "input_ids" and "labels".
    """

    def __init__(
        self,
        tokenizer_or_path: Union[str, Path, Tokenizer],
        max_seq_length: int = -1,
        mask_prompt: bool = True,
        test_split_fraction: float = 0.1,
        ignore_index: int = -1,
        seed: int = 42,
        include_multiturn_conversations: bool = False,
        data_repo_id: str = "GAIR/lima",
        access_token: Optional[str] = os.getenv("HF_TOKEN"),
        batch_size: int = 1,
        num_workers: int = 4,
    ) -> None:
        super().__init__()
        if access_token is None:
            raise ValueError(
                "LIMA requires authentication, please set the `HF_TOKEN=your_token` environment"
                " variable or pass --access_token=your_token. You can find your token by visiting"
                " https://huggingface.co/settings/tokens"
            )

        if isinstance(tokenizer_or_path, (str, Path)):
            self.tokenizer = Tokenizer(Path(tokenizer_or_path))
        else:
            self.tokenizer = tokenizer_or_path

        self.max_seq_length = max_seq_length
        self.mask_prompt = mask_prompt
        self.test_split_fraction = test_split_fraction
        self.ignore_index = ignore_index
        self.seed = seed
        self.batch_size = batch_size
        self.num_workers = num_workers

        self.access_token = access_token
        self.data_repo_id = data_repo_id
        self.include_multiturn_conversations = include_multiturn_conversations
        self.train_dataset: Optional[SFTDataset] = None
        self.test_dataset: Optional[SFTDataset] = None

    def prepare_data(self) -> None:
        from datasets import load_dataset

        load_dataset(self.data_repo_id, token=self.access_token)

    def setup(self, stage: str = None) -> None:
        from datasets import load_dataset

        dataset = load_dataset(self.data_repo_id, token=self.access_token)
        data = format_dataset(dataset["train"], self.include_multiturn_conversations)

        # Partition the dataset into train and test
        train_data, test_data = random_split(
            data,
            [1.0 - self.test_split_fraction, self.test_split_fraction],
            generator=torch.Generator().manual_seed(self.seed)
        )
        train_data, test_data = list(train_data), list(test_data)

        self.train_dataset = SFTDataset(
            data=train_data,
            tokenizer=self.tokenizer,
            prompt_template=prompt_template,
            max_seq_length=self.max_seq_length,
            mask_prompt=self.mask_prompt,
            ignore_index=self.ignore_index,
        )
        self.test_dataset = SFTDataset(
            data=test_data,
            tokenizer=self.tokenizer,
            prompt_template=prompt_template,
            max_seq_length=self.max_seq_length,
            mask_prompt=self.mask_prompt,
            ignore_index=self.ignore_index,
        )

    def train_dataloader(self) -> DataLoader:
        return DataLoader(
            self.train_dataset,
            batch_size=self.batch_size,
            shuffle=True,
            num_workers=self.num_workers,
            collate_fn=partial(sft_collate_fn, max_seq_length=self.max_seq_length, ignore_index=self.ignore_index)
        )

    def val_dataloader(self) -> DataLoader:
        return DataLoader(
            self.test_dataset,
            batch_size=self.batch_size,
            shuffle=False,
            num_workers=self.num_workers,
            collate_fn=partial(sft_collate_fn, max_seq_length=self.max_seq_length, ignore_index=self.ignore_index)
        )

    def test_dataloader(self) -> DataLoader:
        return self.val_dataloader()


def format_dataset(dataset_partition: dict, include_multi_turn_conversations: bool) -> List[dict]:
    formatted_ds = []

    for entry in dataset_partition:
        convo = entry["conversations"]
        if include_multi_turn_conversations:
            for i in range(0, len(convo) - 1, 2):
                formatted_ds.append({"instruction": convo[i], "input": "", "output": convo[i + 1]})
        else:
            formatted_ds.append({"instruction": convo[0], "input": "", "output": convo[1]})

    return formatted_ds


if __name__ == "__main__":
    alpaca = LIMA(
        tokenizer_or_path="checkpoints/TinyLlama/TinyLlama-1.1B-Chat-v1.0",
        batch_size=4,
    )
    alpaca.prepare_data()
    alpaca.setup()

    train_dataloader = alpaca.train_dataloader()
    for batch in train_dataloader:
        print(batch)