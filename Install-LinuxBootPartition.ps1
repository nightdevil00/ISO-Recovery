#Requires -Version 5.1
<#
.SYNOPSIS
    Boot a Linux ISO (archiso-family: Arch, CachyOS, Omarchy, EndeavourOS, ...)
    directly from an internal disk, with no USB stick.

.DESCRIPTION
    Creates two small partitions out of a disk's unallocated space:

      [ FAT32 loader ]  ~1 GB   - EFI/BOOT/BOOTx64.EFI taken from the ISO, plus a
                                  firmware boot entry pointing at it.
      [ NTFS ISO store ] ~8 GB  - the untouched .iso file, the archiso marker file,
                                  and a grub.cfg that loopback-boots the ISO.

    It leaves the rest of the free space unallocated for the real installation.

    Nothing on an existing partition is modified. Only the unallocated tail of the
    chosen disk is used.

.PARAMETER IsoPath
    Path to the .iso. Defaults to the most recent *.iso in Downloads.

.PARAMETER DiskNumber
    Target disk for the two new partitions. Default: 0.

.PARAMETER LoaderSizeGB
    Size of the FAT32 loader partition. Default: 1.

.PARAMETER StoreSizeGB
    Size of the NTFS ISO store. Default: ISO size + 2 GB, minimum 6.

.PARAMETER StoreFileSystem
    NTFS (default) or exFAT. Both handle files larger than 4 GiB.

.PARAMETER EntryPath
    EFI path the firmware entry points at. Default: \EFI\BOOT\BOOTx64.EFI

.PARAMETER DryRun
    Report exactly what would happen, change nothing.

.PARAMETER Remove
    Undo: delete the firmware boot entry and both partitions.

.EXAMPLE
    .\Install-LinuxBootPartition.ps1 -DryRun

.EXAMPLE
    .\Install-LinuxBootPartition.ps1 -IsoPath C:\Users\SANDO\Downloads\omarchy-4.0.6.iso

.EXAMPLE
    .\Install-LinuxBootPartition.ps1 -Remove
