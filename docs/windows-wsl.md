# cc-telegram on Windows (WSL2)

cc-telegram runs on Windows **inside WSL2** — the bot, tmux, and every Claude Code session it drives all live in the Linux distro. This guide explains why that is structural (not a packaging gap), the recommended layout, and — for people who also keep a native-Windows Claude Code install — how to share the parts of `~/.claude` that are safe to share.

---

## 1. Why WSL is required (and stays required)

The bot's entire terminal seam is tmux. Every session is a tmux window; internal routing is keyed by tmux window id (`@0`, `@12`); keystrokes are delivered with `tmux send-keys`; the terminal is read back with `tmux capture-pane`; and live state (idle / running / which program owns the pane) is verified through `pane_current_command`. These are not incidental helpers — they are the load-bearing mechanism the whole architecture is built on.

tmux has no native Windows port. Replacing it with a Windows-native pseudo-terminal (ConPTY) would mean rewriting that core seam — the window model, the send-keys dispatch, the pane capture/parse, and the `pane_current_command` liveness checks — against a completely different API. That is a different program, not a config flag.

So on Windows the bot, tmux, and the Claude Code sessions it launches all run **inside WSL2**. This is a structural requirement of the design, not a temporary limitation, and it is not expected to change.

---

## 2. Option A — everything in WSL (recommended)

The cleanest setup keeps the whole stack on the Linux side:

- Install the bot in WSL: `uv tool install --force --no-cache .` (see the main [README](../README.md) and [docs/DEPLOYMENT.md](DEPLOYMENT.md)).
- `tmux`, the `claude` CLI, and `uv` all installed **inside WSL** (`sudo apt install tmux`, then install `uv` and Claude Code per their Linux instructions).
- Keep your repos on the **WSL filesystem** (e.g. `~/dev/...`), **not** on `/mnt/c`. Files under `/mnt/c` cross the 9p filesystem bridge and are markedly slower for the many small reads Claude Code and git do; native WSL paths avoid that.
- Interact through **Windows Terminal** or **VS Code Remote-WSL** — both attach straight into the distro.

If you do **not** run a separate Windows-native Claude Code install, there is nothing to share and no cross-OS clunkiness: everything Claude Code needs already lives in the WSL `~/.claude`, and this is the whole story. Skip straight to the [README](../README.md) / [DEPLOYMENT.md](DEPLOYMENT.md) and treat WSL as your machine.

---

## 3. Option B — dual install (share config with a native Windows Claude Code)

If you keep a Windows-native Claude Code install alongside the WSL one, you can share the **portable** parts of `~/.claude` so skills, agents, and global memory stay in sync — while keeping the OS-specific parts separate.

### Share (symlink from WSL to a canonical copy on the Windows side)

- `skills/` — your skill library
- `agents/` — subagent definitions
- `CLAUDE.md` — global cross-project memory
- optionally `commands/` and `plans/`

Keep the **canonical copy on the Windows side** and symlink into it from WSL:

```bash
# in WSL — the real files live on the Windows side, WSL points at them
ln -s /mnt/c/Users/<you>/.claude/skills    ~/.claude/skills
ln -s /mnt/c/Users/<you>/.claude/agents    ~/.claude/agents
ln -s /mnt/c/Users/<you>/.claude/CLAUDE.md  ~/.claude/CLAUDE.md
# optional:
ln -s /mnt/c/Users/<you>/.claude/commands   ~/.claude/commands
ln -s /mnt/c/Users/<you>/.claude/plans      ~/.claude/plans
```

**Direction matters.** Canonical-on-`/mnt/c` with symlinks from WSL is more reliable than the reverse (canonical-in-WSL, reached from Windows via `\\wsl$\...`): the WSL filesystem is only reachable from Windows while the distro is actually running, so a Windows-side Claude Code launched before WSL starts would find its config missing. `/mnt/c` is always mounted while WSL runs, so the WSL side never has that problem.

### Do NOT share

- **`settings.json`** — its hook entries reference OS-specific commands. The managed `SessionStart` / `PreToolUse` / `Notification` hooks run `cc-telegram hook`, a command that exists **only inside WSL**; a Windows path in a hook command is meaningless to Linux and vice versa. `settings.json` must stay **per-OS**.
- **`.credentials.json`** — machine/OS-scoped auth; sharing it invites token confusion and is a needless secret-spread.
- **`projects/`** — machine-specific session transcripts (the JSONL the bot tails). Cross-linking them would mix two machines' session state.
- **`session_map`-adjacent state and `history/`** — runtime state tied to one machine's sessions.

### Caveats

- **`/mnt/c` reads are slow.** Fine for session-start skill/agent loads (read once, infrequently); do not put anything latency-sensitive (repos, transcripts) there.
- **Git line endings.** If the shared directory is itself a git repo, set `core.autocrlf` sensibly (`input` on the WSL side is usually safest) so CRLF churn doesn't rewrite every file across the two checkouts.
- **Reverse direction (junctions).** If you insist on canonical-in-WSL, Windows `mklink /J` **junctions** work without Developer Mode (unlike `mklink /D` symlinks, which need it) — but the distro-not-running failure mode above still applies, so prefer canonical-on-`/mnt/c`.

---

## 4. Option C — dotfiles-style repo

If your config churns often, the most robust long-term setup is a small git repo (a `dotfiles`-style repo) holding just the portable pieces — `skills/`, `agents/`, `CLAUDE.md`, optionally `commands/` — checked out on **both** sides. A tiny per-OS bootstrap script then:

- symlinks the shared pieces into `~/.claude/` (WSL) and `%USERPROFILE%\.claude\` (Windows), and
- installs a **separate, per-OS `settings.json`** from an OS-specific template (WSL template registers `cc-telegram hook`; the Windows template does not).

This keeps the shareable config versioned and identical across machines while the non-portable `settings.json` stays correctly per-OS. It's more setup than Option B's symlinks, but it survives reinstalls and new machines cleanly.

---

## 5. FAQ

**Can the bot drive a Windows-native Claude Code install?**
No. There is no tmux on native Windows, and the bot drives sessions exclusively through tmux. Bot-launched Claude Code sessions always run inside WSL. A native-Windows Claude Code install is fine to keep for your own interactive use — the bot just never touches it.

**Do my repos need to be in WSL?**
Strongly recommended, for performance. Repos on the WSL filesystem avoid the `/mnt/c` 9p bridge, which is noticeably slower for the many small file operations Claude Code and git perform. `/mnt/c` works but is slow; native WSL paths are the fast path.

**Does `cc-telegram hook --install` touch my Windows-side `~/.claude/settings.json`?**
No. It writes the **WSL-side** `~/.claude/settings.json` only (the one inside the distro). That's exactly why `settings.json` must not be a shared symlink: the hooks it installs (`SessionStart` / `PreToolUse` / `Notification`) all run `cc-telegram hook`, which exists only inside WSL — pointing a native-Windows Claude Code at those same hook entries would just fail. Let each OS keep its own `settings.json`.
