# Copyright Lightning AI. Licensed under the Apache License 2.0, see LICENSE file.

def test_dolly(mock_tockenizer, dolly_path):
    from lit_gpt.data import Dolly
    from lit_gpt.prompts import Alpaca as AlpacaPromptStyle

    alpaca = Dolly(
        test_split_fraction=0.5,
        download_dir=dolly_path.parent,
        file_name=dolly_path.name,
        num_workers=0,
    )
    alpaca.connect(mock_tockenizer, batch_size=2, max_seq_length=10)
    alpaca.prepare_data()
    alpaca.setup()

    train_dataloader = alpaca.train_dataloader()
    val_dataloader = alpaca.val_dataloader()

    assert len(train_dataloader) == 3
    assert len(val_dataloader) == 3

    train_batch = next(iter(train_dataloader))
    val_batch = next(iter(val_dataloader))

    assert train_batch.keys() == val_batch.keys() == {"input_ids", "labels"}
    assert all(seq.shape == (2, 10) for seq in train_batch.values())
    assert all(seq.shape == (2, 10) for seq in val_batch.values())

    assert isinstance(train_dataloader.dataset.prompt_style, AlpacaPromptStyle)
    assert isinstance(val_dataloader.dataset.prompt_style, AlpacaPromptStyle)

