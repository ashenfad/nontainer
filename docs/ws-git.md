# ws-git

`ws-git` is the agent's git over its own session: an index it fills
across several edits, commits it names, a log it can read back, and the
verbs that reach the sessions around it. It is a terminal builtin, so an
agent types it in the shell next to `cat` and `grep`.

This page is the human-readable twin of `ws-git help`. The host's half
of the same machinery is `ws.index` in the
[API reference](api.md#versioning-gated-by-wscaps) — one implementation
with two spellings, so the host and the agent see one index and one
graph. Registration is the embedder's call:

```python
from nontainer.wsgit import register_wsgit

register_wsgit(ws)        # now the shell answers `ws-git`
```

Until someone calls it the agent gets `ws-git: command not found`. The
verbs need a provider with `caps.index` (kvgit has it); anywhere else
every verb refuses by name.

## The verbs

Git's spelling is on the left, what it does here on the right.

| ws-git | what it does here |
|---|---|
| `stage <paths>` | add paths to the index. Silent, like `git add` |
| `unstage <paths>` | drop paths from the index |
| `commit [-m MSG]` | commit the staged set, or everything modified when nothing is staged |
| `reset` | abandon the composition and keep the tree. Mixed-only |
| `status [--porcelain]` | what is staged, what is modified, what a merge left marked |
| `diff [--cached] [--check] [paths...]` | unified diff against your last commit |
| `diff <session>` | your tree against another session's last commit, grouped by what that session was sent to do |
| `log [-n N] [--all] [-S <string>]` | your own commits, newest first |
| `log <session>` | another session's commits |
| `show <ref>` | one commit: its message and its diff |
| `checkout <ref>` | restore your tree to one of your commits |
| `checkout <ref> -- <paths>` | make just those paths match that ref, which may name another session |
| `tag` | your bookmarks, one `name -> commit` per line |
| `tag [-f] <name> [<commit>]` | bookmark a commit of yours by name. `-f` moves a name that is taken |
| `tag -d <name>` | drop a bookmark. The commit stays where it is |
| `stash [push [-m MSG]]` | put everything you have modified on a branch of its own and restore your tree to your last commit |
| `stash list` | your stashes, newest first, as `stash@{N}: message` |
| `stash pop [stash@{N}]` | merge one back and delete its branch |
| `stash drop [stash@{N}]` | delete one without bringing it back |
| `stash show [stash@{N}]` | what one holds, as a diff against your last commit |
| `branch` | the sessions on this store, yours marked `*` |
| `branch <name> [--at <ref>] [--fresh] [--paths <paths>]` | fork a session. It does not switch, as `git branch` does not |
| `merge <session>` | merge that session's last commit into yours |
| `merge --abort` | restore the tree to the commit an outstanding merge landed on, and clear it |
| `revert <commit>` | a new commit undoing one commit's change |
| `cherry-pick <session>@<commit>` | a new commit applying one change from another session |
| `worktree add <dir> <session>[@<commit>]` | check another session's tree out under a directory, read-only |
| `worktree list` / `worktree remove <dir>` | the worktrees here; take one down |
| `sparse-checkout list` | the paths this session was given to see |
| `help` | the whole surface in one screen |

There is no `.git`: branches are sessions and history is commits.

## Two ways to commit

`ws-git commit` reads what you staged, and there are exactly two cases.

**Nothing staged commits everything modified.** Git would refuse this
("no changes added to commit") because git has `-a` to ask with; here
the verb reads its own context instead, since an agent has no index open
in front of it to have forgotten about.

**A staged set commits only that set.** What you left out stays in the
working tree, uncommitted and unchanged — the commit's tree is exactly
what you said it was, never what happened to be modified at the time. So
a partial commit is safe to make in the middle of other work: the rest
is still there afterwards, still modified, still yours to commit next.

`-m MSG` is optional, where git insists on a message. A commit without
one is still a point in the graph, and `log` shows it under the name of
the verb that made it.

There is no `-a` and no pathspec. Staging is what names a subset:

```
$ ws-git status
 M auth.py
 M notes.md

$ ws-git stage auth.py

$ ws-git status
M  auth.py
 M notes.md

$ ws-git commit -m 'add auth'
[main 3250a37] add auth (1 file)

$ ws-git status
 M notes.md
```

`ws-git reset` clears the index and leaves every file as it is. Going
back to a commit is `ws-git checkout <ref>`, so `--soft` and `--hard`
are usage errors naming it.

## What status shows

Clean is silent, as in git. Otherwise `status` prints, in this order:

- **`view: a/, b.md`** — the paths this session was given to see, when
  it was forked with a narrowed view. A session that sees the whole tree
  prints no such line. It tells a reader what the rows below are
  measured over.
- **`## merging <session>@<commit> (N unresolved)`** — the merge, revert
  or cherry-pick that left markers behind, and how many files still
  carry them. Git has no equivalent line; a session has no working tree
  to leave a conflicted state in, so the state is named instead.
- **the file rows**, in git's short `XY` columns: `M ` staged, ` M`
  modified and not staged, `UU` left marked by a merge. A path is in one
  column or the other, never both — staging a file moves it from the
  right column to the left.
- **a `worktrees:` block** when any are up, one line per worktree in the
  shape `worktree list` prints.

```
$ ws-git status
## merging other@569ede1 (1 unresolved)
UU shared.md
```

The rows are measured against **your last ws-git commit**, never the
store's head. The workspace commits on its own as you work, and none of
those commits disturbs a composition: they land in the store and leave
your index and your modified files exactly where they were.

`--porcelain` is accepted and changes nothing: the porcelain shape is
the only shape, so the flag is there for an agent that types it out of
habit.

## Refs and short ids

Five things can be a ref, and which of them a verb takes is the verb's
business:

- **`HEAD`** — your last ws-git commit. Unborn before your first one,
  and the refusal says so.
- **a commit id** — seven hex characters or more. Seven is what every
  ws-git line prints, so an id read off a log line is one you can type
  back. `checkout` and `show` take one of *your* commits; a commit of
  this session that is not one of yours is refused by name, because
  taking one as your head would strand your whole log behind it.
- **a session name** — what that session's agent last committed, or its
  branch head when it never used ws-git. `merge <session>`,
  `diff <session>`, `log <session>` and `checkout <session> -- <paths>`
  all read a name that way, so they cannot disagree about what a
  delegate said.
- **`<session>@<commit>`** — one exact state on one session: this commit
  on this branch. `cherry-pick` requires this form, and
  `worktree add` accepts it.
- **a tag** — a name you gave one of your own commits with `ws-git tag`.
  It is a ref wherever a ref is taken, and `<session>@<tag>` reads
  another session's bookmark the way `<session>@<commit>` reads one of
  its commits. These are not the host's tags (`ws.tags`, `store.tags`),
  which name states for the code around the workspace; a ws-git tag is
  yours, and the [Tags](#tags) section says what that means.

Every spelling ws-git prints, it accepts back. Commit lines print seven
characters of an id; worktree lines print `session@<seven characters>`;
both go straight back into the next verb.

A short id that matches more than one commit is refused, naming the
commits it could mean and asking for more of it — never resolved to
whichever came first. A word that names neither a commit nor a session
is refused too: `diff` calls it an ambiguous argument rather than
reading it as a pathspec that matches nothing, since silence there means
"no differences" and would answer a mistyped session name with an
apparent all-clear.

## Tags

`ws-git tag <name>` bookmarks one of your commits, and from then on the
name is a ref:

```
$ ws-git tag before-refactor
$ ws-git tag
before-refactor -> 3250a37

$ ws-git log
df5ad62 strip the user
3250a37 (tag: before-refactor) add auth

$ ws-git checkout before-refactor
[main] restored to 3250a37
```

With no commit named it bookmarks your head; `ws-git tag <name> <ref>`
bookmarks whatever the ref names. The bare `ws-git tag` prints `(none)`
when you have made none. A name already taken is refused, as
git refuses one, and `-f` moves it. `ws-git tag -d <name>` drops the
name and leaves the commit exactly where it is.

**The tag is your bookmark, not a name in the store.** It lives with
your index and your commit graph, in your session:

- It pins nothing. The store's history is append-only and every commit
  you made is reachable from your head, so there is nothing for a tag
  to hold in place — which is what the host's tags are for.
- It is gone when the session is.
- A fork starts with none. A name you chose points into your log, and
  your log is not the one the delegate has.
- A merge brings none over. Merging a session brings its files, never
  its names for its own commits.

Two names it will not take: one that is already a tag here, and one
spelled like a commit id (seven or more hex characters). The second is
what lets a tag be read before a hash with nothing ambiguous about it.

Another session's bookmarks are readable as `<session>@<tag>`, which
is that session's name for one exact state:

```
$ ws-git worktree add review polish@shipped
worktree review: polish@dd906ff (read-only)

$ ws-git cherry-pick polish@shipped
```

What the verb prints back is the commit, never the name it was given:
the name is the other session's and means nothing in yours.

## Stash

`ws-git stash` puts everything you have modified on a branch of its own
and restores your tree to your last commit:

```
$ ws-git status
 M auth.py
 M notes.md

$ ws-git stash
Saved working directory and index state stash@{0}: add auth

$ ws-git status
$ ws-git stash list
stash@{0}: add auth
```

The message defaults to your last commit's; `ws-git stash push -m
"half-done rewrite"` gives it one of its own. Taking it back is
`ws-git stash pop`:

```
$ ws-git stash pop
Auto-merging auth.py
Auto-merging notes.md
[main 8c1f2ad] merge main.stash-0 (2 files)
Dropped stash@{0} (main.stash-0)
```

It is **a fork and a checkout**, which is why it behaves the way it
does. The branch is forked at your last commit and your modified files
are written onto it, so what the stash holds is the change; popping it
is an ordinary three-way merge against that same commit, and it comes
back even though your tree has since moved. Overlapping work conflicts
exactly as a merge does — markers in the commit, `UU` in `status` — and
a pop that conflicts **keeps the stash**, so you can drop it yourself
once the resolution is what you wanted. `ws-git stash drop` deletes one
without bringing it back, and `ws-git stash show` diffs one against
your last commit.

Two refusals, both git's. With nothing modified: `No local changes to
save`. In a session that has committed nothing: `You do not have the
initial commit yet` — there is no commit to restore your tree to, so
there is nothing a stash could put aside.

A stash **is a branch**, so `ws-git branch` lists `<session>.stash-N`
while one is up, and so does a host or an embedder listing the store's
sessions (`store.sessions()`) — a stash is an ordinary session there,
and `Store.delete` takes one by name. `ws-git stash list` is the view
that numbers them git's way, newest `stash@{0}`, and prints `(none)`
when there are none; the branch keeps a number of its own that only
ever goes up, so no stash is ever renamed under you.

## What a merge carries, and what a fork starts with

Everything ws-git knows about *your* session — your head, your staged
set, your tags, the stash you took, the commit an outstanding merge
landed on, and the view the session was given — is one record on your
branch, and two rules cover all of it.

**A merge takes yours.** `ws-git merge <session>` brings that session's
files and nothing else. Your tags stay yours and none of its arrive;
its stashes are its own, on its own branch and numbered there; the
merge `merge --abort` finds is the one you started, never one the
source left outstanding; and merging a delegate that was given a narrow
view never narrows you. Two sessions' bookkeeping is never reconciled,
because on this record a merge resolves to ours every time.

**A fork starts with none of it.** A new session has no head, nothing
staged, no tags, no stashes and no merge outstanding, so `ws-git log`
there is empty until it commits and until then everything it can see
reads as modified — the way a repo reads before its first commit. It
inherits the files, and a view only when it is forked with `--paths`:
a fork of a narrowed session that is handed the whole tree sees the
whole tree.

## Conflicts

A merge, a revert and a cherry-pick all **land even when they
conflict**. Git leaves conflicts uncommitted in the working tree; there
is no working tree here to leave them in, so the markers go *into* the
commit and the conflicted state is itself a commit you can check out.

```
$ ws-git merge other
CONFLICT (content): Merge conflict in shared.md
[main 569ede1] merge other (1 file)
ws-git: Merge landed with conflict markers in 1 file(s): fix them and commit (ws-git status shows them as UU).
```

The exit code is non-zero and the news on stderr says what to do next.
From there:

- `ws-git status` lists the marked files as `UU` under the `## merging`
  line.
- `ws-git diff --check` finds the marker lines by path and line number,
  and exits 2 when it finds any.
- Edit the files as you would any others — the markers are ordinary
  bytes.
- `ws-git commit` records the resolution.

A file stops being unresolved **when its markers are gone**, not when it
is committed. So resolving them one at a time shows progress in
`status`, and the merge context ends with the last marker — whichever
commit carried the resolution:

```
$ ws-git status
 M shared.md

$ ws-git commit -m resolve
[main 728de11] resolve (1 file)

$ ws-git status
```

A commit that *includes* a marked path resolves it only if what it
commits has no markers left; a commit that leaves the path out leaves it
marked. Membership in a commit is not resolution.

There is no `--continue`, for merge or for the other two: the commit is
already made, so there is no half-finished operation to continue out
of.

**While a merge is outstanding, nothing else moves your tree.**
`merge`, `stash`, `stash pop`, `revert` and `cherry-pick` all refuse
until the markers are gone. A second change would mark files this merge
already marked, with no way to tell whose conflict is whose; moving the
tree elsewhere would carry the markers along and drop the record of
where they are, leaving `status` clean over a tree full of them. There
are two ways out, and every one of those refusals names both: commit
the resolution, or abort.

**`ws-git merge --abort` is the way out of a merge you do not want.**
It restores your tree to the commit the merge landed on and clears the
merge, so `status` reads clean and the markers are gone:

```
$ ws-git merge --abort
[main] aborted merge of other, restored to 3250a37
```

The merge itself stays in the session's history — the restore is a new
appended commit, as `ws-git checkout` is — so the conflicted state is
still there for anyone who wants it. It is refused when no merge is
outstanding, and refused in a session that had no commit of its own
when the merge landed, since there is then no commit of yours to go
back to. A revert and a cherry-pick have no abort: neither records a
merge to be outstanding, and the way back from either is
`ws-git checkout <your commit before it>`.

Non-file contested state — something no merge function can resolve —
aborts instead, changes nothing, and says so.

## Worktrees

`ws-git worktree add <dir> <session>` checks another session's tree out
under a directory of its own, and you read it with `cat`, `ls` and
`grep`:

```
$ ws-git worktree add review polish
worktree review: polish@dd906ff (read-only)

$ ws-git worktree list
worktree review: polish@dd906ff (read-only)
```

It is **read-only and pinned at a commit**, on purpose. Work moves
between sessions by merge and by take, and a second tree an agent could
edit in place would be a third way with no way back. To change another
session's branch, ask that session, or take its files with
`ws-git checkout <session> -- <paths>` and change yours.

A bare session name takes its head at that moment, and it stays there;
nothing later appears in the worktree by itself. To see newer work,
take the worktree down and put it up again:

```
$ ws-git worktree remove review
$ ws-git worktree add review polish
worktree review: polish@a3b4203 (read-only)
```

Adding over a worktree that is still up refuses and says the same
thing, since what is pinned stays pinned:

```
$ ws-git worktree add review polish
ws-git: there is already a worktree at 'review': it is pinned at a commit, so seeing newer work means taking it down and putting it up again (ws-git worktree remove review).
```

To pin a state deliberately rather than take a head, name the commit
with `<session>@<commit>`.

What lands is the source's **whole branch**, not what its own session
can see — a delegate given a narrow view is exactly the one worth
reading this way.

A worktree lives outside your versioned tree. No commit of yours carries
it, and no `status` or `diff` row can ever name a file in it, which is
why `status` ends with a `worktrees:` block: a directory full of files
that never show as modified is otherwise a puzzle.

`worktree add` refuses a directory that already holds something, a
directory inside a worktree already up, and this session's own name.

## Revert and cherry-pick

These are one operation with the two commits swapped: a three-way merge
whose base is *named* rather than found, resolved by the rules a merge
resolves by, landing as a new commit.

```
$ ws-git revert HEAD
Auto-merging shared.md
[main 1661332] revert 728de11 (1 file)

$ ws-git cherry-pick helper@c274c81
Auto-merging b.txt
[main fae9ede] cherry-pick helper@c274c81 (1 file)
```

`revert <commit>` undoes what that commit changed. The commit stays in
the log and the undoing lands on top of it — history is append-only, and
a revert is itself a commit that can be reverted. What is undone is the
**change**, not the state: work done since in other files, and elsewhere
in the same file, stands.

`cherry-pick <session>@<commit>` applies one commit's change from
another session. Its change, not its tree: what the commit before it
holds is left as yours. Taking everything a session committed is
`ws-git merge`. The named session must be one whose history reaches that
commit, so a sibling's commit under the wrong name is refused rather
than applied with a provenance nothing supports.

Two rules decide what "that commit's change" means:

- **The virtual parent.** A commit's change is measured against its
  parent in *your* graph, never the store's. The workspace commits your
  edits for durability as you make them, so the store parent of a ws-git
  commit already holds them; only the agent's graph knows what the
  commit was composed on.
- **The fork point.** A session's first ws-git commit is measured
  against the commit its session was forked at, which is what makes a
  delegate's first commit its own work rather than the tree it
  inherited. A session that never forked measures against the empty
  tree, so reverting its first commit removes the files it introduced.

A merge commit reverts to **ours** — the side it was made from, the way
`git revert -m 1` does. It is the only side a session has.

Where the change is already in your tree, nothing is committed and the
verb says so. Both verbs refuse work you have not committed, and refuse
while a merge is still outstanding, since a second change over a
conflicted merge would mark files that merge already marked.

## Bringing a delegate's work back

A delegate works on a branch of its own and touches none of your files.
You bring the work back yourself, and there are five ways, in rising
order of commitment:

| you want | type |
|---|---|
| to read what it changed | `ws-git diff <name>` |
| to read its tree with ordinary tools | `ws-git worktree add <dir> <name>` |
| all of it | `ws-git merge <name>` |
| some files | `ws-git checkout <name> -- <paths>` |
| one of its commits | `ws-git cherry-pick <name>@<commit>` |

A commit there may be spelled as that session's own tag,
`<name>@<tag>`.

`ws-git diff <name>` groups its changes by the view the delegate was
given:

```
$ ws-git diff polish
# 1 path(s) in polish's seed
diff --git a/auth.py b/auth.py
--- a/auth.py
+++ b/auth.py
@@ -1,2 +1,2 @@
 def login(user):
-    return user.strip()
+    return user.strip().lower()
# 1 path(s) elsewhere
diff --git a/why.md b/why.md
--- a/why.md
+++ b/why.md
@@ -0,0 +1 @@
+lowercased for lookup
```

Git has no such headers. The collateral a delegate touched outside what
it was sent to do is the thing a caller must not miss, and a merge takes
both groups — the grouping is what stops the second from going
unnoticed. The split is by the paths the delegate was *seeded* with: a
file it created is in its view because it made it, and that is exactly a
change you have not seen before.

`ws-git checkout <name> -- <paths>` is git's
`restore --source=<ref> -- <paths>`, exactly. A path that names a
**directory** mirrors that subtree, so a file it holds here and the ref
does not is removed — taking a delegate's `pkg/` cannot leave behind the
`pkg/old.py` the delegate deleted. A path that names a file moves that
file and removes nothing. What lands are ordinary writes, so your own
`status` sees them as work in the tree, to commit or not as you choose.

`ws-git merge <name>` takes only what has been **committed, on both
sides**. Your tree must have nothing modified against your own last
ws-git commit, and the source is merged at its last agent commit rather
than at its branch head — a source that has written since is refused
rather than merged at a state its agent has moved past. Both refusals
name the same two fixes: commit the work, or check out the last commit
to drop it. A session that has never made a ws-git commit has no such
commit to differ from, so there only an open index refuses.

`ws-git branch <name> --paths <paths>` is the other direction: forking a
session and narrowing what it can **see**, not what its branch holds.

A narrowed session asks what it was given with
`ws-git sparse-checkout list`, instead of discovering it by being
refused a write. It prints the seed paths one per line, rendered the way
every other path is — relative to the root, a directory with its slash —
or `(full)` for a session that sees the whole tree. The bare
`ws-git sparse-checkout` prints the same, and any other subcommand is a
usage error, because a view is given at the fork and cannot be changed
from the terminal.

It prints the **seed**, not the view as it stands: a file the session
created joined its view because it made it, while what it was narrowed
to is what it was given.

## What is refused

One verb refuses with a hint that names a terminal verb to reach for
instead: **`rebase`**, and history rewriting generally. History here is
append-only, so branch from the commit you want
(`ws-git branch <name> --at <ref>`) and merge forward;
`ws-git checkout <ref>` goes back.

`switch`, `reflog`, `remote` and every other git verb are simply not
ws-git commands: they earn the unknown-command message, which lists the
whole supported surface. What each would have meant is covered anyway —
a session **is** a branch, so there is nothing to switch to;
`ws-git log --all` is the reflog, showing every commit the session
holds; and there is no remote, because every session lives in one store
and the verbs that reach another one name it directly.

## ws-git and the store

The session's own history is append-only: a branch head only ever moves
forward, and nothing but store-level administration takes a commit away.
That has three consequences worth knowing at the terminal.

**The workspace commits on its own.** Every tool call that changes
something lands in the store, for durability, at moments you did not
choose. Those commits are plumbing: `ws-git log` never shows them, and
showing them would bury your history in your own tool calls. `--all`
shows every commit the session holds, the framework's and the fiction's
bookkeeping included:

```
$ ws-git log
df5ad62 ws-git.merge from polish
e0ec0be strip the user
3250a37 add auth

$ ws-git log --all -n 6
b1b59c9 terminal
42ddae4 ws-git.merge-record
df5ad62 ws-git.merge from polish
cefe3a1 terminal
7fd1dcb ws-git.restore
e0ec0be strip the user
```

**Your work is durable before you commit it.** Nothing is withheld from
the store while you compose, and nothing suspends the workspace's own
committing. A commit of yours is a point in *your* graph, not the
difference between saved and lost.

**`ws-git checkout` rewinds you, and the store appends.** The tree is
restored and your head moves back, but the restore lands as a new
commit, so nothing already committed leaves the session:

```
$ ws-git checkout 23954a1
[main] restored to 23954a1
```

The commits you stepped off are still in the session's history, which
means an undo is redo-able and stepping off a line of work costs nothing
— you do not have to tag or fork before going back.

`ws-git log -S <string>` is git's pickaxe over the same history: only
the commits where the number of times `<string>` occurs in the tree
changed between the commit and its parent, with `+` or `-` after the id
for appeared or vanished. A commit that only moved the string within a
file is not one of them. It reads the files that changed in each commit
and no others — a file both sides hold unchanged counts the same on each
side, so it cannot move the total — and bytes that are not text hold no
occurrences, so a binary file never matches.
