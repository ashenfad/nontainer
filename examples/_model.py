"""Pick an LLM for the examples from whatever key is in the env."""

import os


def pick_model():
    if os.getenv("ANTHROPIC_API_KEY"):
        from agno.models.anthropic import Claude

        return Claude(id=os.getenv("NONTAINER_EXAMPLE_MODEL", "claude-sonnet-5"))
    if os.getenv("OPENAI_API_KEY"):
        from agno.models.openai import OpenAIChat

        return OpenAIChat(id=os.getenv("NONTAINER_EXAMPLE_MODEL", "gpt-5"))
    raise SystemExit(
        "Set ANTHROPIC_API_KEY or OPENAI_API_KEY to run the examples "
        "(and `pip install anthropic` / `pip install openai`)."
    )
