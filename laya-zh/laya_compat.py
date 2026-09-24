"""Local compatibility for Laya 0.3.4's single-option topk(2) bug.

Only tensor padding changes. The original question, option keys and model
forward remain intact; Agent.predict slices logits using the original option
count, so the masked padding is never exposed as a decision candidate.
"""


def _pad_single_option(_model, args):
    if len(args) < 4:
        return None
    positions, mask = args[2:4]
    if positions.ndim != 2 or positions.shape[1] != 1:
        return None

    import torch

    padded_positions = torch.cat((positions, torch.zeros_like(positions)), dim=1)
    padded_mask = torch.cat((mask, torch.zeros_like(mask)), dim=1)
    return args[:2] + (padded_positions, padded_mask) + args[4:]


def register_single_option_padding(model):
    """Install once on the loaded model, on either CPU or MPS."""
    handle = getattr(model, "_laya_single_option_padding_handle", None)
    if handle is None:
        handle = model.register_forward_pre_hook(_pad_single_option)
        model._laya_single_option_padding_handle = handle
    return handle
