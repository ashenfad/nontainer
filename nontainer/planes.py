"""The reserved key prefixes a session's state is partitioned into.

A session's branch holds one flat mapping: the files (under monkeyfs's
own prefix), the agent's cache, the stored conversation, and the
framework's small bookkeeping keys. Which prefix a key falls under is
what decides how a merge treats it — files three-way, every other
plane ours — so the prefixes are named once here rather than in each
plane's own module, and the merge policy reads as the table it is.

Each plane still owns its behaviour where it lives (``cache.py``, the
agno db adapter, ``agentgit.py``); this module owns only the names,
which is what lets core code state the policy without importing an
optional extra.
"""

from __future__ import annotations

CACHE_PREFIX = "__cache__/"
"""The agent's own persistent dict (``ws.cache``). Session-scoped by
construction: a delegate's cache is its own working memory and is
never merged back."""

CONVERSATION_PREFIX = "__conversation__/"
"""The stored conversation, in any harness's format (see
:mod:`nontainer.conversation`): a core-owned index, the harness's own
session record, and one key per run. A delegate's conversation never
merges into its caller's: two chats that never happened together cannot
be interleaved, and what a delegate has to say arrives as its answer."""

CONVERSATION_INDEX_KEY = CONVERSATION_PREFIX + "index"
"""Core's index of the conversation: which harness wrote it, the session
it belongs to, its runs in order, and the session it was forked from.
Forking, deleting and reading run lineage need nothing else, so core
never parses a harness's own record."""

CONVERSATION_RECORD_KEY = CONVERSATION_PREFIX + "record"
"""The harness's own session record, opaque to core."""

CONVERSATION_RUN_PREFIX = CONVERSATION_PREFIX + "runs/"
"""One key per run, in the harness's own format."""

LEGACY_CONVERSATION_PREFIX = "__agno__/"
"""Where the conversation lived before the plane was harness-neutral:
the agno adapter's session record and runs. Still read when a head has
no index (a session never written since, or a checkout or fork of an
older commit), and removed by the first write after, so a head never
holds both. Every plane rule covers both prefixes for as long as this
one can be read."""

LEGACY_SESSION_KEY = LEGACY_CONVERSATION_PREFIX + "session"
"""The legacy plane's session record: agno's session dict minus its
runs, with ``run_ids`` in order, ``session_id``, and fork lineage in
``session_data["forked_from_session_id"]``."""

LEGACY_RUN_PREFIX = LEGACY_CONVERSATION_PREFIX + "runs/"

CONVERSATION_PREFIXES = (CONVERSATION_PREFIX, LEGACY_CONVERSATION_PREFIX)
"""Both conversation planes, for the rules that must cover each: the
merge policy, a fresh fork's wipe, a delete."""

COMPACTION_PREFIX = "__compaction__/"
"""Compaction's records: one key per fold, never rewritten (see
``nontainer.compaction`` and docs/compaction.md). A fold describes the
conversation of the session that made it, so like the conversation it
never merges into another session, and a fork that drops the
conversation drops these with it."""
