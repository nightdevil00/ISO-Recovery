#!/usr/bin/env python3
"""
Linux ISO Boot Installer - boot an archiso-family ISO from an internal disk, no USB.

Carve a FAT32 loader partition plus a large-file-capable ISO store out of a
disk's unallocated space, drop the ISO's own GRUB + marker + a generated
grub.cfg in place, and register a UEFI boot entry. Works with any archiso
build (Omarchy, Arch, CachyOS, EndeavourOS, ...); the ISO is validated first.

Requires: Windows, Python 3.9+, UEFI + GPT, Administrator for the write steps.

    python omarchy_iso_boot.py              # GUI
    python omarchy_iso_boot.py --selftest   # headless checks, changes nothing
    python omarchy_iso_boot.py --remove     # undo a previous run
    python omarchy_iso_boot.py --help
"""

from __future__ import annotations

import ctypes
import datetime
import hashlib
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# --------------------------------------------------------------- constants ---

VERSION = "1.1.0"
APP_TITLE = "Linux ISO Boot Installer"
ENTRY_PATH = r"\EFI\BOOT\BOOTx64.EFI"
ENTRY_SUFFIX = "(live ISO on disk)"
# Labels used by versions before 1.1.0; still cleaned up so an upgrade never
# leaves the old partitions stranded.
LEGACY_LABELS = ("OMARCHYBOOT", "OMARCHY_ISO")

PROGRAM_DATA = os.environ.get("ProgramData", r"C:\ProgramData")
STATE_DIR = Path(PROGRAM_DATA) / "LinuxBootPartition"
STATE_FILE = STATE_DIR / "state.json"
CACHE_DIR = Path(tempfile.gettempdir()) / "omarchy_iso_boot"

NO_WINDOW = 0x08000000
CHUNK = 8 * 1024 * 1024
GIB = 1024 ** 3


class ToolError(RuntimeError):
    """Something the user needs to read and act on."""


# ------------------------------------------------------------- shell utils ---

def run(args: list[str], timeout: int = 900) -> str:
    """Run a command, return stdout+stderr, raise ToolError on failure."""
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        creationflags=NO_WINDOW,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise ToolError(f"Command failed ({proc.returncode}): {' '.join(args[:3])}\n{out.strip()}")
    return out


def ps(script: str, timeout: int = 900) -> str:
    """Run a PowerShell snippet. Returns raw text."""
    return run(
        [
            "powershell", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-Command", script,
        ],
        timeout=timeout,
    )


def ps_json(script: str) -> list:
    """Run PowerShell that emits a JSON array; normalise to a list."""
    raw = ps(script).strip()
    if not raw:
        raise ToolError("PowerShell returned no output; expected JSON.")
    if not raw.lstrip().startswith(("[", "{")):
        raise ToolError(f"PowerShell returned something that is not JSON:\n{raw[:500]}")
    data = json.loads(raw)
    return data if isinstance(data, list) else [data]


def ps_str(value: str) -> str:
    """Quote a value for safe embedding in a PowerShell single-quoted string."""
    return "'" + str(value).replace("'", "''") + "'"


def fill(template: str, **kw) -> str:
    """Replace {{name}} placeholders without clashing with ${} or @{}."""
    for key, val in kw.items():
        template = template.replace("{{" + key + "}}", str(val))
    return template


# ------------------------------------------------------------- iso naming ---

def display_name(iso_name: str) -> str:
    """'omarchy-4.0.6.iso' -> 'Omarchy 4.0.6'."""
    stem = Path(iso_name).stem
    words = []
    for part in re.split(r"[\s_-]+", stem):
        if not part:
            continue
        words.append(part.upper() if part.lower() == "iso" else part[:1].upper() + part[1:])
    return " ".join(words) or stem


def labels_for(iso_name: str) -> dict:
    """
    Volume labels that are unique per ISO and fit FAT32's 11-character limit.

    Both are derived from the ISO file name plus a short content tag, so two
    different ISOs never fight over the same labels and a re-run of the same
    ISO is detectable before anything is written.
    """
    stem = Path(iso_name).stem
    clean = re.sub(r"[^A-Za-z0-9]+", "", stem).upper() or "ISO"
    tag = hashlib.sha1(stem.encode("utf-8")).hexdigest()[:3].upper()
    return {
        "loader": ("B" + clean[:7] + tag)[:11],   # B = boot loader
        "store": ("S" + clean[:7] + tag)[:11],    # S = ISO store
    }


def entry_description(iso_name: str) -> str:
    return f"{display_name(iso_name)} {ENTRY_SUFFIX}"


def downloads_dir() -> Path:
    """The real Downloads folder (OneDrive may have redirected it)."""
    try:
        class _GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        known = uuid.UUID("{374DE290-123F-4565-9164-39C4925E467B}")
        guid = _GUID(
            known.fields[0], known.fields[1], known.fields[2],
            (ctypes.c_ubyte * 8)(*known.bytes[8:]),
        )
        ptr = ctypes.c_wchar_p()
        shell32 = ctypes.windll.shell32
        shell32.SHGetKnownFolderPath.argtypes = [
            ctypes.POINTER(_GUID), ctypes.c_ulong, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_wchar_p),
        ]
        shell32.SHGetKnownFolderPath.restype = ctypes.c_long
        if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(ptr)) == 0:
            value = ptr.value
            ctypes.windll.ole32.CoTaskMemFree(ctypes.cast(ptr, ctypes.c_void_p))
            if value and Path(value).is_dir():
                return Path(value)
    except Exception:
        pass
    return Path.home() / "Downloads"


def find_boot_entries(description: str, contains: bool = True) -> list[str]:
    """
    Return GUIDs of BCD entries whose description matches.

    bcdedit output is localized, so never look for English keywords: split the
    listing into blocks on the dashed separators and search the whole block for
    the description text itself.
    """
    try:
        out = run(["bcdedit", "/enum", "all"])
    except ToolError:
        return []
    needle = description.lower()
    found: list[str] = []
    for block in re.split(r"-{3,}", out):
        if needle not in block.lower():
            continue
        match = re.search(r"\{[0-9a-fA-F-]{36}\}", block)
        if match and match.group(0).lower() not in {g.lower() for g in found}:
            found.append(match.group(0))
    return found


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> None:
    """Re-launch this script (or EXE) elevated, keeping the same arguments."""
    if getattr(sys, "frozen", False):
        exe = sys.executable
        params = " ".join(f'"{a}"' for a in sys.argv[1:])
    else:
        exe = sys.executable
        script = str(Path(__file__).resolve())
        params = " ".join(f'"{a}"' for a in [script, *sys.argv[1:]])
    ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)


# ------------------------------------------------------------ system probe ---

def get_firmware_type() -> str:
    try:
        out = ps("(Get-ComputerInfo -Property BiosFirmwareType).BiosFirmwareType").strip()
        return out or "unknown"
    except Exception:
        return "unknown"


