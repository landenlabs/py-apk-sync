#!/usr/bin/env python3
"""
apk_sync.py - sync user-installed apps (base + split APKs, and optionally the
private cache/data of *debuggable* apps) between Android devices via adb.

Runs on macOS, Linux and Windows (Python 3.9+, standard library only).

An ENDPOINT is either
  * an adb serial      R52X90F9TKF  (USB)   or  192.168.1.42:5555  (Wi-Fi)
  * a local folder     ./snapshot   C:\\apks   (contains a path separator)

Examples
  python3 apk_sync.py devices
  python3 apk_sync.py check SERIAL_B                 # settings that block installs
  python3 apk_sync.py -n  SERIAL_A SERIAL_B          # dry run
  python3 apk_sync.py     SERIAL_A 192.168.1.42:5555 # one way
  python3 apk_sync.py -b  SERIAL_A SERIAL_B          # both ways
  python3 apk_sync.py --cache SERIAL_A ./snapshot    # + cache of debug apps
  python3 apk_sync.py -I --cache SERIAL_A ./snapshot # rerun: only what changed
  python3 apk_sync.py ./snapshot SERIAL_B
  python3 apk_sync.py --packages SERIAL_A            # list apps, versions, sizes
  python3 apk_sync.py --packages -v SERIAL_A ./snap  # + debuggable / cache columns

Pulls from the source and installs on the destination run in parallel,
pipelined: while app N installs on DST, apps N+1.. are being pulled from SRC.

Default comparison is by versionCode only, and --cache/--data copy the whole
cache/data. With -I/--incremental it works like rsync:
  APKs  same versionCode is re-checked: base + feature split sizes, and sha256
        for debuggable apps (all apps with -c), so a rebuilt debug APK that kept
        its versionCode is reinstalled. Config splits (abi/dpi/lang) are ignored
        because they legitimately differ per device.
  data  per-file size+mtime manifest on both sides; only new/changed files are
        transferred, files gone from SRC are deleted on DST (unless --no-delete).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

PKG_RE = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)+$")
ENTRY_RE = re.compile(r"^[A-Za-z0-9_.+-]+$")
# never copied with --data: rebuilt by the system, CPU/device specific, or
# (no_backup) per-install identifiers such as Firebase installation IDs
DATA_SKIP = {"code_cache", "lib", "no_backup"}
STAGE_DIR = "/data/local/tmp"

_print_lock = threading.Lock()
T0 = time.time()


def log(msg: str = "") -> None:
    with _print_lock:
        print(msg, flush=True)


def plog(pkg: str, msg: str) -> None:
    log(f"  [{time.time() - T0:6.1f}s] {pkg:<48} {msg}")


def warn(msg: str) -> None:
    with _print_lock:
        print(f"warning: {msg}", file=sys.stderr, flush=True)


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


# ------------------------------------------------------------------ adb lookup

def find_adb(explicit: str | None) -> str:
    cands = [explicit, os.environ.get("ADB"), shutil.which("adb")]
    exe = "adb.exe" if os.name == "nt" else "adb"
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(var):
            cands.append(os.path.join(os.environ[var], "platform-tools", exe))
    home = Path.home()
    cands += [
        str(home / "Library/Android/sdk/platform-tools" / exe),                     # macOS
        str(home / "Android/Sdk/platform-tools" / exe),                             # Linux
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android/Sdk/platform-tools", exe),  # Windows
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    die("adb not found; install Android SDK platform-tools, add it to PATH, or pass --adb")
    return ""  # unreachable


def run(cmd: list[str], timeout: float | None = None, stdout=subprocess.PIPE,
        stdin: bytes | None = None) -> subprocess.CompletedProcess:
    if stdin is None:
        return subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=stdout,
                              stderr=subprocess.PIPE, timeout=timeout)
    return subprocess.run(cmd, input=stdin, stdout=stdout, stderr=subprocess.PIPE, timeout=timeout)


def text(b: bytes | None) -> str:
    return (b or b"").decode("utf-8", "replace").replace("\r", "")


def is_folder(ep: str) -> bool:
    return os.path.isdir(ep) or "/" in ep or "\\" in ep or ep.startswith(".")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_rel(name: str) -> str | None:
    """Normalize a data-relative path; None if unsafe or out of scope."""
    name = name.lstrip("/")
    while name.startswith("./"):
        name = name[2:]
    name = name.rstrip("/")
    parts = name.split("/")
    if not name or ".." in parts or "" in parts or not ENTRY_RE.match(parts[0]) or parts[0] in DATA_SKIP:
        return None
    if any("\n" in p for p in parts):
        return None
    return name


def split_tar(tar_path: Path | None, out_path: Path) -> tuple[list[str], int, int]:
    """Copy file/symlink members of tar_path into out_path (validated names).
    Return (dirs, n_files, n_bytes). Directories are created separately with
    mkdir so they inherit setgid / the *_cache group on the device."""
    dirs, n, size = [], 0, 0
    with tarfile.open(out_path, "w", format=tarfile.GNU_FORMAT) as out:
        if tar_path is None:
            return dirs, 0, 0
        with tarfile.open(tar_path) as t:
            for m in t.getmembers():
                rel = safe_rel(m.name)
                if rel is None or not (m.isfile() or m.isdir() or m.issym()):
                    continue
                m.name = rel
                if m.isdir():
                    dirs.append(rel)
                    continue
                out.addfile(m, t.extractfile(m) if m.isfile() else None)
                n += 1
                size += m.size
    return dirs, n, size


# ------------------------------------------------------------------ data plan

@dataclass
class DataPlan:
    full: bool                       # copy everything (no manifest comparison)
    tops: list[str]                  # top-level entries in scope
    copy: list[str] | None = None    # incremental: files/symlinks to transfer
    mkdirs: list[str] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)
    nbytes: int = 0

    def empty(self) -> bool:
        return not self.full and not self.copy and not self.mkdirs and not self.delete

    def describe(self, mode: str) -> str:
        if self.full:
            return f"+{mode} (full)"
        if self.empty():
            return f"{mode} unchanged"
        bits = [f"{len(self.copy or [])} file(s) {human(self.nbytes)}"]
        if self.mkdirs:
            bits.append(f"{len(self.mkdirs)} dir(s)")
        if self.delete:
            bits.append(f"{len(self.delete)} deleted")
        return f"+{mode} " + ", ".join(bits)


def diff_manifest(src: dict, dst: dict, no_delete: bool) -> DataPlan:
    """src/dst: rel -> (kind, size, mtime), kind in f/d/l."""
    copy, mkdirs, delete, nbytes = [], [], [], 0
    for p, (k, s, m) in src.items():
        d = dst.get(p)
        if k == "d":
            if d is None or d[0] != "d":
                mkdirs.append(p)
            if d is not None and d[0] != "d":
                delete.append(p)
            continue
        if d is not None and d[0] != k:
            delete.append(p)
            d = None
        if d is None or d[1] != s or (k == "f" and d[2] != m):
            copy.append(p)
            nbytes += s
    if not no_delete:
        delete += [p for p in dst if p not in src]
    # top-level entries are never removed (cache/ carries setgid + cache gid);
    # drop paths whose ancestor is already being deleted
    dset = {p for p in delete if "/" in p}
    delete = sorted(p for p in dset if not any(p.startswith(a + "/") for a in _ancestors(p) if a in dset))
    tops = sorted({p.split("/")[0] for p in list(src) + list(dst)})
    return DataPlan(False, tops, sorted(copy), sorted(mkdirs, key=lambda x: x.count("/")), delete, nbytes)


def _ancestors(p: str):
    parts = p.split("/")
    for i in range(1, len(parts)):
        yield "/".join(parts[:i])


def apk_same(a: dict | None, b: dict | None) -> bool:
    """Compare base + feature splits by size (and sha256 when both known)."""
    if not a or not b:
        return True  # no identity available: trust versionCode
    def core(files):
        return {n: v for n, v in files.items() if not n.startswith("split_config.")}
    ca, cb = core(a["files"]), core(b["files"])
    if ca.keys() != cb.keys():
        return False
    for n in ca:
        (sa, ha), (sb, hb) = ca[n], cb[n]
        if sa != sb or (ha and hb and ha != hb):
            return False
    return True


# ------------------------------------------------------------------ endpoints

class Device:
    is_device = True

    def __init__(self, adb: str, serial: str):
        self.adb, self.serial = adb, serial
        self.sdk = 0
        self.model = self.abi = ""

    def cmd(self, *args: str) -> list[str]:
        return [self.adb, "-s", self.serial, *args]

    def sh(self, command: str, timeout: float = 300) -> str:
        p = run(self.cmd("shell", command), timeout=timeout)
        return text(p.stdout) + text(p.stderr)

    def sh_in(self, command: str, script: str, timeout: float = 1800) -> str:
        """Run command on the device with script fed on stdin (e.g. 'sh -s')."""
        p = run(self.cmd("shell", command), timeout=timeout, stdin=script.encode())
        return text(p.stdout) + text(p.stderr)

    def connect(self) -> None:
        def state() -> str:
            return text(run(self.cmd("get-state"), timeout=15).stdout).strip()
        st = state()
        if st != "device" and ":" in self.serial:
            log(f"connecting to {self.serial} ...")
            run([self.adb, "connect", self.serial], timeout=30)
            st = state()
        if st != "device":
            die(f"device '{self.serial}' not available (state: {st or 'none'}); see: adb devices -l")
        self.model = self.sh("getprop ro.product.model").strip()
        self.abi = self.sh("getprop ro.product.cpu.abi").strip()
        try:
            self.sdk = int(self.sh("getprop ro.build.version.sdk").strip())
        except ValueError:
            self.sdk = 0

    def label(self) -> str:
        return f"{self.serial} ({self.model}, SDK {self.sdk}, {self.abi})"

    def storage_locked(self) -> bool:
        """True if user 0 has not been unlocked since boot: credential-encrypted
        app data (/data/user/0) is unavailable, even to run-as."""
        return "0=RUNNING_LOCKED" in self.sh("dumpsys user | grep -m1 'Started users state'", timeout=30)

    # ---- preflight: device settings that can block or stall `adb install`

    def _user0_restrictions(self) -> set[str]:
        """Effective user restrictions of user 0 plus device-wide policy ones,
        parsed from `dumpsys user` (guest/profile defaults are ignored)."""
        out = self.sh("dumpsys user", timeout=30)
        found: set[str] = set()
        in_user0 = False
        section = None   # indent of the "...restrictions:" header being read
        for line in out.split("\n"):
            s = line.strip()
            ind = len(line) - len(line.lstrip())
            if s.startswith("UserInfo{"):
                in_user0 = s.startswith("UserInfo{0:")
                section = None
                continue
            if section is not None:
                if s and ind > section:      # list item ("none" or no_xxx)
                    if re.fullmatch(r"no_[a-z_]+", s):
                        found.add(s)
                    continue
                section = None
            low = s.lower()
            if low.endswith("restrictions:") and "default" not in low and "guest" not in low and \
                    (in_user0 or low.startswith("device policy global")):
                section = ind
        return found

    def preflight(self) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        """Return (blockers, warnings); each item is (problem, how to fix it)."""
        script = "\n".join([
            "echo man=$(getprop ro.product.manufacturer)",
            "echo miui=$(getprop ro.miui.ui.version.name)$(getprop ro.mi.os.version.name)",
            "echo adbinstall=$(getprop persist.security.adbinstall)",
            "echo coloros=$(getprop ro.build.version.opporom)$(getprop ro.build.version.oplusrom)",
            "echo vivo=$(getprop ro.vivo.os.version)",
            "echo verify_adb=$(settings get global verifier_verify_adb_installs)",
            "echo verifier=$(settings get global package_verifier_enable)",
            "echo free=$(df -k /data | tail -1 | awk '{print $4}')",
            "echo owner=$(dumpsys device_policy 2>/dev/null | grep -A3 -m1 'Device Owner:' | grep -m1 -o 'admin=[^ ]*')",
        ])
        kv = dict(re.findall(r"^(\w+)=(.*)$", self.sh_in("sh -s", script, timeout=60), re.M))
        g = lambda k: kv.get(k, "").strip()  # noqa: E731
        man = g("man").lower()
        blockers: list[tuple[str, str]] = []
        warns: list[tuple[str, str]] = []

        r = self._user0_restrictions()
        if "no_install_apps" in r:
            blockers.append(("user restriction no_install_apps is set (MDM / device policy): "
                             "all installs are refused",
                             "remove it in the device-management app or ask the admin; "
                             "it can't be turned off from adb"))
        if "no_debugging_features" in r:
            blockers.append(("user restriction no_debugging_features is set: adb installs are refused",
                             "remove the policy in the device-management app or ask the admin"))
        if "no_install_unknown_sources" in r or "no_install_unknown_sources_globally" in r:
            warns.append(("user restriction no_install_unknown_sources is set; some Android "
                          "versions/OEMs also apply it to adb installs",
                          "remove the policy in the device-management app if installs fail "
                          "with INSTALL_FAILED_USER_RESTRICTED"))
        if g("owner"):
            warns.append((f"device is managed (Device Owner {g('owner')[6:]}); its policy may block installs",
                          "check Settings > Security > Device admin apps, or ask the admin"))

        if g("miui") or man in ("xiaomi", "redmi", "poco"):
            if g("adbinstall") != "1":
                blockers.append(("Xiaomi 'Install via USB' is off: adb installs fail with "
                                 "INSTALL_FAILED_USER_RESTRICTED",
                                 "Settings > Additional settings > Developer options > turn on "
                                 "'Install via USB' (needs a SIM card and a signed-in Mi account; "
                                 "for -g also turn on 'USB debugging (Security settings)')"))
        if g("coloros") or man in ("oppo", "realme", "oneplus"):
            warns.append(("ColorOS/OxygenOS can show an on-screen 'install via USB' prompt for "
                          "every app; an unanswered prompt ends in INSTALL_FAILED_ABORTED",
                          "watch the phone during the sync, or Settings > Additional settings > "
                          "Developer options > turn off 'Disable permission monitoring'/'Verify apps "
                          "over USB' and turn on 'Install via USB' where present"))
        if g("vivo") or man in ("vivo", "iqoo"):
            warns.append(("vivo/iQOO asks to confirm each USB install; unanswered prompts fail",
                          "Settings > System > Developer options > turn on 'Install via USB' and "
                          "turn off 'Monitor apps installed by ADB'; watch the phone during the sync"))

        if g("verify_adb") != "0" and g("verifier") != "0":
            warns.append(("Play Protect scans apps installed over adb; installs may be slow, "
                          "show a 'Blocked by Play Protect' prompt, or fail with "
                          "INSTALL_FAILED_VERIFICATION_FAILURE",
                          "Developer options > turn off 'Verify apps over USB', or run: "
                          f"adb -s {self.serial} shell settings put global verifier_verify_adb_installs 0"))

        try:
            free = int(g("free")) * 1024
            if free < 1024 ** 3:
                warns.append((f"only {human(free)} free on /data; installs fail with "
                              "INSTALL_FAILED_INSUFFICIENT_STORAGE when it runs out",
                              "free up space on the device"))
        except ValueError:
            pass
        return blockers, warns

    def packages(self) -> dict[str, int]:
        out = self.sh("pm list packages -3 --show-versioncode")
        found = re.findall(r"^package:(\S+) versionCode:(\d+)", out, re.M)
        if found:
            return {p: int(v) for p, v in found}
        # Android < 9 has no --show-versioncode
        res = {}
        for p in re.findall(r"^package:(\S+)", self.sh("pm list packages -3"), re.M):
            m = re.search(r"versionCode=(\d+)", self.sh(f"dumpsys package {p}"))
            res[p] = int(m.group(1)) if m else 0
        return res

    def apk_table(self) -> dict[str, dict]:
        """All user apps in 3 device commands (dumpsys, pm -f, one stat over every APK):
        pkg -> {code, name, date, installed, installer, target, debug_flag, dir, files{name: size}}."""
        if getattr(self, "_table", None) is not None:
            return self._table
        codes = self.packages()
        script = r"""
