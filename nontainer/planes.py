"""The planes state is partitioned into, and the names that mark them.

Three of the four are key prefixes inside one session's branch; the
fourth is a branch namespace of its own.

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

CONVERSATION_PREFIX = "__agno__/"
"""The stored conversation: the session record and one key per run.
Written by ``nontainer.adapters.agno_db``, which derives its own key
names from this. A delegate's conversation never merges into its
caller's — two chats that never happened together cannot be
interleaved, and what a delegate has to say arrives as its answer."""

SHARED_BRANCH_PREFIX = "@store/shared/"
"""The shared plane: a whole branch rather than a prefix inside one.

``@store/shared/<name>`` is a workspace no session owns — it outlives
every one of them and many write it at once. A session id cannot begin
with ``@``, so the namespace is the store's alone: the branch is not a
session, ``Store.sessions()`` never lists one, and ``Store.delete``
cannot name one. Its files merge three-way on a concurrent commit,
because two writers landing at once is what the plane is for, and what
lives there is the embedder's word."""
