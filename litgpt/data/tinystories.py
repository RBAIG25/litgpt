# Copyright Lightning AI. Licensed under the Apache License 2.0, see LICENSE file.
import glob
import json
import os
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Optional

from torch.utils.data import DataLoader
from tqdm import tqdm

from litgpt import Tokenizer
from litgpt.data import DataModule
from litgpt.data.alpaca import download_if_missing

_URL = "https://huggingface.co/datasets/roneneldan/TinyStories/resolve/main/TinyStories_all_data.tar.gz"


@dataclass
class TinyStories(DataModule):
    """The TinyStories data module: https://huggingface.co/datasets/roneneldan/TinyStories

    Provides training and validation dataloaders that return batches of tokens. Every sample is set to a fixed length.
    """

    data_path: Path = Path("data/tinystories")
    """The path to the data directory, containing two folders 'train' and 'val'
    which are the output of the preprocessing step. The path can also be a remote path (e.g., s3://)."""
    seed: int = 42
    """The seed to use for shuffling the dataset."""
    num_workers: int = 8
    """The number of workers to use for the dataloaders."""

    tokenizer: Optional[Tokenizer] = field(default=None, init=False, repr=False)
    batch_size: int = field(default=1, init=False, repr=False)
    max_seq_length: int = field(default=-1, init=False, repr=False)

    def __post_init__(self) -> None:
        # Could be a remote path (s3://) or a local path
        self.data_path_train = str(self.data_path).rstrip("/") + "/train"
        self.data_path_val = str(self.data_path).rstrip("/") + "/val"

    def connect(
        self, tokenizer: Optional[Tokenizer] = None, batch_size: int = 1, max_seq_length: int = -1
    ) -> None:
        self.tokenizer = tokenizer
        self.batch_size = batch_size
        self.max_seq_length = max_seq_length + 1  # Increase by one because we need the next token as well

    def prepare_data(self) -> None:
        from litdata import optimize

        download(self.data_path)

        files = sorted(glob.glob(str(self.data_path / "TinyStories_all_data" / "*.json")))
        assert len(files) > 0, f"No json files found in {files}"
        assert len(files) > 1, f"Expected at least two json files in {files}"
        # train/test split. let's use only shard 0 for test split, rest train
        val_files, *train_files = files
        num_workers = os.cpu_count() - 1

        if not Path(self.data_path_train).is_dir():
            optimize(
                fn=partial(tokenize, tokenizer=self.tokenizer),
                inputs=train_files,
                output_dir=self.data_path_train,
                num_workers=num_workers,
                chunk_bytes="200MB",
            )
        if not Path(self.data_path_val).is_dir():
            optimize(
                fn=partial(tokenize, tokenizer=self.tokenizer),
                inputs=val_files,
                output_dir=self.data_path_val,
                num_workers=num_workers,
                chunk_bytes="200MB",
            )

    def train_dataloader(self) -> DataLoader:
        from litdata.streaming import StreamingDataLoader, StreamingDataset, TokensLoader

        train_dataset = StreamingDataset(
            input_dir=self.data_path_train,
            item_loader=TokensLoader(block_size=self.max_seq_length),
            shuffle=True,
            drop_last=True,
        )
        train_dataloader = StreamingDataLoader(
            train_dataset, batch_size=self.batch_size, pin_memory=True, num_workers=self.num_workers, drop_last=True
        )
        return train_dataloader

    def val_dataloader(self) -> DataLoader:
        from litdata.streaming import StreamingDataset, TokensLoader

        val_dataset = StreamingDataset(
            input_dir=self.data_path_val,
            item_loader=TokensLoader(block_size=self.max_seq_length),
            shuffle=True,
            # Consider setting to False, but we would lose some samples due to truncation when world size > 1
            drop_last=True,
        )
        val_dataloader = DataLoader(
            val_dataset, batch_size=self.batch_size, pin_memory=True, num_workers=self.num_workers, drop_last=True
        )
        return val_dataloader


def tokenize(filename: str, tokenizer: Tokenizer):
    with open(filename, "r") as f:
        data = json.load(f)
    global_rank = int(os.environ["DATA_OPTIMIZER_GLOBAL_RANK"])
    num_workers = int(os.environ["DATA_OPTIMIZER_NUM_WORKERS"])
    local_rank = global_rank % num_workers
    for example in tqdm(data, position=local_rank):
        text = example["story"]
        text = text.strip()  # get rid of leading/trailing whitespace
        tokens = tokenizer.encode(text, bos=True, eos=False)  # encode the text, use BOS
        yield tokens


def download(data_dir: Path):
    data_dir.mkdir(exist_ok=True)

    data_dir = data_dir / "TinyStories_all_data"
    shard_filenames = sorted(glob.glob(str(data_dir / "*.json")))
    if shard_filenames:
        print(f"{data_dir} already exists, skipping unpacking...")
        return

    # download the TinyStories dataset, unless it's already downloaded
    data_filename = data_dir / "TinyStories_all_data.tar.gz"
    download_if_missing(data_filename, _URL, stream=True, mode="wb")
    print("Download done.")

    # unpack the tar.gz file into all the data shards (json files)
    data_dir.mkdir(exist_ok=True)
    print(f"Unpacking {data_filename}...")
    os.system(f"tar -xzf {data_filename} -C {data_dir}")
    shard_filenames = sorted(glob.glob(str(data_dir / "*.json")))
    print(f"Number of shards: {len(shard_filenames)}")