dumpsys package packages </dev/null | grep -E '^ +(Package \[|versionCode=|versionName=|lastUpdateTime=|firstInstallTime=|flags=|installerPackageName=)'
pm list packages -3 -f </dev/null | sed -n 's/^package:\(.*\)\/base\.apk=\(.*\)$/@@M \2 \1/p'
stat -c '@@F %s %n' $(pm list packages -3 -f </dev/null | sed -n 's/^package:\(.*\)\/base\.apk=.*$/\1\/*.apk/p')
"""
        info: dict[str, dict] = {p: {"pkg": p, "code": v, "files": {}} for p, v in codes.items()}
        dir2pkg: dict[str, str] = {}
        cur = None
        seen: set[str] = set()
        for line in self.sh_in("sh -s", script).split("\n"):
            if line.startswith("@@M "):
                parts = line[4:].split(" ", 1)
                if len(parts) == 2 and parts[0] in info:
                    dir2pkg[parts[1]] = parts[0]
                    info[parts[0]]["dir"] = parts[1]
                continue
            if line.startswith("@@F "):
                parts = line[4:].split(" ", 1)
                if len(parts) == 2 and parts[0].isdigit():
                    d, n = parts[1].rsplit("/", 1)
                    if d in dir2pkg:
                        info[dir2pkg[d]]["files"][n] = int(parts[0])
                continue
            ls = line.strip()
            m = re.match(r"Package \[([^\]]+)\]", ls)
            if m:
                # first section only (later ones are hidden/system copies)
                name = m.group(1)
                cur = info.get(name) if name not in seen else None
                seen.add(name)
                continue
            if cur is None:
                continue
            if ls.startswith("versionCode="):
                t = re.search(r"targetSdk=(\d+)", ls)
                if t:
                    cur["target"] = int(t.group(1))
            elif ls.startswith("versionName="):
                cur["name"] = ls.split("=", 1)[1]
            elif ls.startswith("lastUpdateTime="):
                cur["date"] = ls.split("=", 1)[1][:16]
            elif ls.startswith("firstInstallTime="):
                cur["installed"] = ls.split("=", 1)[1][:16]
            elif ls.startswith("installerPackageName="):
                cur["installer"] = ls.split("=", 1)[1]
            elif ls.startswith("flags="):
                cur["debug_flag"] = "DEBUGGABLE" in ls
        self._table = info
        return info

    def apk_identity(self, pkgs: list[str], checksum: bool, want_hash: set[str] = frozenset()) -> dict:
        """pkg -> {'dbg': bool, 'files': {apk name: (size, sha256 or None)}}.
        Sizes from apk_table(); sha256 (one sha256sum call) of base + feature splits for
        debuggable apps, apps in want_hash, or all apps with checksum."""
        table = self.apk_table()
        to_hash: list[str] = []
        for p in pkgs:
            e = table.get(p)
            if e and e.get("dir") and (checksum or e.get("debug_flag") or p in want_hash):
                to_hash += [f"{e['dir']}/{n}" for n in e["files"] if not n.startswith("split_config.")]
        hashes: dict[str, str] = {}
        if to_hash:
            out = self.sh_in("sh -s", "sha256sum " + " ".join(shlex.quote(f) for f in to_hash) + "\n", timeout=3600)
            for line in out.split("\n"):
                parts = line.split(None, 1)
                if len(parts) == 2 and len(parts[0]) == 64:
                    hashes[parts[1].strip()] = parts[0]
        res = {}
        for p in pkgs:
            e = table.get(p)
            if not e or not e["files"]:
                continue
            res[p] = {"dbg": bool(e.get("debug_flag")),
                      "files": {n: (sz, hashes.get(f"{e.get('dir')}/{n}")) for n, sz in e["files"].items()}}
        return res

    def is_debuggable(self, pkg: str) -> bool:
        return self.sh(f"run-as {pkg} id -u", timeout=30).strip().isdigit()

    def pkg_meta(self, pkg: str) -> dict:
        out = self.sh(f"dumpsys package {pkg} | grep -m3 -E '^ +(versionName|versionCode|lastUpdateTime)='")
        meta = {}
        m = re.search(r"versionName=(.*)", out)
        if m:
            meta["versionName"] = m.group(1).strip()
        m = re.search(r"targetSdk=(\d+)", out)
        if m:
            meta["targetSdk"] = int(m.group(1))
        m = re.search(r"lastUpdateTime=(.*)", out)
        if m:
            meta["lastUpdateTime"] = m.group(1).strip()
        return meta

    def force_stop(self, pkg: str) -> None:
        self.sh(f"am force-stop {pkg}", timeout=30)

    def fetch_apks(self, pkg: str, work: Path) -> list[Path]:
        paths = re.findall(r"^package:(\S+)", self.sh(f"pm path {pkg}"), re.M)
        if not paths:
            raise RuntimeError("pm path returned nothing")
        work.mkdir(parents=True, exist_ok=True)
        p = run(self.cmd("pull", *paths, str(work)), timeout=3600)
        if p.returncode != 0:
            raise RuntimeError("adb pull failed: " + text(p.stderr).strip()[-200:])
        files = [work / os.path.basename(x) for x in paths]
        missing = [f.name for f in files if not f.is_file()]
        if missing:
            raise RuntimeError(f"pull incomplete, missing {missing}")
        return files

    def install(self, files: list[Path], opts: argparse.Namespace) -> None:
        # -t: allow testOnly APKs (what Android Studio "Run" produces for debug builds)
        args = ["install" if len(files) == 1 else "install-multiple", "-r", "-t"]
        if opts.downgrade:
            args.append("-d")
        if opts.grant:
            args.append("-g")
        if opts.installer:
            args += ["-i", opts.installer]
        if self.sdk >= 34:  # Android 14+ blocks targetSdk < 23 otherwise
            args.append("--bypass-low-target-sdk-block")
        p = run(self.cmd(*args, *map(str, files)), timeout=3600)
        out = text(p.stdout) + text(p.stderr)
        if "Success" in out:
            return
        m = re.search(r"INSTALL_[A-Z_]+[^\]\n]*", out)
        reason = m.group(0) if m else (out.strip().splitlines() or ["unknown error"])[-1]
        hints = {
            "UPDATE_INCOMPATIBLE": "signed with a different key on DST; uninstall it there first (loses its data)",
            "NO_MATCHING_ABIS": "native code for SRC's CPU only; DST CPU differs",
            "OLDER_SDK": "needs a newer Android version than DST has",
            "MISSING_SPLIT": "split set incomplete (Play Feature/Asset Delivery?)",
            "USER_RESTRICTED": "blocked by a device setting; run: apk_sync.py check SERIAL",
            "VERIFICATION_FAILURE": "rejected by Play Protect; turn off Developer options > 'Verify apps over USB'",
            "INSUFFICIENT_STORAGE": "DST is out of space",
            "ABORTED": "install prompt declined or timed out on DST; watch the phone and accept it",
        }
        for k, h in hints.items():
            if k in reason:
                reason += f"  -> {h}"
        raise RuntimeError(reason)

    # ---- private data of debuggable apps, via run-as + toybox tar

    def data_tops(self, pkg: str, mode: str) -> list[str]:
        if not self.is_debuggable(pkg):
            return []
        names = [n for n in self.sh(f"run-as {pkg} ls -1a").split("\n") if n and n not in (".", "..")]
        if mode == "cache":
            names = [n for n in names if n == "cache"]
        else:
            names = [n for n in names if n not in DATA_SKIP]
        bad = [n for n in names if not ENTRY_RE.match(n)]
        if bad:
            warn(f"{pkg}: skipping data entries with unusual names: {bad}")
        return [n for n in names if ENTRY_RE.match(n)]

    def data_manifest(self, pkg: str, mode: str) -> dict:
        tops = self.data_tops(pkg, mode)
        if not tops:
            return {}
        out = self.sh(f"run-as {pkg} sh -c 'find {' '.join(tops)} -exec stat -c \"%f %s %Y %n\" {{}} +'")
        res = {}
        for line in out.split("\n"):
            parts = line.split(" ", 3)
            if len(parts) != 4:
                continue
            try:
                mode_bits, size, mtime = int(parts[0], 16), int(parts[1]), int(parts[2])
            except ValueError:
                continue
            rel = safe_rel(parts[3])
            if rel is None:
                continue
            kind = "d" if stat.S_ISDIR(mode_bits) else "f" if stat.S_ISREG(mode_bits) else \
                "l" if stat.S_ISLNK(mode_bits) else None
            if kind:
                res[rel] = (kind, size if kind != "d" else 0, mtime)
        return res

    def data_pull(self, pkg: str, plan: DataPlan, dest: Path) -> Path | None:
        """Tar the plan's files (or all tops when plan.full) from the device into dest."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        # exec-out is binary-clean; stderr must not leak into the stream
        if plan.full:
            if not plan.tops:
                return None
            remote = f"run-as {pkg} sh -c 'tar -cf - {' '.join(plan.tops)} 2>/dev/null'"
            lst = None
        else:
            if not plan.copy:
                return None
            lst = f"{STAGE_DIR}/apk_sync_{pkg}.lst"
            local_lst = dest.with_suffix(".lst")
            local_lst.write_bytes(("\n".join(plan.copy) + "\n").encode())
            if run(self.cmd("push", str(local_lst), lst), timeout=120).returncode != 0:
                raise RuntimeError("push of file list failed")
            remote = f"cat {lst} | run-as {pkg} sh -c 'tar -cf - -T - 2>/dev/null'"
        try:
            with open(dest, "wb") as f:
                run(self.cmd("exec-out", remote), timeout=3600, stdout=f)
        finally:
            if lst:
                self.sh(f"rm -f {lst}")
        try:
            with tarfile.open(dest) as t:
                t.getmembers()
        except tarfile.TarError as e:
            raise RuntimeError(f"data archive unreadable ({e}); is tar available on SRC?")
        return dest

    def data_apply(self, pkg: str, plan: DataPlan, tar_path: Path | None) -> None:
        """Apply plan on the device. tar runs as the app uid (run-as), so new files
        get the app's owner and SELinux label. Directories are created with mkdir,
        not by tar (toybox tar chmods dirs, and a non-member chmod clears setgid),
        so they inherit the setgid bit and *_cache group from cache/."""
        tmpd = Path(tempfile.mkdtemp(prefix="apk-sync-apply-"))
        staged = f"{STAGE_DIR}/apk_sync_{pkg}.tar"
        try:
            files_tar = tmpd / "files.tar"
            dirs, nfiles, _ = split_tar(tar_path, files_tar)
            q = shlex.quote
            script = []
            if plan.full:
                # empty each top dir (keep the dir itself), drop top-level files
                for n in plan.tops:
                    script.append(f"if [ -d {q(n)} ]; then find {q(n)} -mindepth 1 -delete; "
                                  f"else rm -f {q(n)}; fi")
            for p in plan.delete:
                script.append(f"rm -rf {q(p)}")
            for d in sorted(set(dirs) | set(plan.mkdirs), key=lambda x: x.count("/")):
                script.append(f"mkdir -p {q(d)}")
            if script:
                out = self.sh_in(f"run-as {pkg} sh -s", "\n".join(script) + "\necho __prep_ok__\n")
                if "__prep_ok__" not in out:
                    raise RuntimeError("prepare: " + (out.strip().splitlines() or ["failed"])[-1])
            if nfiles:
                p = run(self.cmd("push", str(files_tar), staged), timeout=3600)
                if p.returncode != 0:
                    raise RuntimeError("push of data archive failed: " + text(p.stderr).strip()[-200:])
                # cat runs as shell (it can read /data/local/tmp); tar runs as the app
                out = self.sh(f"cat {staged} | run-as {pkg} tar -xof - 2>&1")
                if out.strip():
                    raise RuntimeError("tar extract: " + out.strip().splitlines()[-1])
        finally:
            self.sh(f"rm -f {staged}")
            shutil.rmtree(tmpd, ignore_errors=True)


    # ---- package listing (--packages)

    def package_info(self, verbose: bool) -> list[dict]:
        """apk_table() (+ with verbose: parallel run-as du/find for debuggable apps)."""
        info = {p: dict(e) for p, e in self.apk_table().items()}
        for e in info.values():
            f = e.pop("files")
            e.pop("dir", None)
            if f:
                e.update(size=sum(f.values()), apks=len(f))
        if verbose:
            locked = self.storage_locked()
            if locked:
                warn(f"{self.serial}: phone not unlocked since boot; app data is encrypted and unreadable. "
                     "Unlock the screen once, then rerun for cache/data sizes.")
            dbg = [p for p, e in info.items() if e.get("debug_flag")]

            def sizes(pkg: str) -> tuple[str, str]:
                return pkg, self.sh(f"run-as {pkg} sh -c 'du -sk cache 2>/dev/null | cut -f1; "
                                    f"find cache -type f 2>/dev/null | wc -l; du -sk . 2>/dev/null | cut -f1'",
                                    timeout=120)
            if not locked:
                with ThreadPoolExecutor(8) as ex:  # one run-as per app, in parallel
                    for pkg, out in ex.map(sizes, dbg):
                        if out.startswith("run-as:"):
                            info[pkg]["runas_err"] = out.strip().splitlines()[0]
                            continue
                        nums = [int(x) for x in out.split() if x.isdigit()][:3] + [0, 0, 0]
                        info[pkg].update(cache_b=nums[0] * 1024, cache_files=nums[1], data_b=nums[2] * 1024)
            for e in info.values():
                e["debug"] = bool(e.get("debug_flag"))
                if locked and e["debug"]:
                    e["locked"] = True
        return list(info.values())