def get_secure_boot() -> Optional[bool]:
    """True/False, or None when it cannot be determined (needs admin)."""
    try:
        out = ps("try { Confirm-SecureBootUEFI } catch { 'unknown' }").strip().lower()
    except Exception:
        return None
    if out == "true":
        return True
    if out == "false":
        return False
    return None


def list_disks() -> list[dict]:
    return ps_json("""
$out = @(
  Get-Disk | ForEach-Object {
    [pscustomobject]@{
      number = [int]$_.Number
      model  = [string]$_.FriendlyName
      sizeGB = [math]::Round($_.Size / 1GB, 1)
      freeGB = [math]::Round(($_.Size - $_.AllocatedSize) / 1GB, 1)
      style  = [string]$_.PartitionStyle
      bus    = [string]$_.BusType
    }
  }
)
ConvertTo-Json -InputObject $out -Compress -Depth 4
""")


def list_partitions(disk_number: int) -> list[dict]:
    return ps_json(fill("""
$out = @(
  Get-Partition -DiskNumber {{disk}} | ForEach-Object {
    [pscustomobject]@{
      number = [int]$_.PartitionNumber
      letter = if ($_.DriveLetter) { [string]$_.DriveLetter } else { '' }
      sizeGB = [math]::Round($_.Size / 1GB, 2)
      type   = [string]$_.Type
    }
  }
)
ConvertTo-Json -InputObject $out -Compress -Depth 4
""", disk=disk_number))


_SYS_DISK: dict = {}


def system_disk_number() -> Optional[int]:
    if "value" in _SYS_DISK:
        return _SYS_DISK["value"]
    drive = os.environ.get("SystemDrive", "C:").rstrip(":")
    try:
        out = ps(f"(Get-Partition -DriveLetter {drive} -ErrorAction SilentlyContinue).DiskNumber")
        value = int(out.strip()) if out.strip() else None
    except Exception:
        value = None
    _SYS_DISK["value"] = value
    return value


# --------------------------------------------------------------- ISO work ----

@dataclass
class IsoLayout:
    path: str
    label: str
    basedir: str
    kernel: str          # /arch/boot/x86_64/vmlinuz-linux-t2
    initrd: str          # /arch/boot/x86_64/initramfs-linux-t2.img
    marker: str          # 2026-10-19-22-18-53-00.uuid
    efi_src: str
    marker_src: str
    size_gb: float
    efi_size_mb: float = 0.0


def mount_iso(path: str) -> tuple[str, str]:
    """Mount an ISO, return (drive_letter, filesystem_label)."""
    data = ps_json(fill("""
# Never stack mounts: a previous attachment would leave a ghost CD-ROM drive.
$existing = Get-DiskImage -ImagePath {{p}} -ErrorAction SilentlyContinue
if ($existing -and $existing.Attached) {
    Dismount-DiskImage -ImagePath {{p}} -ErrorAction SilentlyContinue | Out-Null
}
$img = Mount-DiskImage -ImagePath {{p}} -PassThru -ErrorAction Stop
$v = $img | Get-Volume
$o = [pscustomobject]@{ letter = [string]$v.DriveLetter; label = [string]$v.FileSystemLabel }
ConvertTo-Json -InputObject $o -Compress
""", p=ps_str(path)))
    if not data or not data[0].get("letter"):
        raise ToolError("The ISO mounted but Windows gave it no drive letter.")
    return data[0]["letter"], data[0].get("label", "")


def unmount_iso(path: str) -> None:
    try:
        ps(fill("Dismount-DiskImage -ImagePath {{p}} -ErrorAction SilentlyContinue | Out-Null",
                p=ps_str(path)), timeout=120)
    except Exception:
        pass


def inspect_iso(path: str) -> IsoLayout:
    """Mount the ISO, work out how to boot it, unmount. No writes."""
    iso = str(Path(path))
    if not Path(iso).is_file():
        raise ToolError(f"ISO not found: {iso}")
    if Path(iso).suffix.lower() != ".iso":
        raise ToolError("That file does not look like a .iso.")

    letter, label = mount_iso(iso)
    root = Path(f"{letter}:/")
    try:
        # archiso layout: <basedir>/x86_64/airootfs.sfs
        sfs = None
        for dirpath, _dirnames, filenames in os.walk(root):
            p = Path(dirpath)
            if p.name == "x86_64" and "airootfs.sfs" in filenames:
                sfs = p / "airootfs.sfs"
                break
        if sfs is None:
            raise ToolError(
                "Not an archiso image: no <basedir>/x86_64/airootfs.sfs found.\n"
                "This tool only handles Arch, CachyOS, Omarchy, EndeavourOS, etc."
            )

        basedir_dir = sfs.parent.parent
        basedir = basedir_dir.name

        boot_dir = basedir_dir / "boot" / "x86_64"
        if not boot_dir.is_dir():
            raise ToolError(f"Missing {basedir}/boot/x86_64/ inside the ISO.")

        def first(pred: Callable[[str], bool]) -> Optional[Path]:
            for entry in sorted(boot_dir.iterdir()):
                if pred(entry.name):
                    return entry
            return None

        kernel = first(lambda n: n.startswith("vmlinuz"))
        initrd = first(lambda n: n.startswith("initramfs") and n.endswith(".img"))
        if kernel is None:
            raise ToolError(f"No vmlinuz found in /{basedir}/boot/x86_64/.")
        if initrd is None:
            raise ToolError(f"No initramfs found in /{basedir}/boot/x86_64/.")

        efi = root / "EFI" / "BOOT" / "BOOTx64.EFI"
        if not efi.is_file():
            raise ToolError("ISO has no EFI/BOOT/BOOTx64.EFI - it cannot be UEFI booted.")

        marker_dir = root / "boot"
        marker = None
        if marker_dir.is_dir():
            for entry in sorted(marker_dir.iterdir()):
                if entry.name.endswith(".uuid"):
                    marker = entry
                    break
        if marker is None:
            raise ToolError("No archiso marker file (/boot/*.uuid) in the ISO.")

        # Verify the EFI GRUB really does marker discovery + loopback boot.
        blob = efi.read_bytes()
        if b"ARCHISO_HINT" not in blob or marker.name.encode() not in blob:
            raise ToolError(
                f"EFI/BOOT/BOOTx64.EFI does not reference '{marker.name}'.\n"
                "Unsupported ISO build - refusing to guess."
            )
        if b"loopback" not in blob:
            raise ToolError("EFI/BOOT/BOOTx64.EFI has no GRUB loopback module.")

        def rel(p: Path) -> str:
            return "/" + p.relative_to(root).as_posix()

        # The mount is about to disappear, so cache the two files we need to
        # copy later. install() must not depend on the ISO still being mounted.
        key = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(iso).stem)
        cache = CACHE_DIR / key
        cache.mkdir(parents=True, exist_ok=True)
        cached_efi = cache / "BOOTx64.EFI"
        cached_marker = cache / marker.name
        shutil.copyfile(efi, cached_efi)
        shutil.copyfile(marker, cached_marker)

        return IsoLayout(
            path=iso,
            label=label,
            basedir=basedir,
            kernel=rel(kernel),
            initrd=rel(initrd),
            marker=marker.name,
            efi_src=str(cached_efi),
            marker_src=str(cached_marker),
            size_gb=os.path.getsize(iso) / GIB,
            efi_size_mb=cached_efi.stat().st_size / 1024 / 1024,
        )
    finally:
        unmount_iso(iso)


