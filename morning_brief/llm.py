"""Shared Claude settings and text response handling."""

import os
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


DEFAULT_MODEL = "claude-sonnet-5-5"
TASK_EFFORTS = {"journal": "low", "brief": "medium"}
SUPPORTED_EFFORTS = {"low", "medium", "high"}


def call_claude(prompt, task, *, model=None, effort=None, max_tokens=4096):
    if task not in TASK_EFFORTS:
        raise ValueError(f"Unknown Claude task: {task}")
    model = model or os.getenv("CLAUDE_MODEL", DEFAULT_MODEL)
    effort = effort or os.getenv(f"CLAUDE_{task.upper()}_EFFORT", TASK_EFFORTS[task])
    if effort not in SUPPORTED_EFFORTS:
        raise ValueError("Claude effort must be low, medium or high")

    client = anthropic.Anthropic(timeout=60.0, max_retries=1)
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        output_config={"effort": effort},
        messages=[{"role": "user", "content": prompt}],
    )
    # Adaptive thinking can precede the answer. Never assume block zero is text.
    text = "\n".join(block.text for block in message.content if block.type == "text").strip()
    if message.stop_reason == "max_tokens":
        raise RuntimeError(f"Claude response was truncated ({model}, {effort}); no result applied")
    if not text:
        raise RuntimeError(f"Claude returned no answer text ({model}, {effort})")
    return {
        "text": text, "model": message.model, "effort": effort,
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
    }


def print_usage(result):
    print(f"  Claude: {result['model']} / {result['effort']} effort — "
          f"{result['input_tokens']} input + {result['output_tokens']} output tokens")
