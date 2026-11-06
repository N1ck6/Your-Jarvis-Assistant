"""The program's own exe: a copy of the venv's Python interpreter (pythonw) with the program's name and icon.

Windows Task Manager names a process by its exe: under pythonw.exe the program shows as "Python" (Processes) and
"pythonw.exe" (Startup apps) with the Python icon. Started through <venv>\\Scripts\\<Name>.exe it shows its own
name and icon, and it is one process (the venv's pythonw.exe is a launcher that starts the real interpreter as a
second one). The copy finds the venv by pyvenv.cfg one folder up, like any venv interpreter; the interpreter's DLLs
are copied next to it. Name and icon are written by Windows SDK rc.exe; without the SDK nothing is made (None) and
the program keeps running under pythonw.exe. Rebuilt when the base Python is updated or the icon changes.
"""
from __future__ import annotations

import ctypes
import glob
import os
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

RT_ICON, RT_GROUP_ICON, RT_VERSION = 3, 14, 16
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _base_python(scripts: Path) -> Path | None:
    cfg = scripts.parent / "pyvenv.cfg"
    try:
        for line in cfg.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            if k.strip() == "home":
                return Path(v.strip())
    except OSError:
        pass
    return None


def _rc_exe() -> Path | None:
    kits = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Windows Kits" / "10" / "bin"
    found = sorted(glob.glob(str(kits / "10.*" / "x64" / "rc.exe")))
    return Path(found[-1]) if found else None


def _icon(src: Path, out: Path) -> None:
    """Every size Windows asks for (an icon with only 256 px is scaled down badly in lists)."""
    from PIL import Image
    im = Image.open(src)
    if getattr(im, "info", {}).get("sizes"):
        im.size = max(im.info["sizes"])
    im = im.convert("RGBA")
    im.save(out, sizes=[(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)])


def _rc_text(name: str, company: str) -> str:
    q = lambda s: s.replace('"', '""')  # noqa: E731
    vals = {"FileDescription": name, "ProductName": name, "InternalName": name, "CompanyName": company,
            "FileVersion": "1.0.0.0", "ProductVersion": "1.0.0.0"}
    body = "\n".join(f'            VALUE "{k}", "{q(v)}"' for k, v in vals.items())
    return f'''#pragma code_page(65001)
1 ICON "app.ico"
1 VERSIONINFO
FILEVERSION 1,0,0,0
PRODUCTVERSION 1,0,0,0
FILEOS 0x4
FILETYPE 0x1
BEGIN
    BLOCK "StringFileInfo"
    BEGIN
        BLOCK "040904B0"
        BEGIN
{body}
        END
    END
    BLOCK "VarFileInfo"
    BEGIN
        VALUE "Translation", 0x409, 1200
    END
END
'''


def _res_entries(res: bytes):
    """(type, name, lang, data) from a compiled .res (numeric types and names only - what rc makes here)."""
    i = 0
    while i + 8 <= len(res):
        size, hsize = struct.unpack_from("<II", res, i)
        h = res[i + 8:i + hsize]
        j = 0

        def ident():
            nonlocal j
            if struct.unpack_from("<H", h, j)[0] == 0xFFFF:
                v = struct.unpack_from("<H", h, j + 2)[0]
                j += 4
                return v
            end = h.index(b"\0\0", j)
            end += (end - j) % 2
            s = h[j:end].decode("utf-16-le")
            j = end + 2
            return s
        typ, name = ident(), ident()
        j += (4 - (j + 8) % 4) % 4
        _, _, lang = struct.unpack_from("<IHH", h, j)
        data = res[i + hsize:i + hsize + size]
        if size:
            yield typ, name, lang, data
        i += hsize + size
        i += (4 - i % 4) % 4


def _existing(exe: Path, types) -> list[tuple[int, int, int]]:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.LoadLibraryExW.restype = ctypes.c_void_p
    k32.LoadLibraryExW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint32]
    k32.FreeLibrary.argtypes = [ctypes.c_void_p]
    NP = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
    LP = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ushort,
                            ctypes.c_void_p)
    k32.EnumResourceNamesW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, NP, ctypes.c_void_p]
    k32.EnumResourceLanguagesW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, LP, ctypes.c_void_p]
    h = k32.LoadLibraryExW(str(exe), None, 0x2)                  # LOAD_LIBRARY_AS_DATAFILE
    out = []
    try:
        for t in types:
            names = []
            k32.EnumResourceNamesW(h, t, NP(lambda _h, _t, n, _p: names.append(n) or True), None)
            for n in names:
                if n is None or n >> 16:                             # string names: none in python.exe
                    continue
                k32.EnumResourceLanguagesW(h, t, n, LP(lambda _h, _t, _n, lg, _p: out.append((t, n, lg)) or True),
                                           None)
    finally:
        k32.FreeLibrary(h)
    return out