# ---------------------------------------------------------- grub.cfg build ---

GRUB_TEMPLATE = """\
# Generated by omarchy_iso_boot.py -- safe to edit by hand.
#
# Boot chain:
#   firmware -> FAT32 loader partition -> EFI/BOOT/BOOTx64.EFI (GRUB from the ISO)
#   GRUB searches every readable filesystem for /boot/{{MARKER}}, finds it on the
#   ISO store, loads THIS file from there and exports ARCHISO_HINT / ARCHISO_UUID.
#   We loopback-mount the .iso and hand those to the archiso initramfs.

set timeout=10
set default=0

set isofile=/{{ISOFILE}}

insmod part_gpt
insmod loopback
insmod search_fs_file
insmod ntfs
insmod exfat

# Self-heal if the bootstrap did not export them for some reason.
if [ -z "${ARCHISO_HINT}" ]; then
    search --no-floppy --set=ARCHISO_HINT --file /boot/{{MARKER}}
fi
if [ -z "${ARCHISO_UUID}" ]; then
    probe --set=ARCHISO_UUID --fs-uuid "${ARCHISO_HINT}"
fi

menuentry "{{NAME}}  -  boot live ISO from disk" --id live-iso {
    set gfxpayload=keep
    search --no-floppy --set=isodev --file ${isofile}
    if [ -z "${isodev}" ]; then
        echo "Could not find ${isofile} on any partition."
        sleep 15
    fi
    loopback loopdev (${isodev})${isofile}
    linux (loopdev){{KERNEL}} archisobasedir={{BASEDIR}} img_dev=/dev/disk/by-label/{{STORE_LABEL}} img_loop=${isofile} quiet splash initramfs_async=0
    initrd (loopdev){{INITRD}}
}

menuentry "{{NAME}}  -  alternate (mount store by UUID)" --id live-iso-uuid {
    set gfxpayload=keep
    loopback loopdev (${ARCHISO_HINT})${isofile}
    linux (loopdev){{KERNEL}} archisobasedir={{BASEDIR}} img_dev=UUID=${ARCHISO_UUID} img_loop=${isofile} quiet splash initramfs_async=0
    initrd (loopdev){{INITRD}}
}

menuentry "Show what GRUB sees (diagnostics)" {
    echo "ARCHISO_HINT = ${ARCHISO_HINT}"
    echo "ARCHISO_UUID = ${ARCHISO_UUID}"
    echo "isofile      = ${isofile}"
    echo ""
    ls
    echo ""
    echo "Returning in 20s."
    sleep 20
}

menuentry "GRUB command line" { commandline }
menuentry "Reboot"              { reboot }
menuentry "Shut down"           { halt }
"""


def render_grub_cfg(layout: IsoLayout, iso_name: str, store_label: Optional[str] = None) -> str:
    name = display_name(iso_name)
    return fill(
        GRUB_TEMPLATE,
        NAME=name,
        ISOFILE=iso_name,
        MARKER=layout.marker,
        KERNEL=layout.kernel,
        INITRD=layout.initrd,
        BASEDIR=layout.basedir,
        STORE_LABEL=store_label or labels_for(iso_name)["store"],
    )


# ------------------------------------------------------------------ writes ---

def create_partition(disk_number: int, size_gb: float, fs: str, label: str) -> dict:
    size_bytes = int(size_gb * GIB)
    data = ps_json(fill("""
$p = New-Partition -DiskNumber {{disk}} -Size {{size}} -AssignDriveLetter -ErrorAction Stop
$v = Format-Volume -Partition $p -FileSystem {{fs}} -NewFileSystemLabel {{label}} -Confirm:$false -Force
$o = [pscustomobject]@{
  partition = [int]$p.PartitionNumber
  guid      = [string]$p.Guid
  letter    = [string]$v.DriveLetter
  label     = [string]$v.FileSystemLabel
}
ConvertTo-Json -InputObject $o -Compress
""", disk=disk_number, size=size_bytes, fs=ps_str(fs), label=ps_str(label)))
    if not data:
        raise ToolError("Partition creation returned no result.")
    info = data[0]
    if not info.get("letter"):
        raise ToolError("New partition got no drive letter.")
    if not info.get("guid"):
        raise ToolError("New partition reported no GUID; refusing to continue.")
    return info


def create_boot_entry(letter: str, efi_path: str, description: str) -> str:
    out = run(["bcdedit", "/copy", "{bootmgr}", "/d", description])
    match = re.search(r"\{[0-9a-fA-F-]{36}\}", out)
    if not match:
        raise ToolError(f"Could not read the new boot entry GUID from bcdedit:\n{out}")
    guid = match.group(0)

    run(["bcdedit", "/set", guid, "device", f"partition={letter}"])
    run(["bcdedit", "/set", guid, "path", efi_path])
    run(["bcdedit", "/set", guid, "description", description])
    run(["bcdedit", "/set", "{fwbootmgr}", "displayorder", guid, "/addfirst"])
    return guid


def copy_with_progress(
    src: str,
    dst: str,
    on_progress: Callable[[float, str], None],
    label: str,
) -> None:
    total = os.path.getsize(src)
    done = 0
    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        while True:
            buf = fsrc.read(CHUNK)
            if not buf:
                break
            fdst.write(buf)
            done += len(buf)
            on_progress(done / total if total else 1.0, label)


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def partitions_with_labels(disk_number: int, labels) -> list[dict]:
    """Partitions on this disk whose volume label is in `labels`."""
    wanted = {str(label).upper() for label in labels}
    data = ps_json(fill("""
$out = @(
  Get-Partition -DiskNumber {{disk}} -ErrorAction SilentlyContinue | ForEach-Object {
    $v = Get-Volume -Partition $_ -ErrorAction SilentlyContinue
    [pscustomobject]@{
      number = [int]$_.PartitionNumber
      guid   = [string]$_.Guid
      label  = [string]$v.FileSystemLabel
    }
  }
)
ConvertTo-Json -InputObject $out -Compress -Depth 4
""", disk=disk_number))
    return [p for p in data if str(p.get("label", "")).upper() in wanted]


def remove_partition_by_guid(disk_number: int, guid: str) -> bool:
    """Remove a partition by GUID; partition numbers can be renumbered."""
    if not guid:
        return False
    out = ps(fill("""
$p = Get-Partition -DiskNumber {{d}} -ErrorAction SilentlyContinue |
     Where-Object { [string]$_.Guid -eq {{g}} }
if ($p) {
    Remove-Partition -DiskNumber {{d}} -PartitionNumber $p.PartitionNumber -Confirm:$false -ErrorAction Stop
    'removed'
} else {
    'absent'
}
""", d=disk_number, g=ps_str(guid))).strip().lower()
    return out.endswith("removed")


