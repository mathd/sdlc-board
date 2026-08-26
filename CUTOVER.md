# Cutover: from the git board to the vault board

Both boards run against the same work. The **vault board is read-write**; the **git board is
read-only**, kept current by a scheduled `vault.py pull`. Nothing reads the `sdlc-state` branch in
the live path any more — it is the archive and the rollback target.

## Before you start

Tag the rollback point in the project repo, so it is named rather than hunted for:

```sh
git -C ~/sources/ticketing_system tag -l pre-vault-migration   # already created
```

That commit is the last one whose `.sdlc/server.py` reads the state worktree. Rolling back means
running the project repo at that tag against an `sdlc-state` branch the pull job has kept current.

## The scheduled pull

One cron line. It is what makes the archive real and the rollback possible.

```cron
*/15 * * * * cd ~/sources/sdlc-board && \
  FNS_API=http://10.99.0.31:9000 FNS_VAULT=sdlc FNS_CLIENT=sdlcBoard \
  FNS_TOKEN=$(cat ~/.config/sdlc-board/token) \
  /usr/bin/python3 vault.py pull --prune >> /tmp/sdlc-pull.log 2>&1
```

Keep the token in a file rather than the crontab. `--prune` deletes ticket JSON whose note has been
archived, so the two boards agree about what is still open.

The pull does **not** commit. Commit deliberately, so the state branch's history stays readable:

```sh
cd ~/sources/ticketing_system.sdlc-state
git add -A .sdlc/tickets && git commit -m "chore(sdlc): pull from vault [skip ci]"
```

## The daily check

```sh
cd ~/sources/sdlc-board && python3 vault.py pull
git -C ~/sources/ticketing_system.sdlc-state diff --stat
```

An **empty diff after a day of work means the boards agree**. After five consecutive clean days,
stop reading the git board.

The diff is a real signal, not noise: a pull against an untouched worktree rewrites **271 of 272**
tickets byte-for-byte. Getting there took matching the corpus's own formatting rather than imposing
one — indent width varies (a handful of tickets use a 1-space indent), some files end without a
trailing newline, and top-level key order differs between tickets. Any formatting the pull imposed
would show up as drift that is not drift, and the check would stop meaning anything.

### The one known, permanent difference

`TKT-219.json` always shows one added line:

```
+  "summary": "Bind a settlement plan's identity to the payment operation"
```

It is the single ticket in the corpus that uses `title` instead of `summary`. The board reads
`summary`, so the note derives one from the heading. Expected, stable, and not drift. Everything
else should be empty.

## What a non-empty diff means

| The diff shows | Cause |
|---|---|
| A `status` change | Real drift: someone moved a card on one board and not the other. Investigate before trusting either. |
| A whole file rewritten | Formatting, not drift — the pull's shape-matching missed a case. Fix the pull, do not commit the churn. |
| A ticket disappearing | It was archived in the vault and `--prune` removed it. Expected. |
| `TKT-219` only | Expected. See above. |

## Rollback

**A checkout, not a switch.** Run the project repo at `pre-vault-migration`, against the
`sdlc-state` branch the pull job has kept current:

```sh
cd ~/sources/ticketing_system
git checkout pre-vault-migration
python3 .sdlc/server.py          # the old git board, on the state worktree
```

Stop the vault board first, or two writers will disagree about the same tickets. The vault keeps
whatever it had; nothing is lost, but transitions made on the vault board after the last pull are
not in git.

## After cutover

Repoint the `sdlc-ticket` skill. `.claude/skills/sdlc-ticket/references/local-tracker.md` still
documents the git board — state on the `sdlc-state` branch, timing from `git log`, the board served
by `.sdlc/server.py`. That file is correct today and wrong the moment you stop reading the git
board, so it is deliberately **not** changed until the five clean days have passed.

Keep the scheduled pull permanently. It costs one cron line and it is what makes the archive real.
