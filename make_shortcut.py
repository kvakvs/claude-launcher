"""Create "Claude Launcher.lnk" (Windows) that can be pinned to Start / taskbar.

    python make_shortcut.py                # shortcut in this folder
    python make_shortcut.py --start-menu   # also add it to the Start menu

The shortcut runs pythonw.exe (no console window) and carries the same
AppUserModelID the app sets at runtime, so the pinned taskbar button and the
running window are one button instead of being grouped with other Python apps.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from claude_launcher import APP_USER_MODEL_ID, ICON_PATH

HERE = Path(__file__).resolve().parent
SHORTCUT_NAME = "Claude Launcher.lnk"

# IShellLink + IPropertyStore: WScript.Shell cannot set System.AppUserModel.ID.
CREATE_LNK_CS = r"""
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;

public static class LnkWriter {
    [ComImport, Guid("000214F9-0000-0000-C000-000000000046"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IShellLinkW {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder f, int c, IntPtr fd, uint fl);
        void GetIDList(out IntPtr p);
        void SetIDList(IntPtr p);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder s, int c);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string s);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder s, int c);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string s);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder s, int c);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string s);
        void GetHotkey(out short h);
        void SetHotkey(short h);
        void GetShowCmd(out int c);
        void SetShowCmd(int c);
        void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder s, int c, out int i);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string s, int i);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string s, uint r);
        void Resolve(IntPtr h, uint f);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string s);
    }

    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IPropertyStore {
        void GetCount(out uint c);
        void GetAt(uint i, out PropertyKey k);
        void GetValue(ref PropertyKey k, out PropVariant v);
        void SetValue(ref PropertyKey k, ref PropVariant v);
        void Commit();
    }

    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    struct PropertyKey { public Guid fmtid; public uint pid; }

    [StructLayout(LayoutKind.Explicit)]
    struct PropVariant {
        [FieldOffset(0)] public ushort vt;
        [FieldOffset(8)] public IntPtr p;
        [FieldOffset(16)] public long pad;
    }

    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    class ShellLink {}

    public static void Create(string lnk, string target, string args, string workDir,
                              string icon, string description, string appId) {
        var link = (IShellLinkW)new ShellLink();
        link.SetPath(target);
        link.SetArguments(args);
        link.SetWorkingDirectory(workDir);
        link.SetIconLocation(icon, 0);
        link.SetDescription(description);

        var store = (IPropertyStore)link;
        var key = new PropertyKey {
            fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), pid = 5 };
        var value = new PropVariant { vt = 31 /* VT_LPWSTR */,
                                      p = Marshal.StringToCoTaskMemUni(appId) };
        try {
            store.SetValue(ref key, ref value);
            store.Commit();
        } finally {
            Marshal.FreeCoTaskMem(value.p);
        }
        ((IPersistFile)link).Save(lnk, true);
    }
}
"""

PS_SCRIPT = """
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition $env:CL_CS
[LnkWriter]::Create($env:CL_LNK, $env:CL_TARGET, $env:CL_ARGS, $env:CL_WORKDIR,
                    $env:CL_ICON, $env:CL_DESC, $env:CL_APPID)
"""


def make_icon(path: Path) -> None:
    """Draw the app icon: slate tile, orange play triangle, white sparkle."""
    from PIL import Image, ImageDraw  # only needed to (re)generate the icon

    s = 1024  # draw large, downsample for anti-aliasing
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((32, 32, s - 32, s - 32), radius=220, fill=(43, 42, 51, 255))
    orange, tri, rr = (217, 119, 87, 255), [(330, 260), (330, 764), (770, 512)], 45
    d.polygon(tri, fill=orange)  # rounded triangle: thick outline + round caps
    d.line(tri + tri[:1], fill=orange, width=rr * 2)
    for x, y in tri:
        d.ellipse((x - rr, y - rr, x + rr, y + rr), fill=orange)
    cx, cy, r, w = 760, 250, 150, 38  # four-point sparkle
    d.polygon([(cx, cy - r), (cx + w, cy - w), (cx + r, cy), (cx + w, cy + w),
               (cx, cy + r), (cx - w, cy + w), (cx - r, cy), (cx - w, cy - w)],
              fill=(255, 255, 255, 255))
    img = img.resize((256, 256), Image.LANCZOS)
    img.save(path, sizes=[(n, n) for n in (16, 20, 24, 32, 40, 48, 64, 128, 256)])


def pythonw() -> str:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate if candidate.is_file() else exe)


def create_shortcut(lnk: Path) -> None:
    env = dict(os.environ,
               CL_CS=CREATE_LNK_CS,
               CL_LNK=str(lnk),
               CL_TARGET=pythonw(),
               CL_ARGS=f'"{HERE / "claude_launcher.py"}"',
               CL_WORKDIR=str(HERE),
               CL_ICON=str(ICON_PATH),
               CL_DESC="Start VS Code / Claude Desktop with a chosen Claude account",
               CL_APPID=APP_USER_MODEL_ID)
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", PS_SCRIPT],
                   env=env, check=True)


def main() -> None:
    if sys.platform != "win32":
        sys.exit("Windows only.")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start-menu", action="store_true",
                        help="also place the shortcut in the Start menu")
    parser.add_argument("--icon", action="store_true", help="regenerate the icon")
    args = parser.parse_args()

    if args.icon or not ICON_PATH.exists():
        make_icon(ICON_PATH)
        print(f"Icon:     {ICON_PATH}")

    targets = [HERE / SHORTCUT_NAME]
    if args.start_menu:
        targets.append(Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu"
                       / "Programs" / SHORTCUT_NAME)
    for lnk in targets:
        create_shortcut(lnk)
        print(f"Shortcut: {lnk}")


if __name__ == "__main__":
    main()