def read_state() -> Optional[dict]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def preflight(
    disk_number: int,
    loader_label: str,
    store_label: str,
    description: str,
    on_log: Callable[[str], None],
) -> None:
    """
    Make a second run safe: never stack a new layout on top of an old one.

    A complete/partial run recorded in state must be removed explicitly. Leftover
    partitions from a crashed run (no usable state) and stale boot entries with
    the same description are cleaned up automatically.
    """
    prior = read_state()
    if prior is not None:
        iso = prior.get("IsoFile") or "an unknown ISO"
        stage = "complete" if prior.get("Complete") else "incomplete"
        raise ToolError(
            f"A previous setup already exists on this machine ({iso}, {stage}).\n\n"
            "Use 'Remove previous setup' in the GUI, or run with --remove, "
            "then start again. Nothing has been changed."
        )

    for part in partitions_with_labels(disk_number, {loader_label, store_label}):
        on_log(f"Removing leftover '{part['label']}' partition {part['number']} "
               "from an earlier attempt ...")
        if not remove_partition_by_guid(disk_number, part.get("guid", "")):
            raise ToolError(
                f"Leftover partition '{part['label']}' exists but could not be removed. "
                "Delete it in Disk Management, then try again."
            )

    legacy = partitions_with_labels(disk_number, LEGACY_LABELS)
    if legacy:
        names = ", ".join(sorted({f"{p['label']} (partition {p['number']})" for p in legacy}))
        raise ToolError(
            f"Disk {disk_number} still has partitions from an older version of this tool: "
            f"{names}.\n\n"
            "Run 'Remove previous setup' (or --remove) once to clean them up, "
            "then start again. Nothing has been changed."
        )

    for guid in find_boot_entries(description):
        try:
            run(["bcdedit", "/delete", guid])
            on_log(f"Removed stale boot entry {guid}")
        except ToolError as exc:
            on_log(f"Could not remove stale boot entry {guid}: {exc}")


def disk_guid(disk_number: int) -> str:
    try:
        return ps(fill("(Get-Disk -Number {{d}} -ErrorAction SilentlyContinue).Guid", d=disk_number)).strip()
    except Exception:
        return ""


def install(
    layout: IsoLayout,
    disk_number: int,
    loader_gb: float,
    store_gb: float,
    filesystem: str,
    on_log: Callable[[str], None],
    on_progress: Callable[[float, str], None],
) -> dict:
    """Create partitions, populate them, register the boot entry."""
    if not is_admin():
        raise ToolError("Administrator rights are required to create partitions.")

    iso_name = Path(layout.path).name
    iso_bytes = os.path.getsize(layout.path)
    labels = labels_for(iso_name)
    loader_label, store_label = labels["loader"], labels["store"]
    description = entry_description(iso_name)

    on_log(f"Plan: loader FAT32 '{loader_label}', store {filesystem} '{store_label}'")
    on_log(f"      boot entry \"{description}\"")
    preflight(disk_number, loader_label, store_label, description, on_log)

    on_log(f"Creating {loader_gb:g} GB FAT32 loader partition ...")
    loader = create_partition(disk_number, loader_gb, "FAT32", loader_label)
    on_log(f"  loader -> partition {loader['partition']}, {loader['letter']}: (FAT32)")

    on_log(f"Creating {store_gb:g} GB {filesystem} ISO store ...")
    try:
        store = create_partition(disk_number, store_gb, filesystem, store_label)
    except Exception:
        # Never leave a half-built layout behind.
        on_log("  store creation failed - rolling back the loader partition")
        try:
            remove_partition_by_guid(disk_number, loader.get("guid", ""))
        except Exception:
            pass
        raise
    on_log(f"  store  -> partition {store['partition']}, {store['letter']}: ({filesystem})")
    if store["label"].upper() != store_label:
        raise ToolError(
            f"Store volume label is '{store['label']}', expected {store_label}. "
            "grub.cfg mounts by this label, so the build cannot continue."
        )

    # Record the layout now, so a failure during the long copy can still be
    # undone with Remove instead of stranded partitions.
    state = {
        "CreatedUtc": datetime.datetime.now().astimezone().isoformat(),
        "Version": VERSION,
        "EntryGuid": None,
        "EntryDescription": description,
        "DiskNumber": int(disk_number),
        "DiskGuid": disk_guid(disk_number),
        "LoaderPartition": int(loader["partition"]),
        "LoaderPartitionGuid": loader["guid"],
        "StorePartition": int(store["partition"]),
        "StorePartitionGuid": store["guid"],
        "LoaderLabel": loader_label,
        "StoreLabel": store_label,
        "LoaderLetter": f"{loader['letter']}:",
        "StoreLetter": f"{store['letter']}:",
        "IsoPath": layout.path,
        "IsoFile": iso_name,
        "Complete": False,
    }
    save_state(state)

    # 1. EFI loader
    efi_dst_dir = Path(f"{loader['letter']}:/EFI/BOOT")
    efi_dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(layout.efi_src, efi_dst_dir / "BOOTx64.EFI")
    on_log("  copied EFI/BOOT/BOOTx64.EFI")

    # 2. marker + grub.cfg
    boot_dir = Path(f"{store['letter']}:/boot")
    grub_dir = boot_dir / "grub"
    grub_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(layout.marker_src, boot_dir / layout.marker)
    (grub_dir / "grub.cfg").write_text(
        render_grub_cfg(layout, iso_name, store_label), encoding="ascii", newline="\n"
    )
    on_log(f"  wrote boot/{layout.marker} and boot/grub/grub.cfg")

    # 3. the ISO itself
    on_log(f"  copying {iso_name} ({iso_bytes / GIB:.2f} GB) - this takes a few minutes")
    copy_with_progress(layout.path, str(Path(f"{store['letter']}:/{iso_name}")),
                       on_progress, f"copying {iso_name}")

    # 4. firmware entry
    on_log("Registering the UEFI boot entry ...")
    guid = create_boot_entry(f"{loader['letter']}:", ENTRY_PATH, description)
    on_log(f"  boot entry {guid} -> {loader['letter']}:{ENTRY_PATH}")

    state["EntryGuid"] = guid
    state["Complete"] = True
    save_state(state)
    return state


