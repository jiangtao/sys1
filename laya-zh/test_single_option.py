"""Exercise the installed SDK forward without loading the real checkpoint."""

import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_TORCH", "1")

import torch
from laya.common import DecisionModel, confidence_from_probs

from laya_compat import register_single_option_padding


class TinyEncoder(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.config = SimpleNamespace(hidden_size=64)
        self.embedding = torch.nn.Embedding(8, 64)

    def forward(self, input_ids, attention_mask):
        return SimpleNamespace(last_hidden_state=self.embedding(input_ids))


class SingleOptionRegression(unittest.TestCase):
    def setUp(self):
        torch.manual_seed(12)
        self.model = DecisionModel(TinyEncoder(), 0, 2).eval()

    def inputs(self, count):
        return (
            torch.tensor([[1, 2, 3]]), torch.ones(1, 3, dtype=torch.long),
            torch.arange(count).reshape(1, count),
            torch.ones(1, count, dtype=torch.bool), torch.zeros(1, dtype=torch.long),
        )

    def test_single_option_uses_real_forward_and_hides_padding(self):
        inputs = self.inputs(1)
        with self.assertRaisesRegex(RuntimeError, "selected index k out of range"):
            self.model(*inputs)
        register_single_option_padding(self.model)
        with torch.no_grad():
            logits, actions = self.model(*inputs)
        self.assertEqual(logits.shape, (1, 2))
        self.assertEqual(logits[0, 1].item(), -10000)
        # Agent.predict keeps the original k=1 and never returns the masked slot.
        probs = torch.softmax(logits[0, :1], dim=-1).numpy()
        self.assertEqual(probs.tolist(), [1.0])
        self.assertEqual(confidence_from_probs(probs, 1), 1.0)
        self.assertTrue(torch.isfinite(actions).all())
        self.assertEqual(inputs[2].shape, (1, 1))
        self.assertEqual(inputs[3].tolist(), [[True]])

    def test_multiple_options_are_unchanged_and_hook_is_idempotent(self):
        inputs = self.inputs(3)
        with torch.no_grad():
            before = self.model(*inputs)
            first = register_single_option_padding(self.model)
            self.assertIs(first, register_single_option_padding(self.model))
            after = self.model(*inputs)
        for original, patched in zip(before, after):
            torch.testing.assert_close(original, patched, rtol=0, atol=0)


if __name__ == "__main__":
    unittest.main()