class Folder:
    """Snapshot folder: <root>/<pkg>/{*.apk, meta.json, data/<cache|files|...>}"""
    is_device = False

    def __init__(self, path: str):
        self.root = Path(path).expanduser()
        self.sdk = 0
        self.abi = ""

    def connect(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def label(self) -> str:
        return f"folder {self.root}"

    def _meta(self, pkg: str) -> dict:
        d = self.root / pkg
        if (d / "meta.json").is_file():
            try:
                return json.loads((d / "meta.json").read_text())
            except ValueError:
                return {}
        if (d / ".versionCode").is_file():  # written by the old apk-sync.sh
            return {"versionCode": int((d / ".versionCode").read_text().strip() or 0)}
        return {}

    def _write_meta(self, pkg: str, meta: dict) -> None:
        d = self.root / pkg
        (d / "meta.json").write_text(json.dumps(meta, indent=2))
        (d / ".versionCode").unlink(missing_ok=True)

    def packages(self) -> dict[str, int]:
        res = {}
        for d in sorted(self.root.iterdir()):
            if d.is_dir() and PKG_RE.match(d.name) and list(d.glob("*.apk")):
                res[d.name] = int(self._meta(d.name).get("versionCode", 0))
        return res

    def apk_identity(self, pkgs: list[str], checksum: bool, want_hash: set[str] = frozenset()) -> dict:
        """Sizes from the filesystem; sha256 cached in meta.json (keyed by size+mtime)."""
        res = {}
        for pkg in pkgs:
            meta = self._meta(pkg)
            dbg = bool(meta.get("debuggable"))
            cache = meta.get("apk", {})
            hp = checksum or dbg or pkg in want_hash
            files, dirty = {}, False
            for f in sorted((self.root / pkg).glob("*.apk")):
                st = f.stat()
                h = None
                if hp and not f.name.startswith("split_config."):
                    c = cache.get(f.name, {})
                    if c.get("size") == st.st_size and c.get("mtime_ns") == st.st_mtime_ns and c.get("sha256"):
                        h = c["sha256"]
                    else:
                        h = sha256_file(f)
                        cache[f.name] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": h}
                        dirty = True
                files[f.name] = (st.st_size, h)
            if dirty and (self.root / pkg / "meta.json").is_file():  # never convert old snapshots here
                meta["apk"] = cache
                self._write_meta(pkg, meta)
            if files:
                res[pkg] = {"dbg": dbg, "files": files}
        return res

    def is_debuggable(self, pkg: str) -> bool:
        return (self.root / pkg / "data").is_dir()

    def fetch_apks(self, pkg: str, work: Path) -> list[Path]:
        files = sorted((self.root / pkg).glob("*.apk"))
        if not files:
            raise RuntimeError("no APKs in folder")
        return files

    def store_apks(self, pkg: str, ver: int, files: list[Path], src, dbg: bool | None) -> None:
        d = self.root / pkg
        d.mkdir(parents=True, exist_ok=True)
        for old in d.glob("*.apk"):
            old.unlink()
        apk = {}
        for f in files:
            shutil.copy2(f, d / f.name)
            st = (d / f.name).stat()
            apk[f.name] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": sha256_file(d / f.name)}
        meta = self._meta(pkg)
        meta.update(versionCode=ver, saved=time.strftime("%Y-%m-%d %H:%M:%S"),
                    fromSdk=getattr(src, "sdk", 0), fromAbi=getattr(src, "abi", ""), apk=apk)
        if dbg is not None:
            meta["debuggable"] = dbg
        if getattr(src, "is_device", False):
            meta.update(src.pkg_meta(pkg))
        elif hasattr(src, "_meta"):  # folder -> folder is not allowed, but keep fields if present
            meta.update({k: v for k, v in src._meta(pkg).items() if k in ("versionName", "targetSdk")})
        self._write_meta(pkg, meta)

    # ---- data tree

    def _ddir(self, pkg: str) -> Path:
        return self.root / pkg / "data"

    def data_tops(self, pkg: str, mode: str) -> list[str]:
        dd = self._ddir(pkg)
        if not dd.is_dir():
            return []
        names = [p.name for p in dd.iterdir() if ENTRY_RE.match(p.name) and p.name not in DATA_SKIP]
        return [n for n in names if n == "cache"] if mode == "cache" else sorted(names)

    def data_manifest(self, pkg: str, mode: str) -> dict:
        dd, res = self._ddir(pkg), {}
        for top in self.data_tops(pkg, mode):
            base = dd / top
            stack = [base]
            while stack:
                p = stack.pop()
                rel = p.relative_to(dd).as_posix()
                st = p.lstat()
                if stat.S_ISLNK(st.st_mode):
                    res[rel] = ("l", len(os.readlink(p)), int(st.st_mtime))
                elif stat.S_ISDIR(st.st_mode):
                    res[rel] = ("d", 0, int(st.st_mtime))
                    stack.extend(p.iterdir())
                elif stat.S_ISREG(st.st_mode):
                    res[rel] = ("f", st.st_size, int(st.st_mtime))
        return res

    def data_pull(self, pkg: str, plan: DataPlan, dest: Path) -> Path | None:
        dd = self._ddir(pkg)
        names = plan.tops if plan.full else (plan.copy or [])
        if not names:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(dest, "w", format=tarfile.GNU_FORMAT) as t:
            for n in names:
                t.add(dd / n, arcname=n, recursive=plan.full)
        return dest

    def data_apply(self, pkg: str, plan: DataPlan, tar_path: Path | None) -> None:
        dd = self._ddir(pkg)
        dd.mkdir(parents=True, exist_ok=True)

        def rm(p: Path):
            if p.is_symlink() or p.is_file():
                p.unlink()
            elif p.is_dir():
                shutil.rmtree(p)

        if plan.full:
            for top in plan.tops:  # replace exactly the entries in scope
                rm(dd / top)
        for rel in plan.delete:
            rm(dd.joinpath(*rel.split("/")))
        for rel in plan.mkdirs:
            dd.joinpath(*rel.split("/")).mkdir(parents=True, exist_ok=True)
        if tar_path:
            with tarfile.open(tar_path) as t:
                for m in t.getmembers():
                    rel = safe_rel(m.name)
                    if rel is None or not (m.isfile() or m.isdir() or m.issym()):
                        continue
                    m.name = rel
                    target = dd.joinpath(*rel.split("/"))
                    if target.is_symlink() or target.is_file():
                        target.unlink()
                    try:
                        if hasattr(tarfile, "fully_trusted_filter"):  # 3.12+: we validated names
                            t.extract(m, dd, filter="fully_trusted")
                        else:
                            t.extract(m, dd)
                    except OSError as e:  # e.g. symlinks on Windows without privilege
                        warn(f"{pkg}: could not store {rel}: {e}")


    # ---- package listing (--packages)

    def package_info(self, verbose: bool) -> list[dict]:
        res = []
        for pkg, code in self.packages().items():
            d = self.root / pkg
            meta = self._meta(pkg)
            apks = list(d.glob("*.apk"))
            e = {"pkg": pkg, "code": code, "size": sum(f.stat().st_size for f in apks), "apks": len(apks),
                 "date": meta.get("saved", time.strftime("%Y-%m-%d %H:%M",
                                                         time.localtime(max(f.stat().st_mtime for f in apks))))[:16],
                 "name": meta.get("versionName", ""), "target": meta.get("targetSdk", "-")}
            if verbose:
                dd = self._ddir(pkg)
                e["debug"] = bool(meta.get("debuggable")) or dd.is_dir()
                if dd.is_dir():
                    def tree(p: Path):
                        n = b = 0
                        for root, _, fs in os.walk(p):
                            for f in fs:
                                fp = os.path.join(root, f)
                                if not os.path.islink(fp):
                                    n += 1
                                    b += os.path.getsize(fp)
                        return n, b
                    cn, cb = tree(dd / "cache") if (dd / "cache").is_dir() else (0, 0)
                    _, db = tree(dd)
                    e.update(cache_b=cb, cache_files=cn, data_b=db)
            res.append(e)
        return res


# ------------------------------------------------------------------ sync

@dataclass
class Item:
    pkg: str
    sver: int
    dver: int | None
    apk_action: str | None       # install / update / downgrade / reinstall / None
    want_data: bool
    notes: list[str] = field(default_factory=list)


def plan_apks(src, dst, opts, data_mode: str | None) -> tuple[list[Item], dict]:
    s, d = src.packages(), dst.packages()
    counts = dict(new=0, update=0, changed=0, identical=0, newer_on_dst=0)
    selected = [p for p in sorted(s) if PKG_RE.match(p)
                and not (opts.only and not re.search(opts.only, p))
                and not (opts.exclude and re.search(opts.exclude, p))]

    # incremental: same versionCode -> compare APK content (one adb call per device)
    changed: set[str] = set()
    same_ver = [p for p in selected if d.get(p) == s[p]]
    if opts.incremental and same_ver and not opts.missing_only:
        ck = opts.checksum
        if src.is_device and dst.is_device:
            with ThreadPoolExecutor(2) as ex:
                fa = ex.submit(src.apk_identity, same_ver, ck)
                fb = ex.submit(dst.apk_identity, same_ver, ck)
                ia, ib = fa.result(), fb.result()
        elif src.is_device:
            ia = src.apk_identity(same_ver, ck)
            ib = dst.apk_identity(same_ver, ck, {p for p, e in ia.items() if any(h for _, h in e["files"].values())})
        else:
            ib = dst.apk_identity(same_ver, ck)
            ia = src.apk_identity(same_ver, ck, {p for p, e in ib.items() if any(h for _, h in e["files"].values())})
        changed = {p for p in same_ver if not apk_same(ia.get(p), ib.get(p))}

    items = []
    for pkg in selected:
        sv, dv = s[pkg], d.get(pkg)
        if dv is None:
            act = "install"; counts["new"] += 1
        elif sv > dv:
            act = None if opts.missing_only else f"update {dv}->{sv}"
            counts["update" if act else "identical"] += 1
        elif sv < dv:
            counts["newer_on_dst"] += 1
            if not (opts.downgrade and not opts.missing_only):
                continue  # DST newer: neither APK nor data (version mismatch)
            act = f"downgrade {dv}->{sv}"
        elif pkg in changed:
            act = "reinstall (APK changed, same versionCode)"; counts["changed"] += 1
        else:
            act = None; counts["identical"] += 1
        # data only makes sense when DST ends up on exactly SRC's version
        want = bool(data_mode) and (act is not None or sv == dv)
        if act or want:
            items.append(Item(pkg, sv, dv, act, want))
    return items, counts


def data_plan(src, dst, i: Item, opts, mode: str) -> DataPlan:
    if src.is_device and mode == "data" and not opts.no_stop_src and not opts.dry_run:
        src.force_stop(i.pkg)  # flush SQLite/WAL, prefs before reading
    if not opts.incremental:
        tops = src.data_tops(i.pkg, mode)
        return DataPlan(True, tops) if tops else DataPlan(False, [])
    smf = src.data_manifest(i.pkg, mode)
    # a fresh install has nothing on DST: skip the DST scan
    dmf = dst.data_manifest(i.pkg, mode) if i.dver is not None else {}
    return diff_manifest(smf, dmf, opts.no_delete)


def sync_pass(src, dst, opts, data_mode: str | None, work_root: Path) -> list[str]:
    log("")
    log(f"=== {src.label()}  ->  {dst.label()}" + ("   [incremental]" if opts.incremental else ""))
    if src.is_device and dst.is_device and src.abi != dst.abi:
        warn(f"CPU ABI differs (SRC {src.abi}, DST {dst.abi}): apps with native splits may fail")

    items, counts = plan_apks(src, dst, opts, data_mode)
    src_dbg: dict[str, bool] = {}
    if data_mode:
        # debuggable check is a quick run-as per app; do it in parallel
        with ThreadPoolExecutor(max(4, opts.jobs)) as ex:
            src_dbg = dict(zip([i.pkg for i in items],
                               ex.map(lambda i: i.want_data and src.is_debuggable(i.pkg), items)))
        for i in items:
            i.want_data = i.want_data and src_dbg[i.pkg]
        items = [i for i in items if i.apk_action or i.want_data]

    def summary() -> str:
        return (f"--- new: {counts['new']}  updates: {counts['update']}  changed-same-version: {counts['changed']}"
                f"  identical: {counts['identical']}  newer-on-dst: {counts['newer_on_dst']}")

    failed: list[str] = []
    ok = unchanged = 0

    def fetch(i: Item):
        work = work_root / i.pkg
        files = src.fetch_apks(i.pkg, work) if i.apk_action and not opts.dry_run else None
        dp = tar = None
        if i.want_data:
            dp = data_plan(src, dst, i, opts, data_mode)
            if not dp.empty() and not opts.dry_run:
                tar = src.data_pull(i.pkg, dp, work / "data.tar")
        return files, dp, tar

    def put(i: Item, files, dp: DataPlan | None, tar) -> str:
        what = []
        if i.apk_action:
            what.append(i.apk_action + (f" ({len(files)} apk)" if files else ""))
        if dp is not None:
            what.append(dp.describe(data_mode))
        if opts.dry_run:
            return " ".join(what)
        try:
            if dst.is_device:
                if files:
                    dst.install(files, opts)
                if dp is not None and not dp.empty():
                    dst.force_stop(i.pkg)
                    if not dst.is_debuggable(i.pkg):
                        raise RuntimeError("app not debuggable on DST; cannot restore data")
                    dst.data_apply(i.pkg, dp, tar)
            else:
                if files:
                    dst.store_apks(i.pkg, i.sver, files, src, src_dbg.get(i.pkg) if data_mode else None)
                if dp is not None and not dp.empty():
                    dst.data_apply(i.pkg, dp, tar)
        finally:
            if not opts.keep and src.is_device:
                shutil.rmtree(work_root / i.pkg, ignore_errors=True)
        return " ".join(what + i.notes)

    pull_ex = ThreadPoolExecutor(opts.jobs, thread_name_prefix="pull")
    push_ex = ThreadPoolExecutor(opts.install_jobs, thread_name_prefix="push")
    try:
        pulls = {pull_ex.submit(fetch, i): i for i in items}
        pushes = {}
        for f in as_completed(pulls):
            i = pulls[f]
            try:
                files, dp, tar = f.result()
            except Exception as e:  # noqa: BLE001
                plog(i.pkg, f"FAIL fetch: {e}")
                failed.append(i.pkg)
                continue
            if not i.apk_action and (dp is None or dp.empty()):
                unchanged += 1
                if opts.verbose:
                    plog(i.pkg, "unchanged")
                continue
            pushes[push_ex.submit(put, i, files, dp, tar)] = i
        for f in as_completed(pushes):
            i = pushes[f]
            try:
                msg = f.result()
                plog(i.pkg, ("[dry-run] " if opts.dry_run else "ok  ") + msg)
                ok += 1
            except Exception as e:  # noqa: BLE001
                plog(i.pkg, f"FAIL {e}")
                failed.append(i.pkg)
    except KeyboardInterrupt:
        log("interrupted; waiting for running adb commands to finish ...")
        pull_ex.shutdown(cancel_futures=True)
        push_ex.shutdown(cancel_futures=True)
        raise
    pull_ex.shutdown()
    push_ex.shutdown()

    log(summary())
    tail = f"  data unchanged: {unchanged}" if data_mode else ""
    if opts.dry_run:
        log(f"--- would change: {ok}{tail}")
    else:
        log(f"--- ok: {ok}  failed: {len(failed)}{tail}"
            + (f"  ({' '.join(sorted(failed))})" if failed else ""))
    return failed


def list_packages(ep, opts) -> None:
    rows = ep.package_info(opts.verbose)
    rows = [r for r in rows if not (opts.only and not re.search(opts.only, r["pkg"]))
            and not (opts.exclude and re.search(opts.exclude, r["pkg"]))]
    key = {"name": lambda r: r["pkg"], "size": lambda r: -r.get("size", 0),
           "date": lambda r: r.get("date", ""), "cache": lambda r: -r.get("cache_b", -1)}[opts.sort]
    rows.sort(key=key, reverse=opts.sort == "date")
    if opts.debug_only:
        rows = [r for r in rows if r.get("debug")]
    def cut(v: str, n: int) -> str:
        return v if len(v) <= n else v[: n - 2] + ".."
    cols = [("PACKAGE", lambda r: cut(r["pkg"], 48)),
            ("VERSION", lambda r: cut(r.get("name") or "-", 18)),
            ("CODE", lambda r: str(r["code"])),
            ("SIZE", lambda r: human(r["size"]) if "size" in r else "-"),
            ("APKS", lambda r: str(r.get("apks", "-"))),
            ("UPDATED", lambda r: r.get("date", "-")),
            ("TGT", lambda r: str(r.get("target", "-")))]
    if ep.is_device:
        cols.append(("INSTALLER", lambda r: r.get("installer") or "-"))
    if opts.verbose:
        cols += [("DEBUG", lambda r: "yes" if r.get("debug") else "-"),
                 ("CACHE", lambda r: human(r["cache_b"]) if "cache_b" in r
                  else "locked" if r.get("locked") else "refused" if r.get("runas_err") else "-"),
                 ("FILES", lambda r: str(r["cache_files"]) if "cache_b" in r else "-"),
                 ("DATA", lambda r: human(r["data_b"]) if "data_b" in r else "-")]
    table = [[h for h, _ in cols]] + [[f(r) for _, f in cols] for r in rows]
    widths = [max(len(row[i]) for row in table) for i in range(len(cols))]
    right = {"CODE", "SIZE", "APKS", "TGT", "CACHE", "FILES", "DATA"}
    log("")
    log(f"=== {ep.label()}")
    for row in table:
        log("  ".join(c.rjust(w) if cols[i][0] in right else c.ljust(w)
                      for i, (c, w) in enumerate(zip(row, widths))).rstrip())
    total = sum(r.get("size", 0) for r in rows)
    line = f"--- {len(rows)} package(s), APKs {human(total)}"
    if opts.verbose:
        dbg = [r for r in rows if r.get("debug")]
        line += (f"; debuggable: {len(dbg)}, with cache files: {sum(1 for r in dbg if r.get('cache_files'))}"
                 f", cache total {human(sum(r.get('cache_b', 0) for r in dbg))}")
    log(line)
    if opts.verbose:
        for r in rows:
            if r.get("runas_err") and not r.get("locked"):
                log(f"    {r['pkg']}: {r['runas_err']}")


def report_preflight(dev: Device) -> bool:
    """Print install blockers/warnings for dev. True if a hard blocker was found."""
    blockers, warns = dev.preflight()
    if not blockers and not warns:
        log(f"preflight {dev.serial}: OK, no settings found that block adb installs")
        return False
    with _print_lock:
        print(f"preflight {dev.label()}:", file=sys.stderr)
        for tag, items in (("BLOCKS", blockers), ("may block", warns)):
            for problem, fix in items:
                print(f"  [{tag}] {problem}\n           fix: {fix}", file=sys.stderr)
        sys.stderr.flush()
    return bool(blockers)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Sync user-installed APKs (and optionally debug-app cache/data) between Android devices.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("endpoints", nargs="+", metavar="ENDPOINT",
                    help="SRC DST (adb serial, IP:port, or folder); 'devices'; or one+ endpoints with --packages")
    ap.add_argument("-n", "--dry-run", action="store_true", help="show what would happen")
    ap.add_argument("-b", "--both", action="store_true", help="two-way: SRC->DST then DST->SRC (data: first pass only)")
    ap.add_argument("-o", "--only", metavar="REGEX", help="only packages matching REGEX (re.search)")
    ap.add_argument("-x", "--exclude", metavar="REGEX", help="skip packages matching REGEX")
    ap.add_argument("-m", "--missing-only", action="store_true", help="only install apps absent on DST")
    ap.add_argument("-d", "--downgrade", action="store_true", help="replace a newer DST version with SRC's")
    ap.add_argument("-g", "--grant", action="store_true", help="grant all runtime permissions on install")
    ap.add_argument("-i", "--installer", metavar="PKG", help="record PKG as installer, e.g. com.android.vending")
    ap.add_argument("-k", "--keep", metavar="DIR", help="keep pulled files in DIR (default: temp, deleted)")
    ap.add_argument("-j", "--jobs", type=int, default=4, help="parallel pulls from SRC (default 4)")
    ap.add_argument("-J", "--install-jobs", type=int, default=2, help="parallel installs on DST (default 2)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--cache", action="store_true", help="also copy cache/ of debuggable apps (run-as)")
    g.add_argument("--data", action="store_true",
                   help=f"also copy the whole private data dir of debuggable apps, except {sorted(DATA_SKIP)}")
    ap.add_argument("--no-stop-src", action="store_true", help="with --data: don't force-stop the app on SRC first")
    ap.add_argument("-I", "--incremental", action="store_true",
                    help="rsync-like: re-check same-version APKs, transfer only changed cache/data files")
    ap.add_argument("-c", "--checksum", action="store_true",
                    help="with -I: sha256 every same-version APK, not only debuggable ones")
    ap.add_argument("--no-delete", action="store_true",
                    help="with -I: keep DST data files that no longer exist on SRC")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="sync: also list unchanged apps; --packages: add debug/cache/data columns")
    ap.add_argument("-p", "--packages", action="store_true",
                    help="list user-installed packages on each ENDPOINT (name, version, size, date, ...)")
    ap.add_argument("--sort", choices=["name", "size", "date", "cache"], default="name",
                    help="--packages sort order (default name; size/cache largest first, date newest first)")
    ap.add_argument("--debug-only", action="store_true", help="--packages -v: only debuggable apps")
    ap.add_argument("--adb", metavar="PATH", help="path to adb executable")
    ap.add_argument("--no-preflight", action="store_true",
                    help="skip the check of DST settings that block installs (and don't abort on them)")
    opts = ap.parse_args()

    adb = find_adb(opts.adb)
    if opts.endpoints == ["devices"]:
        return subprocess.call([adb, "devices", "-l"])
    if opts.endpoints[0] == "check":
        serials = opts.endpoints[1:]
        if not serials:
            ap.error("check needs one or more device serials")
        bad = False
        for ser in serials:
            dev = Device(adb, ser)
            dev.connect()
            bad |= report_preflight(dev)
        return 1 if bad else 0
    if opts.packages:
        for ep_name in opts.endpoints:
            if is_folder(ep_name):
                if not os.path.isdir(ep_name):
                    die(f"folder {ep_name} does not exist")
                ep = Folder(ep_name)
            else:
                ep = Device(adb, ep_name)
                ep.connect()
            list_packages(ep, opts)
        return 0
    if len(opts.endpoints) != 2:
        ap.error("need exactly SRC and DST")
    if opts.jobs < 1 or opts.install_jobs < 1:
        ap.error("--jobs/--install-jobs must be >= 1")
    if (opts.checksum or opts.no_delete) and not opts.incremental:
        ap.error("--checksum/--no-delete need -I/--incremental")
    for rx in (opts.only, opts.exclude):
        if rx:
            try:
                re.compile(rx)
            except re.error as e:
                ap.error(f"bad regex {rx!r}: {e}")

    a, b = opts.endpoints
    if a == b:
        die("SRC and DST are the same")
    src = Folder(a) if is_folder(a) else Device(adb, a)
    dst = Folder(b) if is_folder(b) else Device(adb, b)
    if not src.is_device and not dst.is_device:
        die("at least one endpoint must be a device")
    data_mode = "cache" if opts.cache else "data" if opts.data else None
    for ep in (src, dst):  # devices first, so nothing is created on a refused run
        if ep.is_device:
            ep.connect()
            if data_mode and ep.storage_locked():
                die(f"{ep.serial}: phone not unlocked since boot, app data is encrypted; "
                    f"unlock the screen once and rerun (or drop --{data_mode})")
    if not opts.no_preflight:
        blocked = False
        for ep in ((dst, src) if opts.both else (dst,)):
            if ep.is_device:
                blocked |= report_preflight(ep)
        if blocked and not opts.dry_run:
            die("install blocked by device settings (see above); fix them and rerun, "
                "or pass --no-preflight to try anyway")
    for ep in (src, dst):
        if not ep.is_device:
            ep.connect()

    if opts.keep:
        work_root = Path(opts.keep).expanduser()
        work_root.mkdir(parents=True, exist_ok=True)
        cleanup = None
    else:
        cleanup = tempfile.mkdtemp(prefix="apk-sync-")
        work_root = Path(cleanup)
    try:
        failed = sync_pass(src, dst, opts, data_mode, work_root)
        if opts.both:
            failed += sync_pass(dst, src, opts, None, work_root)
    except KeyboardInterrupt:
        return 130
    finally:
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:  # e.g. piped into head
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        sys.exit(0)