#>
[CmdletBinding(DefaultParameterSetName = 'Install')]
param(
    [Parameter(ParameterSetName = 'Install')]
    [string]$IsoPath,

    [Parameter(ParameterSetName = 'Install')]
    [int]$DiskNumber = 0,

    [Parameter(ParameterSetName = 'Install')]
    [int]$LoaderSizeGB = 1,

    [Parameter(ParameterSetName = 'Install')]
    [int]$StoreSizeGB = 0,

    [Parameter(ParameterSetName = 'Install')]
    [ValidateSet('NTFS', 'exFAT')]
    [string]$StoreFileSystem = 'NTFS',

    [Parameter(ParameterSetName = 'Install')]
    [string]$EntryPath = '\EFI\BOOT\BOOTx64.EFI',

    [Parameter(ParameterSetName = 'Install')]
    [switch]$DryRun,

    [Parameter(Mandatory = $true, ParameterSetName = 'Remove')]
    [switch]$Remove,

    [switch]$Force
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$StateDir  = Join-Path $env:ProgramData 'LinuxBootPartition'
$StateFile = Join-Path $StateDir 'state.json'
$EntryDesc = 'Omarchy (live ISO on disk)'

# ---------------------------------------------------------------- helpers ---

function Write-Step   { param([string]$m) Write-Host "`n==> $m" -ForegroundColor Cyan }
function Write-Detail { param([string]$m) Write-Host "    $m" }
function Write-Warn   { param([string]$m) Write-Host "    ! $m" -ForegroundColor Yellow }
function Write-Ok     { param([string]$m) Write-Host "    + $m" -ForegroundColor Green }

function Test-Elevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-MountLetter {
    param([int]$Disk, [int]$Partition)
    $v = Get-Partition -DiskNumber $Disk -PartitionNumber $Partition |
         Get-Volume -ErrorAction SilentlyContinue
    if ($v -and $v.DriveLetter) { "$($v.DriveLetter):" } else { $null }
}

function Resolve-Iso {
    param([string]$Path)
    if (-not $Path) {
        $Path = Get-ChildItem "$env:USERPROFILE\Downloads" -Filter *.iso -ErrorAction SilentlyContinue |
                Sort-Object LastWriteTime -Descending |
                Select-Object -First 1 -ExpandProperty FullName
        if (-not $Path) { throw "No -IsoPath given and no .iso found in Downloads." }
    }
    if (-not (Test-Path -LiteralPath $Path)) { throw "ISO not found: $Path" }
    (Resolve-Path -LiteralPath $Path).Path
}

# --------------------------------------------------------- ISO inspection ---

function Get-IsoLayout {
    <#
      Mounts the ISO read-only and works out how to boot it.
      Returns kernel/initrd paths, the archiso basedir, and the marker file.
      Unmounts the ISO before returning.
    #>
    param([string]$Path)

    $image = $null
    try {
        $image = Mount-DiskImage -ImagePath $Path -PassThru
        $vol   = $image | Get-Volume
        if (-not $vol.DriveLetter) { throw "Mounted ISO has no drive letter." }
        $root  = "$($vol.DriveLetter):\"

        # --- archiso basedir: the directory holding <basedir>/x86_64/airootfs.sfs
        $sfs = Get-ChildItem -LiteralPath $root -Recurse -Filter 'airootfs.sfs' -File -ErrorAction SilentlyContinue |
               Where-Object { $_.Directory.Name -eq 'x86_64' } |
               Select-Object -First 1
        if (-not $sfs) {
            throw "This does not look like an archiso image (no <basedir>/x86_64/airootfs.sfs)."
        }
        $basedir = $sfs.Directory.Parent.Name

        $bootDir = Join-Path $sfs.Directory.Parent.FullName 'boot'
        $kernel  = Get-ChildItem -LiteralPath $bootDir -Recurse -Filter 'vmlinuz*' -File -ErrorAction SilentlyContinue |
                   Where-Object { $_.Directory.Name -eq 'x86_64' } | Select-Object -First 1
        $initrd  = Get-ChildItem -LiteralPath $bootDir -Recurse -Filter 'initramfs*.img' -File -ErrorAction SilentlyContinue |
                   Where-Object { $_.Directory.Name -eq 'x86_64' } | Select-Object -First 1
        if (-not $kernel) { throw "No vmlinuz found under /$basedir/boot/x86_64/." }
        if (-not $initrd) { throw "No initramfs found under /$basedir/boot/x86_64/." }

        # --- the EFI GRUB binary the firmware will load
        $efi = Join-Path $root 'EFI\BOOT\BOOTx64.EFI'
        if (-not (Test-Path -LiteralPath $efi)) { throw "ISO has no EFI\BOOT\BOOTx64.EFI." }

        # --- archiso marker file: /boot/<uuid>.uuid, used by the EFI GRUB to find us
        $marker = Get-ChildItem -LiteralPath (Join-Path $root 'boot') -Filter '*.uuid' -File -ErrorAction SilentlyContinue |
                  Select-Object -First 1
        if (-not $marker) { throw "No archiso marker file (/boot/*.uuid) found on the ISO." }

        # --- sanity: confirm the EFI binary really does marker-file discovery
        $bytes = [IO.File]::ReadAllBytes($efi)
        $ascii = [Text.Encoding]::ASCII.GetString($bytes)
        $hasHint = $ascii -match 'ARCHISO_HINT' -and $ascii -match [regex]::Escape($marker.Name)
        $hasLoop = $ascii -match 'loopback'
        if (-not $hasHint) { throw "EFI/BOOT/BOOTx64.EFI does not reference '$($marker.Name)'; unsupported ISO layout." }
        if (-not $hasLoop) { throw "EFI/BOOT/BOOTx64.EFI has no loopback module; unsupported ISO build." }

        [pscustomobject]@{
            IsoPath     = $Path
            Root        = $root
            Label       = $vol.FileSystemLabel
            Basedir     = $basedir
            KernelPath  = '/' + ($kernel.FullName.Substring($root.Length) -replace '\\', '/')
            InitrdPath  = '/' + ($initrd.FullName.Substring($root.Length) -replace '\\', '/')
            EfiSource   = $efi
            MarkerName  = $marker.Name
            MarkerSource= $marker.FullName
            IsoSizeGB   = (Get-Item -LiteralPath $Path).Length / 1GB
        }
    }
    finally {
        if ($image) { Dismount-DiskImage -ImagePath $Path -ErrorAction SilentlyContinue | Out-Null }
    }
}

# ------------------------------------------------------------ grub.cfg gen ---

function New-GrubConfig {
    param($Layout, [string]$IsoFileName)

    $basedir = $Layout.Basedir
    $kernel  = $Layout.KernelPath
    $initrd  = $Layout.InitrdPath

    @"
# Generated by Install-LinuxBootPartition.ps1 -- safe to edit.
#
# The firmware loads EFI/BOOT/BOOTx64.EFI from the FAT32 loader partition.
# That GRUB searches for /boot/$($Layout.MarkerName) and then loads THIS file
# from whichever partition contains it, exporting:
#     ARCHISO_HINT = that GRUB device, e.g. (hd0,gpt4)
#     ARCHISO_UUID = its filesystem UUID
# We loopback-mount the ISO and hand those to the archiso initramfs.

set timeout=10
set default=0

set isofile=/$( $IsoFileName )

insmod part_gpt
insmod loopback
insmod search_fs_file
insmod ntfs
insmod exfat

# Self-heal: the bootstrap normally exports both of these for us.
if [ -z "`${ARCHISO_HINT}" ]; then
    search --no-floppy --set=ARCHISO_HINT --file /boot/$($Layout.MarkerName)
fi
if [ -z "`${ARCHISO_UUID}" ]; then
    probe --set=ARCHISO_UUID --fs-uuid "`${ARCHISO_HINT}"
fi

menuentry "Omarchy  -  boot live ISO from disk" --id omarchy-live {
    set gfxpayload=keep
    search --no-floppy --set=isodev --file `${isofile}
    if [ -z "`${isodev}" ]; then
        echo "Could not find `${isofile} on any partition."
        sleep 15
    fi
    loopback loopdev (`${isodev})`${isofile}
    linux (loopdev)$kernel archisobasedir=$basedir img_dev=/dev/disk/by-label/OMARCHY_ISO img_loop=`${isofile} quiet splash initramfs_async=0
    initrd (loopdev)$initrd
}

menuentry "Omarchy  -  alternate (mount store by UUID)" --id omarchy-uuid {
    set gfxpayload=keep
    loopback loopdev (`${ARCHISO_HINT})`${isofile}
    linux (loopdev)$kernel archisobasedir=$basedir img_dev=UUID=`${ARCHISO_UUID} img_loop=`${isofile} quiet splash initramfs_async=0
    initrd (loopdev)$initrd
}

menuentry "Show what GRUB sees (diagnostics)" {
    echo "ARCHISO_HINT = `${ARCHISO_HINT}"
    echo "ARCHISO_UUID = `${ARCHISO_UUID}"
    echo "isofile      = `${isofile}"
    echo ""
    ls
    echo ""
    echo "Returning in 20s."
    sleep 20
}

menuentry "GRUB command line" { commandline }
menuentry "Reboot"              { reboot }
menuentry "Shut down"           { halt }
"@
}

# ------------------------------------------------------------- BCD / NVRAM ---

function New-BootEntry {
    param([string]$DriveLetter, [string]$Path)

    Write-Step "Registering the firmware boot entry"

    $copyOut = & bcdedit /copy '{bootmgr}' /d "$EntryDesc" 2>&1
    if ($LASTEXITCODE -ne 0) { throw "bcdedit /copy failed: $copyOut" }

    $guid = [regex]::Match(($copyOut -join ' '), '\{[0-9a-fA-F-]{36}\}').Value
    if (-not $guid) { throw "Could not parse new boot entry GUID from: $copyOut" }

    & bcdedit /set $guid device "partition=$DriveLetter" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Failed to set 'device' on $guid" }

    & bcdedit /set $guid path "$Path" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Failed to set 'path' on $guid" }

    & bcdedit /set $guid description "$EntryDesc" | Out-Null
    & bcdedit /set '{fwbootmgr}' displayorder $guid /addfirst | Out-Null

    Write-Ok "Boot entry $guid -> $DriveLetter$Path"
    $guid
}

# ---------------------------------------------------------------- install ---

function Invoke-Install {
    $fw = (Get-ComputerInfo -Property BiosFirmwareType).BiosFirmwareType
    if ($fw -ne 'Uefi') { throw "This machine is not in UEFI mode ($fw). This tool is UEFI-only." }

    Write-Step "Checking Secure Boot"
    $secureBoot = $null
    try { $secureBoot = Confirm-SecureBootUEFI } catch { $secureBoot = $null }
    if ($secureBoot -eq $true) {
        Write-Warn "Secure Boot is ENABLED. The ISO's GRUB (EFI/BOOT/BOOTx64.EFI) is not Microsoft-signed"
        Write-Warn "and will be rejected. You must turn Secure Boot off in firmware before this can boot."
    }
    elseif ($secureBoot -eq $false) { Write-Ok "Secure Boot is off." }
    else { Write-Warn "Could not read Secure Boot state (needs Administrator) - check it manually." }

    $iso = Resolve-Iso -Path $IsoPath
    Write-Step "Inspecting ISO"
    Write-Detail $iso
    $layout = Get-IsoLayout -Path $iso
    Write-Ok  "label      : $($layout.Label)"
    Write-Ok  "basedir    : /$($layout.Basedir)"
    Write-Ok  "kernel     : $($layout.KernelPath)"
    Write-Ok  "initramfs  : $($layout.InitrdPath)"
    Write-Ok  "marker     : /boot/$($layout.MarkerName)"
    Write-Ok  "size       : $([math]::Round($layout.IsoSizeGB,2)) GB"

    $isoFileName = Split-Path -Leaf $iso

    Write-Step "Checking disk $DiskNumber"
    $disk = Get-Disk -Number $DiskNumber
    Write-Detail "$($disk.FriendlyName)  $([math]::Round($disk.Size/1GB,1)) GB  $($disk.PartitionStyle)"
    if ($disk.PartitionStyle -ne 'GPT') { throw "Disk $DiskNumber is not GPT (UEFI needs GPT)." }

    $sysDrive = $env:SystemDrive.TrimEnd(':')
    $bootDisk = (Get-Partition -DriveLetter $sysDrive -ErrorAction SilentlyContinue).DiskNumber
    if ($bootDisk -eq $DiskNumber) {
        Write-Warn "Disk $DiskNumber also holds $($env:SystemDrive) -- Windows. Only unallocated space is touched,"
        Write-Warn "and that is fine, but back up the BitLocker recovery key first."
    }

    $freeGB = ($disk.Size - $disk.AllocatedSize) / 1GB
    Write-Detail ("unallocated: {0:N1} GB" -f $freeGB)

    $storeGB = if ($StoreSizeGB -gt 0) { $StoreSizeGB }
               else { [Math]::Ceiling($layout.IsoSizeGB) + 2 }
    if ($storeGB -lt 6) { $storeGB = 6 }

    $needGB = $LoaderSizeGB + $storeGB
    if ($freeGB -lt ($needGB + 2)) {
        throw ("Need {0} GB ({1} loader + {2} store) plus headroom, but only {3:N1} GB is unallocated on disk {4}." -f
               $needGB, $LoaderSizeGB, $storeGB, $freeGB, $DiskNumber)
    }

    Write-Step "Planned layout"
    Write-Detail ("loader : {0} GB FAT32  (label OMARCHYBOOT)" -f $LoaderSizeGB)
    Write-Detail ("store  : {0} GB {1}     (label OMARCHY_ISO)" -f $storeGB, $StoreFileSystem)
    Write-Detail ("left free for Omarchy: {0:N1} GB" -f ($freeGB - $needGB))

    if ($DryRun) {
        Write-Host "`nDry run: nothing was changed." -ForegroundColor Yellow
        return
    }

    if (-not (Test-Elevated)) {
        throw "Run this in an elevated (Administrator) PowerShell to make changes. (-DryRun works unelevated.)"
    }
    if ($secureBoot -eq $true -and -not $Force) {
        throw "Secure Boot is enabled. Disable it in firmware setup, then re-run (or pass -Force to override)."
    }

    if (-not $Force) {
        $ans = Read-Host "`nProceed? Type YES to continue"
        if ($ans -ne 'YES') { Write-Host 'Aborted.'; return }
    }

    New-Item -ItemType Directory -Path $StateDir -Force | Out-Null

    # ---- 1. partitions
    Write-Step "Creating partitions"
    $loaderPart = New-Partition -DiskNumber $DiskNumber -Size ($LoaderSizeGB * 1GB) -AssignDriveLetter
    $loaderVol  = Format-Volume -Partition $loaderPart -FileSystem FAT32 -NewFileSystemLabel 'OMARCHYBOOT' -Confirm:$false
    $loaderLetter = "$($loaderVol.DriveLetter):"
    Write-Ok "loader partition $($loaderPart.PartitionNumber) -> $loaderLetter (FAT32)"

    $storePart = New-Partition -DiskNumber $DiskNumber -Size ($storeGB * 1GB) -AssignDriveLetter
    $storeVol  = Format-Volume -Partition $storePart -FileSystem $StoreFileSystem -NewFileSystemLabel 'OMARCHY_ISO' -Confirm:$false
    $storeLetter = "$($storeVol.DriveLetter):"
    Write-Ok "store partition $($storePart.PartitionNumber) -> $storeLetter ($StoreFileSystem)"
    # grub.cfg mounts the store by this label, so it must be exact.
    if ($storeVol.FileSystemLabel -ne 'OMARCHY_ISO') {
        throw "Store volume label came out as '$($storeVol.FileSystemLabel)', expected OMARCHY_ISO."
    }

    # ---- 2. EFI loader
    Write-Step "Installing the EFI loader on $loaderLetter"
    $efiDestDir = Join-Path $loaderLetter '\EFI\BOOT'
    New-Item -ItemType Directory -Path $efiDestDir -Force | Out-Null
    Copy-Item -LiteralPath $layout.EfiSource -Destination (Join-Path $efiDestDir 'BOOTx64.EFI') -Force
    Write-Ok "EFI\BOOT\BOOTx64.EFI"

    # ---- 3. ISO store: the ISO, the marker, and our grub.cfg
    Write-Step "Copying the ISO to $storeLetter (this takes a while)"
    $isoDest = Join-Path $storeLetter "\$isoFileName"
    Copy-Item -LiteralPath $iso -Destination $isoDest -Force
    Write-Ok "$isoFileName"

    $markerDestDir = Join-Path $storeLetter '\boot'
    New-Item -ItemType Directory -Path $markerDestDir -Force | Out-Null
    Copy-Item -LiteralPath $layout.MarkerSource -Destination (Join-Path $markerDestDir $layout.MarkerName) -Force
    Write-Ok "boot\$($layout.MarkerName)  (the marker GRUB searches for)"

    $grubDir = Join-Path $markerDestDir 'grub'
    New-Item -ItemType Directory -Path $grubDir -Force | Out-Null
    $cfgPath = Join-Path $grubDir 'grub.cfg'
    New-GrubConfig -Layout $layout -IsoFileName $isoFileName | Set-Content -LiteralPath $cfgPath -Encoding ASCII
    Write-Ok "boot\grub\grub.cfg"

    # ---- 4. firmware entry
    $guid = New-BootEntry -DriveLetter $loaderLetter -Path $EntryPath

    # ---- 5. remember how to undo it
    [pscustomobject]@{
        CreatedUtc      = (Get-Date).ToUniversalTime().ToString('o')
        EntryGuid       = $guid
        EntryDescription= $EntryDesc
        DiskNumber      = $DiskNumber
        LoaderPartition = $loaderPart.PartitionNumber
        StorePartition  = $storePart.PartitionNumber
        LoaderLetter    = $loaderLetter
        StoreLetter     = $storeLetter
        IsoPath         = $iso
    } | ConvertTo-Json | Set-Content -LiteralPath $StateFile -Encoding UTF8

    Write-Step "Done"
    Write-Host @"
    Reboot and open the firmware boot menu (usually F12 / F11 / Esc).
    Pick "$EntryDesc".

    You should land in the Omarchy live environment. From there, install to the
    ~$([math]::Round($freeGB - $needGB)) GB of remaining free space.

    When you install the bootloader, you can point /efi at the $LoaderSizeGB GB FAT32
    loader partition (it is already FAT32), or let the installer make its own.
    If you use a fresh ESP, re-run with -EntryPath pointing at the new loader.

    To undo everything:  .\Install-LinuxBootPartition.ps1 -Remove
"@ -ForegroundColor Green
}

# ----------------------------------------------------------------- remove ---

function Invoke-Remove {
    if (-not (Test-Elevated)) { throw "Run this in an elevated (Administrator) PowerShell." }
    if (-not (Test-Path -LiteralPath $StateFile)) { throw "No state file at $StateFile -- nothing recorded to remove." }

    $state = Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json

    Write-Step "Removing firmware boot entry"
    if ($state.EntryGuid) {
        & bcdedit /delete $state.EntryGuid 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { Write-Ok "deleted $($state.EntryGuid)" }
        else { Write-Warn "bcdedit could not delete $($state.EntryGuid) (already gone?)" }
    }

    Write-Step "Removing partitions"
    foreach ($pn in @($state.StorePartition, $state.LoaderPartition) | Where-Object { $_ }) {
        try {
            Remove-Partition -DiskNumber $state.DiskNumber -PartitionNumber $pn -Confirm:$false
            Write-Ok "partition $pn on disk $($state.DiskNumber) removed"
        } catch {
            Write-Warn "could not remove partition $pn : $($_.Exception.Message)"
        }
    }

    Remove-Item -LiteralPath $StateFile -Force -ErrorAction SilentlyContinue
    Write-Step "Done"
    Write-Host "    The space is unallocated again. Windows was never touched." -ForegroundColor Green
}

# ------------------------------------------------------------------- main ---

try {
    if ($PSCmdlet.ParameterSetName -eq 'Remove') { Invoke-Remove } else { Invoke-Install }
}
catch {
    Write-Host "`nFAILED: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