def remove_previous(on_log: Callable[[str], None]) -> int:
    """Undo a run. Safe to call when there is nothing to undo."""
    if not is_admin():
        raise ToolError("Administrator rights are required.")

    state = read_state()
    performed = 0

    # --- boot entries: the recorded GUID, anything with the recorded
    # description, and anything carrying this tool's suffix (covers old runs).
    guids: list[str] = []
    if state and state.get("EntryGuid"):
        guids.append(state["EntryGuid"])
    descriptions = [ENTRY_SUFFIX]
    if state and state.get("EntryDescription"):
        descriptions.append(state["EntryDescription"])
    for text in descriptions:
        guids += find_boot_entries(text)
    seen: set[str] = set()
    unique: list[str] = []
    for guid in guids:
        key = guid.lower()
        if key not in seen:
            seen.add(key)
            unique.append(guid)
    for guid in unique:
        try:
            run(["bcdedit", "/delete", guid])
            on_log(f"Deleted boot entry {guid}")
            performed += 1
        except ToolError as exc:
            on_log(f"Could not delete boot entry {guid}: {exc}")

    # --- partitions: prefer GUIDs (numbers get renumbered). Fall back to the
    # volume labels, which also lets a legacy (pre-1.1.0) state be cleaned up.
    targets: list[tuple[Optional[int], str, Optional[str], Optional[str]]] = []
    if state:
        disk = state.get("DiskNumber")
        targets += [
            (disk, "store", state.get("StorePartitionGuid"),
             state.get("StoreLabel") or LEGACY_LABELS[1]),
            (disk, "loader", state.get("LoaderPartitionGuid"),
             state.get("LoaderLabel") or LEGACY_LABELS[0]),
        ]
    else:
        on_log("No previous run recorded; checking for leftover partitions ...")
        try:
            disks = [d["number"] for d in list_disks()]
        except ToolError:
            disks = []
        for number in disks:
            for label in LEGACY_LABELS:
                targets.append((number, f"'{label}'", None, label))

    for disk, name, guid, label in targets:
        if disk is None:
            continue
        try:
            if not guid and label:
                found = partitions_with_labels(disk, {label})
                guid = found[0].get("guid") if found else None
            if not guid:
                on_log(f"No {name} partition found.")
                continue
            if remove_partition_by_guid(disk, guid):
                on_log(f"Removed {name} partition ({label or guid})")
                performed += 1
            else:
                on_log(f"{name.capitalize()} partition already gone.")
        except ToolError as exc:
            on_log(f"Could not remove {name} partition: {exc}")

    STATE_FILE.unlink(missing_ok=True)
    shutil.rmtree(CACHE_DIR, ignore_errors=True)
    if performed:
        on_log("Done. The space is unallocated again.")
    else:
        on_log("Nothing to remove.")
    return performed


# --------------------------------------------------------------------- GUI ---

