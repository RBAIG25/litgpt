import os
import sys
from pathlib import Path

import pytest
from transformers import AutoTokenizer
from transformers.utils import cached_file

# support running without installing as a package
wd = Path(__file__).parent.parent.resolve()
sys.path.append(str(wd))

import lit_gpt.config as config_module


@pytest.mark.parametrize("config", config_module.configs, ids=[c["name"] for c in config_module.configs])
def test_tokenizer_against_hf(config):
    from lit_gpt.tokenizer import Tokenizer

    access_token = os.getenv("HF_TOKEN")

    config = config_module.Config(**config)

    repo_id = f"{config.org}/{config.name}"
    cache_dir = Path("/tmp/tokenizer_test_cache")

    # create a checkpoint directory that points to the HF files
    checkpoint_dir = cache_dir / "ligpt" / config.org / config.name
    if not checkpoint_dir.exists():
        file_to_cache = {}
        for file in ("tokenizer.json", "generation_config.json", "tokenizer.model", "tokenizer_config.json"):
            try:
                # download the HF tokenizer config
                hf_file = cached_file(repo_id, file, cache_dir=cache_dir / "hf", token=access_token)
            except OSError as e:
                if "gated repo" in str(e) and not access_token:
                    pytest.xfail("Gated repo")
                if "does not appear to have" in str(e):
                    continue
                raise e
            file_to_cache[file] = str(hf_file)
        checkpoint_dir.mkdir(parents=True)
        for file, hf_file in file_to_cache.items():
            (checkpoint_dir / file).symlink_to(hf_file)

    theirs = AutoTokenizer.from_pretrained(
        repo_id, cache_dir=cache_dir / "hf", local_files_only=True, token=access_token
    )
    ours = Tokenizer(checkpoint_dir)

    assert ours.vocab_size == theirs.vocab_size
    assert ours.vocab_size == config.vocab_size
    assert ours.bos_id == theirs.bos_token_id
    assert ours.eos_id == theirs.eos_token_id

    prompt = "Hello, readers of this test!"
    actual = ours.encode(prompt)
    expected = theirs.encode(prompt)
    assert actual.tolist() == expected
    assert ours.decode(actual) == theirs.decode(actual)
