# Copyright Lightning AI. Licensed under the Apache License 2.0, see LICENSE file.

import logging
import re

from lightning_utilities.core.imports import RequirementCache

from litgpt.api import LLM
from litgpt.model import GPT  # needs to be imported before config
from litgpt.config import Config
from litgpt.prompts import PromptStyle
from litgpt.tokenizer import Tokenizer
from litgpt.utils import has_h100_or_h800


# Suppress excessive warnings, see https://github.com/pytorch/pytorch/issues/111632
pattern = re.compile(".*Profiler function .* will be ignored")
logging.getLogger("torch._dynamo.variables.torch").addFilter(lambda record: not pattern.search(record.getMessage()))

# Avoid printing state-dict profiling output at the WARNING level when saving a checkpoint
logging.getLogger("torch.distributed.fsdp._optim_utils").disabled = True
logging.getLogger("torch.distributed.fsdp._debug_utils").disabled = True

if bool(RequirementCache("flash-attn>=2.6.1")) and has_h100_or_h800():
    print("flash-attn package and Hopper GPU detected: LitGPT will use FlashAttention v3.")

__all__ = ["LLM", "GPT", "Config", "PromptStyle", "Tokenizer"]
