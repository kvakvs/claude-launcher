"""Claude Launcher - run VS Code with Claude Code signed in to a chosen account.

Each account gets its own directory pair under the launcher's data root:

    <data>/profiles/<id>/claude   -> CLAUDE_CONFIG_DIR (Claude Code auth + settings)
    <data>/profiles/<id>/vscode   -> VS Code --user-data-dir

A separate VS Code user-data-dir is required: VS Code is single-instance per
user-data-dir, so launching it again with a different environment would just
hand the request to the already running window, which keeps the old
CLAUDE_CONFIG_DIR. With its own user-data-dir, every account runs in its own
VS Code instance whose extension host (and integrated terminal) inherits the
account's CLAUDE_CONFIG_DIR. Extensions stay shared (~/.vscode/extensions).

Claude Desktop works the same way: it is an Electron app with one instance per
--user-data-dir, so each account gets its own desktop data folder (and the same
CLAUDE_CONFIG_DIR for the Claude Code sessions it runs).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "ClaudeLauncher"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Variables that would make the spawned VS Code misbehave if the launcher itself
# was started from a VS Code terminal or from inside a Claude Code session.
STRIPPED_ENV = {
    "ELECTRON_RUN_AS_NODE",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_USER_DATA_DIR",
}
STRIPPED_ENV_PREFIXES = ("VSCODE_",)


def data_root() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / APP_NAME


def default_vscode_user_dir() -> Path:
    """The user's normal VS Code 'User' settings directory."""
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", "")) / "Code" / "User"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Code" / "User"
    return Path.home() / ".config" / "Code" / "User"


# --------------------------------------------------------------------------- config


class Config:
    def __init__(self, path: Path):
        self.path = path
        self.vscode_path: str | None = None
        self.desktop_path: str | None = None
        self.accounts: list[dict] = []
        self.last_selected: str | None = None
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            messagebox.showwarning(APP_NAME, f"Could not read config, starting fresh:\n{exc}")
            return
        self.vscode_path = data.get("vscode_path")
        self.desktop_path = data.get("desktop_path")
        self.accounts = data.get("accounts", [])
        self.last_selected = data.get("last_selected")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "vscode_path": self.vscode_path,
                    "desktop_path": self.desktop_path,
                    "accounts": self.accounts,
                    "last_selected": self.last_selected,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def find(self, account_id: str) -> dict | None:
        return next((a for a in self.accounts if a["id"] == account_id), None)


def profile_dir(account: dict) -> Path:
    return data_root() / "profiles" / account["id"]


def claude_dir(account: dict) -> Path:
    return profile_dir(account) / "claude"


def vscode_dir(account: dict) -> Path:
    return profile_dir(account) / "vscode"


def desktop_dir(account: dict) -> Path:
    if sys.platform == "win32":
        # The MSIX build of Claude Desktop redirects new files it creates under
        # AppData into its private package cache. Keep its data outside AppData
        # so it lands in a real folder that can be found and deleted.
        return Path.home() / ".claude-launcher" / account["id"] / "desktop"
    return profile_dir(account) / "desktop"


def account_dirs(account: dict) -> list[Path]:
    """Every on-disk folder belonging to the account (for deletion)."""
    dirs = [profile_dir(account)]
    if sys.platform == "win32":
        dirs.append(desktop_dir(account).parent)
    return dirs


