# Claude Launcher

Small Tkinter app (no dependencies, Python 3.10+) for running VS Code with the
Claude Code extension signed in to different Claude accounts — side by side.

```
python claude_launcher.py      # or double-click ClaudeLauncher.pyw (no console)
```

## How it works

Each account gets its own profile directory under `%APPDATA%\ClaudeLauncher\profiles\<id>\`:

| Directory | Used as | Holds |
|-----------|---------|-------|
| `claude\` | `CLAUDE_CONFIG_DIR` | Claude Code login (`.credentials.json`), `.claude.json`, settings, history |
| `vscode\` | VS Code `--user-data-dir` | VS Code settings/state for this account |

**Start** launches `Code.exe --user-data-dir <profile>\vscode [folder]` with
`CLAUDE_CONFIG_DIR=<profile>\claude`. The Claude extension, and `claude` run in
VS Code's integrated terminal, inherit that variable.

The separate `--user-data-dir` matters. VS Code runs one instance per user-data-dir.
If you launch it again with a different environment, the running window takes
over the request and keeps its old environment. A separate dir per account gives
each account its own VS Code instance, so several accounts can run at once.

- Extensions are **shared**, because they live in `~/.vscode/extensions` whatever the user-data-dir is.
- The first time an account starts, the app copies your normal `settings.json`,
  `keybindings.json` and snippets into its profile. After that, each profile's settings are independent.
- The first launch for an account isn't signed in. Log in from the Claude panel and
  the login is stored in that account's `claude\` dir. The list shows a warning
  if the signed-in email differs from the one you entered.
- VS Code is found automatically once, from the usual install locations or `code` on PATH, and the path is cached in
  `%APPDATA%\ClaudeLauncher\config.json`. Use **Change…** to override it.
- **Remove** can either keep or delete the account's profile data. Deleting covers both the VS Code and the Claude Desktop data.

## Claude Desktop

**Start Claude Desktop** runs `claude-desktop.exe --user-data-dir=<dir>`, with
`CLAUDE_CONFIG_DIR` set the same way as for VS Code. Each account's data dir
holds that account's login, so accounts can run side by side in separate windows.

- The data dir is `%USERPROFILE%\.claude-launcher\<id>\desktop`, not AppData.
  The Store/MSIX build of Claude Desktop moves new files it writes under AppData into its
  private package cache, so this path keeps the data in a real folder.
- The app finds Claude Desktop through the MSIX alias
  `%LOCALAPPDATA%\Microsoft\WindowsApps\claude-desktop.exe`, or through the older
  `%LOCALAPPDATA%\AnthropicClaude\claude.exe`.
- The first launch for each account asks you to sign in.
- Your normal Claude Desktop (`%APPDATA%\Claude`) is untouched.

## Notes

- If `ANTHROPIC_API_KEY` is set globally, Claude Code uses it instead of the
  per-account login.
- Your regular `~/.claude` (default account) is untouched. VS Code started normally keeps using it.