def _write_resources(exe: Path, res: bytes) -> None:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.BeginUpdateResourceW.restype = ctypes.c_void_p
    k32.BeginUpdateResourceW.argtypes = [ctypes.c_wchar_p, ctypes.c_bool]
    k32.UpdateResourceW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ushort,
                                    ctypes.c_void_p, ctypes.c_uint32]
    k32.EndUpdateResourceW.argtypes = [ctypes.c_void_p, ctypes.c_bool]
    old = _existing(exe, (RT_ICON, RT_GROUP_ICON, RT_VERSION))
    h = k32.BeginUpdateResourceW(str(exe), False)
    if not h:
        raise OSError(ctypes.get_last_error(), "BeginUpdateResource")
    ok = True
    for t, n, lg in old:                                             # Python's icons and version info out
        ok &= bool(k32.UpdateResourceW(h, t, n, lg, None, 0))
    for t, n, lg, data in _res_entries(res):
        if not isinstance(t, int) or not isinstance(n, int):
            continue
        buf = ctypes.create_string_buffer(data, len(data))
        ok &= bool(k32.UpdateResourceW(h, t, n, lg, buf, len(data)))
    if not k32.EndUpdateResourceW(h, not ok) or not ok:
        raise OSError(ctypes.get_last_error(), "UpdateResource")


def start(module: str, folder: str) -> None:
    """The exe was opened by itself (double click, no arguments): run the program as `cd folder && <exe> -m module`.
    Without this the interpreter has nothing to run and quietly exits. Called from the .pth line while Python is
    still starting (site), so it finishes the process itself."""
    import atexit
    import runpy
    import site
    import sys
    import threading

    for f in ("setquit", "setcopyright", "sethelper"):              # site would do these after the .pth files
        getattr(site, f, lambda: None)()
    os.chdir(folder)
    sys.argv = [module]
    code = 0
    try:
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else int(e.code is not None)
    except BaseException:  # noqa: BLE001
        code = 1
        try:
            import traceback
            Path(folder, f"{Path(sys.executable).stem}-start-error.txt").write_text(traceback.format_exc(),
                                                                                      encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    for f in (getattr(threading, "_shutdown", None), atexit._run_exitfuncs):   # what a normal exit runs
        try:
            if f:
                f()
        except BaseException:  # noqa: BLE001
            pass
    os._exit(code)


def _write_start(scripts: Path, file_name: str, module: str, folder: Path) -> None:
    """A .pth line in the venv's site-packages: every start of this venv's Python reads it, it only compares the exe
    name and the argument count, and calls start() when <file_name> was opened with no arguments."""
    exe = "\\" + file_name.lower()
    line = (f"import sys; len(getattr(sys, 'orig_argv', '..')) == 1 and sys.executable.lower().endswith({exe!r}) "
            f"and (sys.path.insert(0, {str(folder)!r}), __import__('importlib').import_module({__name__!r})"
            f".start({module!r}, {str(folder)!r}))\n")
    pth = scripts.parent / "Lib" / "site-packages" / f"zz_{Path(file_name).stem.lower()}_start.pth"
    if pth.parent.is_dir() and (not pth.exists() or pth.read_text(encoding="utf-8") != line):
        pth.write_text(line, encoding="utf-8")


def build(scripts: Path, file_name: str, name: str, icon: Path, company: str = "",
          run: tuple[str, Path] | None = None) -> Path | None:
    """<scripts>/<file_name> - the venv's interpreter under the program's name and icon. None: cannot be made.
    run = (module, project folder): opening the exe by itself (double click) starts that module from that folder."""
    scripts = Path(scripts)
    out = scripts / file_name
    if run:
        _write_start(scripts, file_name, run[0], Path(run[1]))
    base = _base_python(scripts)
    rc = _rc_exe()
    if not base or not rc or not (base / "pythonw.exe").exists():
        return out if out.exists() else None
    src = base / "pythonw.exe"
    old = out.with_name(f"{out.stem}.old{out.suffix}")
    try:
        old.unlink(missing_ok=True)                                  # set aside by a rebuild, closed since
    except OSError:
        pass
    stamp = out.with_suffix(".build")
    want = f"{src.stat().st_size}:{int(src.stat().st_mtime)}|{icon.stat().st_size}:{int(icon.stat().st_mtime)}|{name}"
    if out.exists() and stamp.exists() and stamp.read_text(encoding="utf-8") == want:
        return out
    for dll in base.glob("*.dll"):                                   # python3XX.dll, python3.dll, vcruntime
        d = scripts / dll.name
        if not d.exists() or d.stat().st_size != dll.stat().st_size:
            shutil.copy2(dll, d)
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        _icon(icon, t / "app.ico")
        (t / "app.rc").write_text(_rc_text(name, company), encoding="utf-8")
        r = subprocess.run([str(rc), "/nologo", "/fo", str(t / "app.res"), str(t / "app.rc")], cwd=t,
                           capture_output=True, text=True, creationflags=NO_WINDOW)
        if r.returncode:
            raise OSError(f"rc.exe: {r.stdout} {r.stderr}")
        exe = t / file_name
        shutil.copy2(src, exe)
        _write_resources(exe, (t / "app.res").read_bytes())
        try:
            os.replace(exe, out)
        except PermissionError:
            try:                                                     # running right now: a running exe can be
                os.replace(out, old)                                 # renamed, not overwritten; the new one is
                os.replace(exe, out)                                 # used from the next start
            except OSError:
                return out if out.exists() else None
    stamp.write_text(want, encoding="utf-8")
    return out
