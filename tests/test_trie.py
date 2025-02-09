import torch
import pytest
import asyncio
import numpy as np
from conftest import cuda_only
from arsenal.maths import compare
from transformers import AutoTokenizer

from genlm_backend.llm import MockAsyncLM
from genlm_backend.trie import (
    TokenCharacterTrie,
    ParallelTokenCharacterTrie,
    AsyncTokenCharacterTrie,
)


@pytest.fixture()
def decode():
    return [b"a", b"b", b"ab", b"<eos>"]


@pytest.fixture(scope="module")
def mock_llm():
    return MockAsyncLM(AutoTokenizer.from_pretrained("gpt2"))


def test_sequential_mass_sum(decode):
    trie = TokenCharacterTrie(decode=decode)
    haves = trie.mass_sum(torch.tensor([0.1, 0.2, 0.2, 0.5]))

    leaf_wants = {
        b"a": 0.1,
        b"b": 0.2,
        b"ab": 0.2,
        b"<eos>": 0.5,
    }
    internal_wants = {
        b"": 1,
        b"a": 0.3,
        b"b": 0.2,
        b"ab": 0.2,
        b"<": 0.5,
        b"<e": 0.5,
        b"<eo": 0.5,
        b"<eos": 0.5,
        b"<eos>": 0.5,
    }

    for node, prefix in trie.node2prefix.items():
        have = haves[node]
        if node in trie.leaf2word:
            want = leaf_wants[bytes(prefix)]
        else:
            want = internal_wants[bytes(prefix)]
        assert np.isclose(have, want, rtol=1e-5, atol=1e-8), [have, want, prefix]


def _test_mass_sum_agreement(decode, device):
    sequential_trie = TokenCharacterTrie(decode=decode)

    parallel_trie = ParallelTokenCharacterTrie(decode=decode, device=device)

    p_llms = torch.stack(
        [
            torch.tensor([0.1, 0.2, 0.2, 0.5]),
            torch.tensor([0, 0.3, 0.6, 0.1]),
            torch.tensor([0.99, 0.01, 0, 0]),
        ]
    ).to(device)

    parallel_masses = parallel_trie.batch_mass_sum(p_llms)
    sequential_masses = sequential_trie.batch_mass_sum(p_llms)

    assert len(parallel_masses) == len(sequential_masses)

    for have, want in zip(sequential_masses, parallel_masses):
        assert compare(have, want).max_rel_err <= 0.001


def test_mass_sum_agreement_cpu(decode):
    _test_mass_sum_agreement(decode, "cpu")


@cuda_only
def test_mass_sum_agreement_gpu(decode):
    _test_mass_sum_agreement(decode, "cuda")


def _test_async_trie(mock_llm, backend):
    async_trie = AsyncTokenCharacterTrie.from_vocab(
        mock_llm.byte_vocab, backend=backend
    )
    all_token_ids = [[0, 1, 3], [10, 20, 30], [8, 100]]
    p_llms = torch.exp(asyncio.run(mock_llm.batch_next_token_logprobs(all_token_ids)))

    async def async_trie_batch_mass_sum(p_llms):
        return await asyncio.gather(*[async_trie.mass_sum(p_llm) for p_llm in p_llms])

    haves = asyncio.run(async_trie_batch_mass_sum(p_llms))
    wants = async_trie.trie.batch_mass_sum(p_llms)

    assert len(haves) == len(wants)

    for have, want in zip(haves, wants):
        assert compare(have, want).max_rel_err <= 0.001, [have, want]


def test_async_trie_sequential(mock_llm):
    _test_async_trie(mock_llm, backend="sequential")


def test_async_trie_parallel(mock_llm):
    _test_async_trie(mock_llm, backend="parallel")
