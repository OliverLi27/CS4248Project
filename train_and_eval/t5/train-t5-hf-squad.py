import os, sys, gc
import json
import random
import numpy as np
import argparse
import logging
from tqdm import tqdm

import torch
from torch.utils.data import Dataset, DataLoader
from torch.nn.utils import clip_grad_norm_

import utils

from datasets import load_dataset
from dataclasses import dataclass, field
from typing import Dict, Optional, Sequence
from transformers import (
    T5Tokenizer, T5ForConditionalGeneration,
    AutoConfig, get_linear_schedule_with_warmup,
    Trainer
)
import transformers

MODEL_CACHE_DIR = "./models"
DATASET_CACHE_DIR = "./data"


def set_seed(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    # Set a fixed value for the hash seed
    os.environ['PYTHONHASHSEED'] = str(seed)


class ConditionalGenerationDataset(Dataset):

    def __init__(self, data_path):
        list_data_dict = utils.jload(data_path)
        self.inputs = []
        self.labels = []

        for data_details in list_data_dict:
            inputs = data_details["instruction"] + "\n" + data_details["input"]
            labels = data_details["output"]
            inputs = inputs.strip()
            labels = labels.strip()
            self.inputs.append(inputs)
            self.labels.append(labels)
    
    def __len__(self):
        return len(self.inputs)

    def __getitem__(self, i) -> Dict[str, torch.Tensor]:
        return dict(input_ids=self.inputs[i], labels=self.labels[i])


@dataclass
class ModelArguments:
    model_name_or_path: Optional[str] = field(default="t5-base")


@dataclass
class DataArguments:
    data_path: str = field(default=None, metadata={"help": "Path to the training data."})


@dataclass
class TrainingArguments(transformers.TrainingArguments):
    optim: str = field(default="adamw_torch")
    model_max_length: int = field(
        default=1024,
        metadata={"help": "Maximum sequence length. Sequences will be right padded (and possibly truncated)."},
    )


@dataclass
class DataCollatorForSupervisedDataset(object):
    """Collate examples for supervised fine-tuning."""

    tokenizer: transformers.PreTrainedTokenizer

    def __call__(self, instances):
        inputs, labels = tuple([instance[key] for instance in instances] for key in ("input_ids", "labels"))
        encoding = tokenizer(inputs, padding="longest", max_length=1024, truncation=True, return_tensors="pt")
        input_ids = encoding.input_ids
        attention_mask = encoding.attention_mask
        labels = tokenizer(labels, padding="longest", max_length=100, truncation=True, return_tensors="pt").input_ids
        # print(labels)
        return dict(
            input_ids=input_ids,
            labels=labels,
            attention_mask=attention_mask,
        )


def make_supervised_data_module(tokenizer: transformers.PreTrainedTokenizer):
    train_dataset = ConditionalGenerationDataset(data_path="squad/Preprocessed_train-v1.1.json")
    data_collator = DataCollatorForSupervisedDataset(tokenizer=tokenizer)
    return dict(train_dataset=train_dataset, eval_dataset=None, data_collator=data_collator)


def smart_tokenizer_and_embedding_resize(
    special_tokens_dict: Dict,
    tokenizer: transformers.PreTrainedTokenizer,
    model: transformers.PreTrainedModel,
):
    """Resize tokenizer and embedding.

    Note: This is the unoptimized version that may make your embedding size not be divisible by 64.
    """
    num_new_tokens = tokenizer.add_special_tokens(special_tokens_dict)
    model.resize_token_embeddings(len(tokenizer))

    if num_new_tokens > 0:
        input_embeddings = model.get_input_embeddings().weight.data
        output_embeddings = model.get_output_embeddings().weight.data

        input_embeddings_avg = input_embeddings[:-num_new_tokens].mean(dim=0, keepdim=True)
        output_embeddings_avg = output_embeddings[:-num_new_tokens].mean(dim=0, keepdim=True)

        input_embeddings[-num_new_tokens:] = input_embeddings_avg
        output_embeddings[-num_new_tokens:] = output_embeddings_avg


if __name__ == "__main__":
    parser = transformers.HfArgumentParser((ModelArguments, DataArguments, TrainingArguments))
    model_args, data_args, training_args = parser.parse_args_into_dataclasses()

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    tokenizer = T5Tokenizer.from_pretrained(model_args.model_name_or_path)
    model = T5ForConditionalGeneration.from_pretrained(model_args.model_name_or_path, max_length=1024).to(device)
    print("Model {model_args.model_name_or_path} loaded.")

    vocab_size = len(tokenizer.get_vocab())
    print("vocab size: " + str(vocab_size))
    print("Generation Task")

    data_module = make_supervised_data_module(tokenizer=tokenizer)
    trainer = Trainer(model=model, tokenizer=tokenizer, args=training_args, **data_module)
    trainer.train()
    trainer.save_state()
    trainer.save_model(output_dir=training_args.output_dir)