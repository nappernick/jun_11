"""
Load the candidate prompts under test — VERBATIM, exactly as authored on disk.

The prompts live as ``*.txt`` files in :data:`config.PROMPT_BENCH_PROMPTS_DIR`
(``bakeoff/promptbench/prompts/``). There is NO adaptation, NO placeholder filling, and NO
iteration here: each file is scored byte-for-byte as written. The filename stem is the
prompt key (and its upper-cased label), so whatever files are present — ``a.txt``, ``b.txt``,
… — are exactly the set tested, in sorted filename order.
"""
from __future__ import annotations

from dataclasses import dataclass

from bakeoff import config

__all__ = ["PromptSpec", "load_prompts"]


@dataclass(frozen=True)
class PromptSpec:
    key: str
    label: str
    text: str


def load_prompts() -> list[PromptSpec]:
    """Return every ``*.txt`` prompt in the prompts dir, verbatim, in sorted-name order.

    The file's stem is the key; its upper-cased stem is the label; its full contents are the
    prompt text, unmodified. Empty/whitespace-only files are skipped. Raises if the directory
    has no usable prompt files (nothing to score).
    """
    prompts_dir = config.PROMPT_BENCH_PROMPTS_DIR
    files = sorted(prompts_dir.glob("*.txt"), key=lambda p: p.name)
    specs: list[PromptSpec] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            continue
        specs.append(PromptSpec(key=path.stem, label=path.stem.upper(), text=text))
    if not specs:
        raise FileNotFoundError(
            f"No prompt .txt files to score in {prompts_dir}. Add the candidate prompts there."
        )
    return specs
