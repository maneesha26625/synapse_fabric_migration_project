"""LLM-assisted transformation of objects the rules cannot fully convert.

Opt-in and per object: a person asks for a draft, reviews it next to the source and the rule-based
output, and accepts it. Only an accepted draft whose source has not changed since is used by a
migration run. Everything else keeps the deterministic path.

The model is reached through the Claude Code CLI on the machine running the API (``claude -p``),
signed in the way that CLI already is. No API key passes through the accelerator.
"""
