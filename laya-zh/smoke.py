"""Run a Chinese single-model Laya inference smoke test.

The script deliberately requests MPS first. If the local PyTorch build or a
model operation cannot run on MPS, it records the exception and retries on CPU.
The model is loaded from this deployment directory and needs no network access.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any


DEPLOY_DIR = Path(__file__).resolve().parent
MODEL_DIR = DEPLOY_DIR / "model"


def configure_environment() -> None:
    """Select the PyTorch backend without enabling optional frameworks."""

    os.environ["USE_TF"] = "0"
    os.environ["USE_TORCH"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"


def run_once(device: str) -> tuple[dict[str, Any], str]:
    """Load only the multilingual checkpoint and evaluate Chinese questions."""

    import laya

    if not (MODEL_DIR / "model.safetensors").is_file():
        raise FileNotFoundError(f"模型尚未下载完整：{MODEL_DIR}")
    agent = laya.load(str(MODEL_DIR), device=device)
    questions = {
        "department": {
            "type": "choice",
            "instructions": "这条客户消息应该由哪个部门处理？",
            "criteria": {
                "账单": "付款、发票、重复扣款或退款",
                "技术支持": "登录、功能故障或产品使用问题",
                "销售": "购买咨询、报价或套餐问题",
            },
        },
        "urgency": {
            "type": "score",
            "instructions": "这条请求有多紧急？",
            "criteria": ["不紧急", "需要尽快处理", "关键业务被阻断"],
        },
        "refund_requested": {
            "type": "noul",
            "instructions": "客户是否明确要求退款？",
        },
    }
    state = {
        "message": "我被重复收取了两次费用，请尽快退还其中一笔。",
    }
    result = agent.predict(state, questions)
    return result, str(agent.device)


def main() -> int:
    configure_environment()

    mps_error: str | None = None
    requested_device = "mps"
    try:
        result, actual_device = run_once(requested_device)
    except Exception:
        mps_error = traceback.format_exc()
        result, actual_device = run_once("cpu")

    payload = {
        "status": "ok",
        "model_repo": "convaiinnovations/laya-multilingual",
        "model_path": str(MODEL_DIR),
        "requested_device": requested_device,
        "actual_device": actual_device,
        "mps_error": mps_error,
        "answers": result["answers"],
        "usage": result.get("usage"),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