def launch_gui(build_only: bool = False) -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    class App(tk.Tk):
        def __init__(self) -> None:
            super().__init__()
            self.title(APP_TITLE)
            self.geometry("860x780")
            self.minsize(780, 700)

            self.q: queue.Queue = queue.Queue()
            self.busy = False
            self.plan_ok = False
            self.layout: Optional[IsoLayout] = None
            self.disks: list[dict] = []
            self.admin = is_admin()
            self.firmware_ok = True
            self.has_state = STATE_FILE.is_file()
            self.secure_boot: Optional[bool] = None

            style = ttk.Style(self)
            if "vista" in style.theme_names():
                style.theme_use("vista")
            style.configure("H1.TLabel", font=("Segoe UI", 15, "bold"))
            style.configure("H2.TLabel", font=("Segoe UI", 10, "bold"))
            style.configure("Dim.TLabel", foreground="#555")
            style.configure("Ok.TLabel", foreground="#0a7d28")
            style.configure("Bad.TLabel", foreground="#b00020")
            style.configure("Warn.TLabel", foreground="#9a6700")

            self._build()
            self.after(80, self._drain)
            self.after(200, self._startup_probe)

        # ---------------------------------------------------------- layout --
        def _build(self) -> None:
            pad = {"padx": 10, "pady": 4}
            root = ttk.Frame(self, padding=12)
            root.pack(fill="both", expand=True)

            ttk.Label(root, text="Boot a Linux ISO from the internal disk", style="H1.TLabel").pack(anchor="w")
            ttk.Label(
                root,
                text="No USB needed. Creates a FAT32 loader + NTFS ISO store from a disk's free space, "
                     "then registers a UEFI boot entry.",
                style="Dim.TLabel",
            ).pack(anchor="w", pady=(0, 8))

            # --- status strip
            strip = ttk.Frame(root)
            strip.pack(fill="x", pady=(0, 8))
            self.lbl_admin = ttk.Label(strip, text="")
            self.lbl_admin.pack(side="left", padx=(0, 16))
            self.lbl_sb = ttk.Label(strip, text="Secure Boot: checking ...")
            self.lbl_sb.pack(side="left", padx=(0, 16))
            self.btn_elevate = ttk.Button(strip, text="Relaunch as Administrator",
                                          command=self._on_elevate)
            self.btn_elevate.pack(side="right")

            # --- ISO
            frm_iso = ttk.LabelFrame(root, text=" 1. Installation image ", padding=8)
            frm_iso.pack(fill="x", **pad)

            row = ttk.Frame(frm_iso)
            row.pack(fill="x")
            self.var_iso = tk.StringVar()
            ttk.Entry(row, textvariable=self.var_iso).pack(side="left", fill="x", expand=True)
            ttk.Button(row, text="Browse...", command=self._on_browse).pack(side="left", padx=(6, 0))
            ttk.Button(row, text="Inspect", command=self._on_inspect).pack(side="left", padx=(6, 0))

            self.txt_iso = tk.Text(frm_iso, height=6, wrap="none", relief="flat",
                                   background="#f6f6f6", font=("Consolas", 9))
            self.txt_iso.pack(fill="x", pady=(6, 0))
            self.txt_iso.configure(state="disabled")

            # --- disk
            frm_disk = ttk.LabelFrame(root, text=" 2. Target disk ", padding=8)
            frm_disk.pack(fill="x", **pad)

            row = ttk.Frame(frm_disk)
            row.pack(fill="x")
            ttk.Label(row, text="Disk:").pack(side="left")
            self.var_disk = tk.StringVar()
            self.cmb_disk = ttk.Combobox(row, textvariable=self.var_disk, state="readonly", width=62)
            self.cmb_disk.pack(side="left", padx=(6, 6), fill="x", expand=True)
            self.cmb_disk.bind("<<ComboboxSelected>>", lambda _e: self._on_disk_changed())
            ttk.Button(row, text="Refresh", command=self._on_refresh).pack(side="left")

            self.lbl_disk = ttk.Label(frm_disk, text="", style="Dim.TLabel")
            self.lbl_disk.pack(anchor="w", pady=(6, 0))

            # --- plan
            frm_plan = ttk.LabelFrame(root, text=" 3. Layout ", padding=8)
            frm_plan.pack(fill="x", **pad)

            row = ttk.Frame(frm_plan)
            row.pack(fill="x")
            ttk.Label(row, text="Loader:").pack(side="left")
            self.var_loader = tk.StringVar(value="1")
            ttk.Spinbox(row, from_=0.5, to=4, increment=0.5, textvariable=self.var_loader,
                        width=6, command=self._update_plan).pack(side="left", padx=(4, 12))
            ttk.Label(row, text="GB FAT32").pack(side="left", padx=(0, 18))

            ttk.Label(row, text="Store:").pack(side="left")
            self.var_store = tk.StringVar(value="9")
            ttk.Spinbox(row, from_=6, to=64, increment=1, textvariable=self.var_store,
                        width=6, command=self._update_plan).pack(side="left", padx=(4, 12))
            ttk.Label(row, text="GB").pack(side="left", padx=(0, 6))

            self.var_fs = tk.StringVar(value="NTFS")
            ttk.Combobox(row, textvariable=self.var_fs, values=["NTFS", "exFAT"],
                         state="readonly", width=8).pack(side="left")
            ttk.Label(row, text="(both beat FAT32's 4 GiB file limit)").pack(side="left", padx=(6, 0))

            self.lbl_plan = ttk.Label(frm_plan, text="", style="Dim.TLabel")
            self.lbl_plan.pack(anchor="w", pady=(6, 0))

            # --- actions
            frm_act = ttk.Frame(root)
            frm_act.pack(fill="x", **pad)
            self.btn_create = ttk.Button(frm_act, text="Create bootable partition",
                                         command=self._on_create)
            self.btn_create.pack(side="left")
            self.btn_remove = ttk.Button(frm_act, text="Remove previous setup",
                                         command=self._on_remove)
            self.btn_remove.pack(side="left", padx=(8, 0))

            self.bar = ttk.Progressbar(frm_act, mode="determinate", maximum=100)
            self.bar.pack(side="left", fill="x", expand=True, padx=(16, 0))

            # --- log
            frm_log = ttk.LabelFrame(root, text=" Log ", padding=6)
            frm_log.pack(fill="both", expand=True, **pad)
            self.txt_log = tk.Text(frm_log, height=12, wrap="word", relief="flat",
                                   background="#111318", foreground="#d8dee9",
                                   insertbackground="#d8dee9", font=("Consolas", 9))
            self.txt_log.pack(side="left", fill="both", expand=True)
            sb = ttk.Scrollbar(frm_log, command=self.txt_log.yview)
            sb.pack(side="right", fill="y")
            self.txt_log.configure(yscrollcommand=sb.set)

        # ----------------------------------------------------------- utils --
        def log(self, msg: str) -> None:
            self.txt_log.insert("end", msg.rstrip() + "\n")
            self.txt_log.see("end")

        def _set_busy(self, busy: bool) -> None:
            self.busy = busy
            self._refresh_buttons()

        def _refresh_buttons(self) -> None:
            if self.busy:
                self.btn_create.configure(state="disabled")
                self.btn_remove.configure(state="disabled")
                return
            can_create = (self.admin and self.firmware_ok and not self.has_state
                          and self.layout is not None and self.plan_ok)
            self.btn_create.configure(state="normal" if can_create else "disabled")
            self.btn_remove.configure(state="normal" if self.admin else "disabled")

        def _drain(self) -> None:
            try:
                while True:
                    item = self.q.get_nowait()
                    kind = item[0]
                    if kind == "log":
                        self.log(item[1])
                    elif kind == "progress":
                        self.bar.configure(value=item[1] * 100)
                    elif kind == "error":
                        self._set_busy(False)
                        self.bar.configure(value=0)
                        messagebox.showerror("Something went wrong", item[1], parent=self)
                    elif kind == "done":
                        self._set_busy(False)
                        self.bar.configure(value=100)
                        if len(item) > 1 and callable(item[1]):
                            item[1]()
                    elif kind == "iso":
                        self._show_layout(item[1])
                    elif kind == "invoke":
                        item[1]()
            except queue.Empty:
                pass
            self.after(80, self._drain)

        def _worker(self, fn: Callable[[], None]) -> None:
            def wrap() -> None:
                try:
                    fn()
                except ToolError as exc:
                    self.q.put(("error", str(exc)))
                except Exception as exc:                       # noqa: BLE001
                    self.q.put(("error", f"{type(exc).__name__}: {exc}"))
            threading.Thread(target=wrap, daemon=True).start()

        # ---------------------------------------------------------- startup --
        def _startup_probe(self) -> None:
            def probe() -> None:
                fw = get_firmware_type()
                sb = get_secure_boot()
                self.q.put(("invoke", lambda: self._apply_probe(fw, sb)))
            threading.Thread(target=probe, daemon=True).start()
            self._on_refresh()

            # Auto-inspect the newest ISO in Downloads so the app is useful on open.
            downloads = downloads_dir()
            candidates = (sorted(downloads.glob("*.iso"),
                                 key=lambda p: p.stat().st_mtime, reverse=True)
                          if downloads.is_dir() else [])
            if candidates:
                self.var_iso.set(str(candidates[0]))
                self._on_inspect()

            if self.has_state:
                prior = read_state() or {}
                self.log(f"Existing setup recorded: {prior.get('IsoFile', 'an unknown ISO')}")
                self.log("Use 'Remove previous setup' before creating another one.")

        def _apply_probe(self, fw: str, sb: Optional[bool]) -> None:
            self.secure_boot = sb
            if self.admin:
                self.lbl_admin.configure(text="Administrator: yes", style="Ok.TLabel")
                self.btn_elevate.pack_forget()
            else:
                self.lbl_admin.configure(text="Administrator: no (read-only)", style="Bad.TLabel")

            if fw.lower() != "uefi":
                self.firmware_ok = False
                self.lbl_sb.configure(text=f"Firmware: {fw} - UEFI required!", style="Bad.TLabel")
            elif sb is True:
                self.lbl_sb.configure(text="Secure Boot: ON - must be disabled", style="Bad.TLabel")
            elif sb is False:
                self.lbl_sb.configure(text="Secure Boot: off", style="Ok.TLabel")
            else:
                self.lbl_sb.configure(text="Secure Boot: unknown (needs admin)", style="Warn.TLabel")
            self._refresh_buttons()

        def _on_elevate(self) -> None:
            relaunch_as_admin()
            self.destroy()

        # -------------------------------------------------------------- ISO --
        def _on_browse(self) -> None:
            path = filedialog.askopenfilename(
                title="Select a Linux ISO",
                initialdir=str(downloads_dir()),
                filetypes=[("ISO images", "*.iso"), ("All files", "*.*")],
            )
            if path:
                self.var_iso.set(path)
                self._on_inspect()

        def _on_inspect(self) -> None:
            if self.busy:
                return
            path = self.var_iso.get().strip()
            if not path:
                default = downloads_dir()
                candidates = sorted(default.glob("*.iso"), key=lambda p: p.stat().st_mtime, reverse=True)
                if candidates:
                    path = str(candidates[0])
                    self.var_iso.set(path)
            if not path:
                messagebox.showinfo("No ISO", "Pick an .iso file first.", parent=self)
                return

            self.log(f"Inspecting {path} ...")
            self.txt_iso.configure(state="normal")
            self.txt_iso.delete("1.0", "end")
            self.txt_iso.insert("1.0", "Mounting and inspecting ...")
            self.txt_iso.configure(state="disabled")

            def work() -> None:
                layout = inspect_iso(path)
                self.q.put(("iso", layout))
                self.q.put(("log", "ISO looks bootable: "
                                   f"{layout.basedir}/x86_64/airootfs.sfs, marker {layout.marker}"))
                self.q.put(("done", None))
            self._set_busy(True)
            self._worker(work)

        def _show_layout(self, layout: IsoLayout) -> None:
            self.layout = layout
            store_gb = max(6, math.ceil(layout.size_gb) + 2)
            self.var_store.set(str(store_gb))
            labels = labels_for(Path(layout.path).name)

            lines = [
                f"volume label : {layout.label}",
                f"boot entry   : {entry_description(Path(layout.path).name)}",
                f"partitions   : loader FAT32 '{labels['loader']}' + store '{labels['store']}'",
                f"archiso dir  : /{layout.basedir}",
                f"kernel       : {layout.kernel}",
                f"initramfs    : {layout.initrd}",
                f"marker       : /boot/{layout.marker}",
                f"size         : {layout.size_gb:.2f} GB",
                f"EFI loader   : {Path(layout.efi_src).name} "
                f"({layout.efi_size_mb:.1f} MB, verified loopback+marker)",
            ]
            self.txt_iso.configure(state="normal")
            self.txt_iso.delete("1.0", "end")
            self.txt_iso.insert("1.0", "\n".join(lines))
            self.txt_iso.configure(state="disabled")
            self._update_plan()

        # ------------------------------------------------------------- disk --
        def _on_refresh(self) -> None:
            try:
                self.disks = list_disks()
            except ToolError as exc:
                self.log(f"Could not list disks: {exc}")
                return
            labels = [
                f"Disk {d['number']}: {d['model']}  "
                f"({d['sizeGB']:g} GB {d['style']}, {d['freeGB']:g} GB free)"
                for d in self.disks
            ]
            self.cmb_disk.configure(values=labels)
            if labels:
                if self.var_disk.get() in labels:
                    pass
                else:
                    best = max(range(len(self.disks)), key=lambda i: self.disks[i]["freeGB"])
                    self.cmb_disk.current(best)
                    self.var_disk.set(labels[best])
            self._on_disk_changed()

        def _selected_disk(self) -> Optional[dict]:
            idx = self.cmb_disk.current()
            if idx < 0 or idx >= len(self.disks):
                return None
            return self.disks[idx]

        def _on_disk_changed(self) -> None:
            disk = self._selected_disk()
            if not disk:
                return
            parts = []
            try:
                parts = list_partitions(disk["number"])
            except ToolError:
                pass

            bits = [f"{len(parts)} existing partition(s)"]
            sysdisk = system_disk_number()
            if sysdisk == disk["number"]:
                bits.append("contains Windows - only free space is touched")
            if disk["style"].upper() != "GPT":
                bits.append("NOT GPT - UEFI needs GPT")
            if disk["freeGB"] < 10:
                bits.append("not enough unallocated space")
            self.lbl_disk.configure(
                text=" | ".join(bits),
                style="Warn.TLabel" if any("NOT GPT" in b or "not enough" in b for b in bits) else "Dim.TLabel",
            )
            self._update_plan()

        def _update_plan(self) -> None:
            disk = self._selected_disk()
            try:
                loader = float(self.var_loader.get())
                store = float(self.var_store.get())
            except ValueError:
                self.plan_ok = False
                self.lbl_plan.configure(text="Loader and store sizes must be numbers.", style="Bad.TLabel")
                self._refresh_buttons()
                return

            if not disk:
                self.plan_ok = False
                self.lbl_plan.configure(text="Select a target disk.", style="Warn.TLabel")
                self._refresh_buttons()
                return

            total = loader + store
            left = disk["freeGB"] - total
            if left < 2:
                self.plan_ok = False
                self.lbl_plan.configure(
                    text=f"Needs {total:g} GB but only {disk['freeGB']:g} GB is free on disk {disk['number']}.",
                    style="Bad.TLabel",
                )
            elif disk["style"].upper() != "GPT":
                self.plan_ok = False
                self.lbl_plan.configure(text="Target disk is not GPT - UEFI requires GPT.", style="Bad.TLabel")
            else:
                self.plan_ok = True
                self.lbl_plan.configure(
                    text=f"{loader:g} GB FAT32 loader + {store:g} GB {self.var_fs.get()} store "
                         f"-> {left:g} GB left free for the Linux installation.",
                    style="Ok.TLabel",
                )
            self._refresh_buttons()

        # ----------------------------------------------------------- actions --
        def _on_create(self) -> None:
            if self.busy:
                return
            if not self.admin:
                messagebox.showwarning(
                    "Administrator required",
                    "Partitions cannot be created without elevation.\n\n"
                    "Click 'Relaunch as Administrator' first.",
                    parent=self,
                )
                return
            if not self.layout:
                messagebox.showinfo("Inspect the ISO first",
                                    "Click Inspect so the image can be validated.", parent=self)
                return
            disk = self._selected_disk()
            if not disk:
                return

            try:
                loader = float(self.var_loader.get())
                store = float(self.var_store.get())
            except ValueError:
                return

            if self.secure_boot is True:
                ok = messagebox.askyesno(
                    "Secure Boot is ON",
                    "Secure Boot is enabled. The ISO's GRUB is not Microsoft-signed and the "
                    "firmware will refuse to boot it.\n\n"
                    "You must disable Secure Boot in the firmware setup menu.\n\n"
                    "Create the partitions anyway?",
                    parent=self,
                )
                if not ok:
                    return

            warn = ""
            if system_disk_number() == disk["number"]:
                warn = ("\n\nThis disk also holds Windows. Only unallocated space is used, but "
                        "make sure you have your BitLocker recovery key.")

            layout = self.layout
            fs = self.var_fs.get()
            desc = entry_description(Path(layout.path).name)

            if not messagebox.askyesno(
                "Confirm",
                f"Create on Disk {disk['number']} ({disk['model']}):\n\n"
                f"  - {loader:g} GB FAT32 loader\n"
                f"  - {store:g} GB {self.var_fs.get()} ISO store\n"
                f"  - copy {Path(self.layout.path).name} ({self.layout.size_gb:.2f} GB)\n"
                f"  - add UEFI boot entry \"{desc}\"\n\n"
                f"{disk['freeGB'] - loader - store:g} GB stays free for the installation."
                f"{warn}\n\nProceed?",
                parent=self,
            ):
                return

            self.log("")
            self.log("=" * 62)
            self._set_busy(True)
            self.bar.configure(value=0)

            def work() -> None:
                state = install(
                    layout=layout,
                    disk_number=disk["number"],
                    loader_gb=loader,
                    store_gb=store,
                    filesystem=fs,
                    on_log=lambda m: self.q.put(("log", m)),
                    on_progress=lambda f, m: self.q.put(("progress", f)),
                )
                self.q.put(("log", ""))
                self.q.put(("log", "Done. Reboot, open the firmware boot menu (F12 / F11 / Esc)"))
                self.q.put(("log", f'and choose "{desc}".'))
                self.q.put(("done", self._show_ready))

            self._worker(work)

        def _show_ready(self) -> None:
            self.has_state = True
            self._refresh_buttons()
            desc = (entry_description(Path(self.layout.path).name)
                    if self.layout else "the new boot entry")
            messagebox.showinfo(
                "Ready",
                "Everything is in place.\n\n"
                "Reboot, open the firmware boot menu (F12 / F11 / Esc) and pick:\n\n"
                f"    {desc}\n\n"
                "From the live session, install onto the remaining free space.",
                parent=self,
            )

        def _on_remove(self) -> None:
            if self.busy:
                return
            if not self.admin:
                messagebox.showwarning("Administrator required",
                                       "Click 'Relaunch as Administrator' first.", parent=self)
                return
            if not messagebox.askyesno(
                "Remove",
                "Delete the UEFI boot entry and the loader + store partitions "
                "created by this tool?\n\nThe space becomes unallocated again. "
                "Windows is not touched.",
                parent=self,
            ):
                return
            self._set_busy(True)
            self.bar.configure(value=0)

            def work() -> None:
                remove_previous(lambda m: self.q.put(("log", m)))
                self.q.put(("invoke", self._after_remove))
            self._worker(work)

        def _after_remove(self) -> None:
            self.has_state = False
            self._refresh_buttons()

    app = App()
    if build_only:
        import time

        # Don't let a modal dialog block a headless check.
        failures: list[str] = []
        messagebox.showerror = lambda *a, **k: failures.append(str(a[1] if len(a) > 1 else a))

        deadline = time.time() + 30
        while time.time() < deadline and app.layout is None and not failures:
            app.update()
            time.sleep(0.05)
        for _ in range(30):
            app.update()
            time.sleep(0.02)

        print(f"title        : {app.title()}")
        print(f"geometry     : {app.winfo_width()}x{app.winfo_height()}")
        print(f"admin label  : {app.lbl_admin.cget('text')}")
        print(f"secure boot  : {app.lbl_sb.cget('text')}")
        print(f"disk choices : {list(app.cmb_disk.cget('values'))}")
        print(f"selected     : {app.var_disk.get()}")
        print(f"disk note    : {app.lbl_disk.cget('text')}")
        print(f"plan note    : {app.lbl_plan.cget('text')}")
        print(f"layout loaded: {app.layout is not None}")
        if app.layout:
            print(f"  marker     : {app.layout.marker}")
            print(f"  kernel     : {app.layout.kernel}")
            print(f"  store size : {app.var_store.get()} GB")
        print(f"create state : {app.btn_create.cget('state')}")
        print(f"log lines    : {len(app.txt_log.get('1.0', 'end').strip().splitlines())}")
        if failures:
            print(f"ERRORS       : {failures}")
        app.destroy()
        return 1 if failures else 0
    app.mainloop()
    return 0


