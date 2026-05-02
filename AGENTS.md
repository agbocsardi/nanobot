# Nanobot Fork Workflow

## Remote layout

- `upstream` = `HKUDS/nanobot` (source of truth)
- `origin` = `agbocsardi/nanobot` (this fork)
- `upstream/main` = stable releases

```
upstream/main ─── origin/main ─── personal-build ─── feat/*
```

## Sync workflow (do this weekly)

```bash
# 1. Pull latest from upstream
git fetch upstream

# 2. Update fork's main
git checkout main
git merge upstream/main
git push origin main

# 3. Rebase personal-build onto updated main
git checkout personal-build
git rebase main
git push --force-with-lease origin personal-build
```

**Never let main drift.** A 1-commit rebase is trivial; a 161-commit gap is hell.

## Feature branches

Branch off `personal-build`, not `main`:

```bash
git checkout personal-build
git checkout -b feat/my-feature
```

When done, merge into `personal-build`:

```bash
git checkout personal-build
git merge feat/my-feature
```

## Conflict minimization

1. **Surgical diffs.** Change only the lines you need. Don't reformat adjacent code.
2. **New files > modified files.** New files (templates, skills, directories) never conflict on rebase. Lean on them.
3. **Enable `rerere`.** Remembers conflict resolutions so each conflict only bites once:
   ```bash
   git config --global rerere.enabled true
   ```
4. **Keep personal-build thin.** Aim for `main + N commits` where N stays small. If upstream rewrites something you've touched heavily, consider dropping your version.

## Personal-build philosophy

This is an **integration branch**, not a feature branch. It carries:

- Features not (yet) in upstream (whisper STT, dream prompt tweaks)
- Local config adjustments (vite bind, etc.)
- Infrastructure that makes more sense on our fork than upstream

When upstream ships something that supersedes a personal-build commit, drop the commit on the next rebase.

## Server install

```bash
ssh uhl
cd nanobot
git fetch origin
git checkout personal-build
git reset --hard origin/personal-build
/home/gergo/.local/bin/uv pip install . --python .venv/bin/python --reinstall
```

Run nanobot from a workspace directory, not from inside the repo.

## Lint

```bash
ruff check nanobot/ tests/
```