def signed_in_email(account: dict) -> str | None:
    """Email Claude Code reports as signed in for this account, if any."""
    cdir = claude_dir(account)
    for name in (".claude.json", ".config.json"):
        try:
            data = json.loads((cdir / name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        email = (data.get("oauthAccount") or {}).get("emailAddress")
        if email:
            return email
    return None


def has_credentials(account: dict) -> bool:
    return (claude_dir(account) / ".credentials.json").exists()


# --------------------------------------------------------------------------- VS Code discovery


def _vscode_candidates() -> list[Path]:
    found: list[Path] = []
    if sys.platform == "win32":
        roots = [
            os.environ.get("LOCALAPPDATA", "") + r"\Programs\Microsoft VS Code",
            os.environ.get("ProgramFiles", "") + r"\Microsoft VS Code",
            os.environ.get("ProgramFiles(x86)", "") + r"\Microsoft VS Code",
        ]
        found += [Path(r) / "Code.exe" for r in roots if r]
        # `code` on PATH is <install>\bin\code.cmd -> Code.exe lives one level up.
        cli = shutil.which("code")
        if cli:
            found.append(Path(cli).resolve().parent.parent / "Code.exe")
            found.append(Path(cli))
    elif sys.platform == "darwin":
        found.append(Path("/Applications/Visual Studio Code.app/Contents/MacOS/Electron"))
        found.append(Path.home() / "Applications/Visual Studio Code.app/Contents/MacOS/Electron")
        cli = shutil.which("code")
        if cli:
            found.append(Path(cli))
    else:
        cli = shutil.which("code")
        if cli:
            found.append(Path(cli))
        found += [Path("/usr/share/code/code"), Path("/snap/bin/code")]
    return found


def _desktop_candidates() -> list[Path]:
    found: list[Path] = []
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            # MSIX (current installer): app execution alias, accepts arguments.
            found.append(Path(local) / "Microsoft" / "WindowsApps" / "claude-desktop.exe")
            # Older Squirrel installer.
            found.append(Path(local) / "AnthropicClaude" / "claude.exe")
        alias = shutil.which("claude-desktop")
        if alias:
            found.append(Path(alias))
    elif sys.platform == "darwin":
        found.append(Path("/Applications/Claude.app/Contents/MacOS/Claude"))
        found.append(Path.home() / "Applications/Claude.app/Contents/MacOS/Claude")
    else:
        for name in ("claude-desktop", "claude-desktop-bin"):
            path = shutil.which(name)
            if path:
                found.append(Path(path))
    return found


def _first_file(candidates: list[Path]) -> str | None:
    return next((str(c) for c in candidates if c.is_file()), None)


def find_vscode() -> str | None:
    return _first_file(_vscode_candidates())


def find_desktop() -> str | None:
    return _first_file(_desktop_candidates())


# --------------------------------------------------------------------------- launching


def seed_vscode_settings(target_user_dir: Path) -> None:
    """On first use, copy the user's normal VS Code settings into the profile."""
    if target_user_dir.exists():
        return
    target_user_dir.mkdir(parents=True)
    source = default_vscode_user_dir()
    for name in ("settings.json", "keybindings.json"):
        if (source / name).is_file():
            shutil.copy2(source / name, target_user_dir / name)
    if (source / "snippets").is_dir():
        shutil.copytree(source / "snippets", target_user_dir / "snippets")


def account_env(account: dict) -> dict[str, str]:
    cdir = claude_dir(account)
    cdir.mkdir(parents=True, exist_ok=True)
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in STRIPPED_ENV and not k.startswith(STRIPPED_ENV_PREFIXES)
    }
    env["CLAUDE_CONFIG_DIR"] = str(cdir)
    return env


def spawn_detached(args: list[str], env: dict[str, str]) -> None:
    kwargs: dict = {"env": env, "close_fds": True, "cwd": str(Path.home())}
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
        if args[0].lower().endswith((".cmd", ".bat")):
            args = ["cmd.exe", "/c", *args]
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, **kwargs)


def launch_vscode(vscode_path: str, account: dict) -> None:
    vdir = vscode_dir(account)
    seed_vscode_settings(vdir / "User")
    args = [vscode_path, "--user-data-dir", str(vdir)]
    folder = account.get("folder")
    if folder:
        args.append(folder)
    spawn_detached(args, account_env(account))


def launch_desktop(desktop_path: str, account: dict) -> None:
    ddir = desktop_dir(account)
    ddir.mkdir(parents=True, exist_ok=True)
    # Chromium only parses switches in --name=value form.
    spawn_detached([desktop_path, f"--user-data-dir={ddir}"], account_env(account))


# --------------------------------------------------------------------------- GUI


class AccountDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, title: str, account: dict | None = None):
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self.transient(parent)
        self.result: dict | None = None

        self.email = tk.StringVar(value=(account or {}).get("email", ""))
        self.folder = tk.StringVar(value=(account or {}).get("folder", ""))

        frm = ttk.Frame(self, padding=12)
        frm.grid(sticky="nsew")
        ttk.Label(frm, text="Email:").grid(row=0, column=0, sticky="w", pady=4)
        email_entry = ttk.Entry(frm, textvariable=self.email, width=42)
        email_entry.grid(row=0, column=1, columnspan=2, sticky="ew", pady=4)

        ttk.Label(frm, text="Open folder:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=self.folder, width=34).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Button(frm, text="Browse…", command=self._browse).grid(row=1, column=2, padx=(4, 0))
        ttk.Label(frm, text="(optional)", foreground="gray").grid(row=2, column=1, sticky="w")

        btns = ttk.Frame(frm)
        btns.grid(row=3, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="OK", command=self._ok, default="active").pack(side="left", padx=4)
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="left")

        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        email_entry.focus_set()
        self.grab_set()
        self.wait_window()

    def _browse(self) -> None:
        path = filedialog.askdirectory(parent=self, initialdir=self.folder.get() or None)
        if path:
            self.folder.set(os.path.normpath(path))

    def _ok(self) -> None:
        email = self.email.get().strip()
        if not EMAIL_RE.match(email):
            messagebox.showerror("Invalid email", "Enter a valid email address.", parent=self)
            return
        folder = self.folder.get().strip()
        if folder and not Path(folder).is_dir():
            messagebox.showerror("Invalid folder", "Folder does not exist.", parent=self)
            return
        self.result = {"email": email, "folder": folder}
        self.destroy()


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Claude Launcher")
        self.minsize(580, 360)
        self.cfg = Config(data_root() / "config.json")

        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        ttk.Label(root, text="Claude Code accounts:").grid(row=0, column=0, sticky="w")

        cols = ("email", "status", "folder")
        self.tree = ttk.Treeview(root, columns=cols, show="headings", selectmode="browse")
        self.tree.heading("email", text="Account")
        self.tree.heading("status", text="Claude status")
        self.tree.heading("folder", text="Opens folder")
        self.tree.column("email", width=200)
        self.tree.column("status", width=200)
        self.tree.column("folder", width=180)
        self.tree.grid(row=1, column=0, sticky="nsew")
        sb = ttk.Scrollbar(root, orient="vertical", command=self.tree.yview)
        sb.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<Double-1>", lambda _e: self.start())
        self.tree.bind("<Return>", lambda _e: self.start())
        self.tree.bind("<Delete>", lambda _e: self.remove())

        side = ttk.Frame(root)
        side.grid(row=1, column=2, sticky="n", padx=(10, 0))
        ttk.Button(side, text="Add…", command=self.add).pack(fill="x", pady=2)
        ttk.Button(side, text="Edit…", command=self.edit).pack(fill="x", pady=2)
        ttk.Button(side, text="Remove", command=self.remove).pack(fill="x", pady=2)
        ttk.Separator(side).pack(fill="x", pady=8)
        ttk.Button(side, text="Data folder", command=self.open_data_folder).pack(fill="x", pady=2)
        ttk.Button(side, text="Refresh", command=self.refresh).pack(fill="x", pady=2)

        bottom = ttk.Frame(root)
        bottom.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        bottom.columnconfigure(1, weight=1)
        self.path_labels: dict[str, ttk.Label] = {}
        for row, (attr, app) in enumerate(APPS.items()):
            ttk.Label(bottom, text=f"{app['label']}:").grid(row=row, column=0, sticky="w")
            self.path_labels[attr] = ttk.Label(bottom, foreground="gray")
            self.path_labels[attr].grid(row=row, column=1, sticky="w", padx=4)
            ttk.Button(bottom, text="Change…", command=lambda a=attr: self.choose_app(a)) \
                .grid(row=row, column=2, pady=1)

        starts = ttk.Frame(root)
        starts.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        starts.columnconfigure((0, 1), weight=1, uniform="start")
        ttk.Button(starts, text="▶  Start VS Code", command=self.start) \
            .grid(row=0, column=0, sticky="ew", padx=(0, 4), ipady=6)
        ttk.Button(starts, text="▶  Start Claude Desktop", command=self.start_desktop) \
            .grid(row=0, column=1, sticky="ew", padx=(4, 0), ipady=6)

        for attr in APPS:
            self.ensure_app(attr, interactive=False)
        self.refresh()
        self.bind("<FocusIn>", self._on_focus)

    # ---- helpers

    def _on_focus(self, event: tk.Event) -> None:
        # Login state changes while VS Code runs; refresh when user comes back.
        if event.widget is self:
            self.refresh()

    def selected(self) -> dict | None:
        sel = self.tree.selection()
        return self.cfg.find(sel[0]) if sel else None

    def refresh(self) -> None:
        keep = self.tree.selection()
        keep = keep[0] if keep else self.cfg.last_selected
        self.tree.delete(*self.tree.get_children())
        for acc in self.cfg.accounts:
            actual = signed_in_email(acc)
            if actual and actual.lower() != acc["email"].lower():
                status = f"⚠ signed in as {actual}"
            elif actual or has_credentials(acc):
                status = "✓ signed in"
            else:
                status = "not signed in (log in on start)"
            self.tree.insert("", "end", iid=acc["id"],
                             values=(acc["email"], status, acc.get("folder") or "—"))
        if keep and self.tree.exists(keep):
            self.tree.selection_set(keep)
            self.tree.see(keep)
        elif self.cfg.accounts:
            self.tree.selection_set(self.cfg.accounts[0]["id"])
        for attr, label in self.path_labels.items():
            label.configure(text=getattr(self.cfg, attr) or "not found")

    def ensure_app(self, attr: str, interactive: bool = True) -> str | None:
        """Return the cached executable path for an app, finding it once if needed."""
        path = getattr(self.cfg, attr)
        if path and Path(path).is_file():
            return path
        path = APPS[attr]["find"]()
        if path:
            setattr(self.cfg, attr, path)
            self.cfg.save()
            return path
        if interactive:
            messagebox.showinfo(APP_NAME, f"{APPS[attr]['label']} was not found automatically. "
                                          "Please locate it.")
            return self.choose_app(attr)
        return None

    def choose_app(self, attr: str) -> str | None:
        app = APPS[attr]
        types = [(app["label"], app["patterns"]), ("All files", "*.*")] \
            if sys.platform == "win32" else [("All files", "*")]
        path = filedialog.askopenfilename(parent=self, title=f"Locate {app['label']}",
                                          filetypes=types)
        if not path:
            return None
        setattr(self.cfg, attr, os.path.normpath(path))
        self.cfg.save()
        self.refresh()
        return getattr(self.cfg, attr)

    def _email_taken(self, email: str, except_id: str | None = None) -> bool:
        return any(a["email"].lower() == email.lower() and a["id"] != except_id
                   for a in self.cfg.accounts)

    # ---- actions

    def add(self) -> None:
        dlg = AccountDialog(self, "Add account")
        if not dlg.result:
            return
        if self._email_taken(dlg.result["email"]):
            messagebox.showerror(APP_NAME, "That account already exists.")
            return
        acc = {"id": uuid.uuid4().hex[:12], **dlg.result}
        self.cfg.accounts.append(acc)
        self.cfg.last_selected = acc["id"]
        self.cfg.save()
        self.tree.selection_set(())
        self.refresh()

    def edit(self) -> None:
        acc = self.selected()
        if not acc:
            return
        dlg = AccountDialog(self, "Edit account", acc)
        if not dlg.result:
            return
        if self._email_taken(dlg.result["email"], except_id=acc["id"]):
            messagebox.showerror(APP_NAME, "That account already exists.")
            return
        acc.update(dlg.result)  # id unchanged -> profile directory stays the same
        self.cfg.save()
        self.refresh()

    def remove(self) -> None:
        acc = self.selected()
        if not acc:
            return
        answer = messagebox.askyesnocancel(
            "Remove account",
            f"Remove {acc['email']} from the list?\n\n"
            "Yes = remove and DELETE its saved login and VS Code profile data\n"
            "No = remove from list only (data stays on disk)",
            icon="warning",
        )
        if answer is None:
            return
        if answer:
            try:
                for path in account_dirs(acc):
                    if path.exists():
                        shutil.rmtree(path)
            except OSError as exc:
                messagebox.showerror(APP_NAME, "Could not delete data "
                                               f"(is VS Code or Claude Desktop still open?):\n{exc}")
                return
        self.cfg.accounts.remove(acc)
        if self.cfg.last_selected == acc["id"]:
            self.cfg.last_selected = None
        self.cfg.save()
        self.tree.selection_set(())
        self.refresh()

    def open_data_folder(self) -> None:
        acc = self.selected()
        path = profile_dir(acc) if acc else data_root()
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])

    def start(self) -> None:
        self._start("vscode_path")

    def start_desktop(self) -> None:
        self._start("desktop_path")

    def _start(self, attr: str) -> None:
        acc = self.selected()
        if not acc:
            messagebox.showinfo(APP_NAME, "Select an account first.")
            return
        exe = self.ensure_app(attr)
        if not exe:
            return
        try:
            APPS[attr]["launch"](exe, acc)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Failed to start {APPS[attr]['label']}:\n{exc}")
            return
        self.cfg.last_selected = acc["id"]
        self.cfg.save()


# Launchable apps, keyed by the Config attribute caching their executable path.
APPS = {
    "vscode_path": {"label": "VS Code", "find": find_vscode, "launch": launch_vscode,
                    "patterns": "Code.exe code.cmd"},
    "desktop_path": {"label": "Claude Desktop", "find": find_desktop, "launch": launch_desktop,
                     "patterns": "claude-desktop.exe claude.exe Claude.exe"},
}


def main() -> None:
    if sys.platform == "win32":
        try:  # crisp text on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    App().mainloop()


if __name__ == "__main__":
    main()