# ---------------------------------------------------------------- selftest ---

def selftest() -> int:
    print(f"python      : {sys.version.split()[0]} ({sys.executable})")
    print(f"admin       : {is_admin()}")
    print(f"firmware    : {get_firmware_type()}")
    print(f"secure boot : {get_secure_boot()}")

    print("\n-- disks --")
    try:
        for d in list_disks():
            print(f"  Disk {d['number']}: {d['model']} {d['sizeGB']} GB {d['style']}, {d['freeGB']} GB free")
        sysdisk = system_disk_number()
        print(f"  system disk: {sysdisk}")
    except ToolError as exc:
        print(f"  FAILED: {exc}")

    default = downloads_dir()
    isos = sorted(default.glob("*.iso"), key=lambda p: p.stat().st_mtime, reverse=True) if default.is_dir() else []
    if not isos:
        print(f"\nno ISO in {default} to inspect")
        return 0

    print(f"\n-- inspecting {isos[0].name} --")
    try:
        layout = inspect_iso(str(isos[0]))
    except ToolError as exc:
        print(f"  FAILED: {exc}")
        return 1

    labels = labels_for(isos[0].name)
    print(f"  {'loader':10}: {labels['loader']}")
    print(f"  {'store':10}: {labels['store']}")
    print(f"  {'entry':10}: {entry_description(isos[0].name)}")
    for field in ("label", "basedir", "kernel", "initrd", "marker"):
        print(f"  {field:10}: {getattr(layout, field)}")
    print(f"  size      : {layout.size_gb:.2f} GB")

    print("\n-- generated grub.cfg --")
    cfg = render_grub_cfg(layout, isos[0].name)
    print(cfg)
    assert "${ARCHISO_HINT}" in cfg and "/arch/boot" in cfg
    assert labels["store"] in cfg, "store label missing from grub.cfg"
    assert "{{" not in cfg and "@@" not in cfg, "unsubstituted placeholder left in grub.cfg"
    print("grub.cfg: all placeholders substituted")
    return 0


