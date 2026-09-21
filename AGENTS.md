<!-- BEGIN focus-board-brain -->
## Task context (Focus Board Brain)

This repo is linked to Nevin's task system. `.brain/` is a symlink to the
shared context folder, synced from his home server.

**Read at the start of a session:** `.brain/STATE.md` — the current plan, what
is in progress, what is next. `.brain/BRIEF.md` — his own notes and
constraints; change it only if he asks you to. `.brain/tasks.json` — the same data with
task ids, if you need them.

Do not re-plan work `STATE.md` already lists as in progress or done.

**When something becomes actionable,** write a NEW markdown file into
`.brain/captures/`, one item per file, e.g.
`.brain/captures/2026-09-21-short-slug.md`. One or two sentences is
enough. Nevin runs a sync, which turns them into items on his task lists.

He has ADHD: write the concrete next physical action, not the topic. A task
whose first step is not obvious does not get started.
Good: "Redo the Nyquist plots in problem set 4 before Thursday."
Bad:  "TODO: various things"

**To update an existing task,** write a capture whose FIRST line is a directive
with the task id from `tasks.json`:

    done: t_a3d66700d70d
    doing: t_334ca79ea71e
    blocked: t_ef1e0428e710
    drop: t_1234567890ab

Anything after the first line is kept as a note. `done` closes it, `doing`
promotes it to In Progress, `blocked` moves it to Waiting, `drop` archives it.
The id must match exactly; an unknown id is treated as an ordinary new capture,
never guessed at. This is how you report work you actually finished — without
it, nothing you do here can close a card.

Backlog steps for a project appear as ONE card listing them, so `tasks.json` is
where you look up ids, not the board.

**Never** edit `.brain/STATE.md`, `.brain/tasks.json` or `.brain/CLAUDE.md`
— the brain owns them and overwrites them. Never delete anything in
`.brain/dumps/`; that is his raw thinking, kept verbatim.
<!-- END focus-board-brain -->