HELP = """\
Linux ISO Boot Installer {version}

Boot an archiso-family ISO (Omarchy, Arch, CachyOS, EndeavourOS, ...) straight
from free space on an internal disk. No USB stick needed.

Usage:
  python omarchy_iso_boot.py              open the GUI
  python omarchy_iso_boot.py --selftest   check the machine + latest ISO (read-only)
  python omarchy_iso_boot.py --gui-check  build the window and print its state
  python omarchy_iso_boot.py --remove     undo a previous run (needs Administrator)
  python omarchy_iso_boot.py --version    print the version
  python omarchy_iso_boot.py --help       show this text

Requirements: Windows 10/11, UEFI + GPT, Python 3.9+ with tkinter,
Administrator rights for the write steps, Secure Boot disabled.

The ISO is validated before anything is written: it must contain
<basedir>/x86_64/airootfs.sfs, a /boot/*.uuid marker, and an
EFI/BOOT/BOOTx64.EFI that supports ARCHISO_HINT discovery and loopback.
""".format(version=VERSION)


def main() -> int:
    args = {a.lower() for a in sys.argv[1:]}
    if args & {"--version", "-v", "version"}:
        print(f"Linux ISO Boot Installer {VERSION}")
        return 0
    if args & {"--help", "-h", "-?", "/?", "help"}:
        print(HELP, end="")
        return 0
    if "--selftest" in args:
        return selftest()
    if "--gui-check" in args:
        return launch_gui(build_only=True)
    if "--remove" in args:
        try:
            remove_previous(print)
        except ToolError as exc:
            print(f"ERROR: {exc}")
            return 1
        return 0
    return launch_gui()


if __name__ == "__main__":
    sys.exit(main())
