# Spelunkr Bridge - Windows setup
# Installs what the bridge needs, then sets it to run in the background every time you log in.
#   install-windows.bat                 set up and start
#   install-windows.bat -Uninstall      stop it and remove the background task
#   install-windows.bat -NoAutostart    only install the requirements
#   install-windows.bat -Port 9000      use another port (default 8765)
#   install-windows.bat -Lan            also let other devices open the status page / streams directly
param([switch]$Uninstall, [switch]$NoAutostart, [switch]$Lan, [int]$Port = 8765)

$ErrorActionPreference = "Stop"
$TaskName = "Spelunkr Bridge"
$Here = $PSScriptRoot
$Script = Join-Path $Here "spelunkr2jellyfin.py"
$LogFile = Join-Path $Here "spelunkr.log"

function Say($msg, $color = "Gray") { Write-Host $msg -ForegroundColor $color }
function Step($msg) { Write-Host ""; Write-Host "== $msg" -ForegroundColor Cyan }
function Ask($msg) {
    $r = Read-Host "$msg [Y/n]"
    return ($r -eq "" -or $r -match "^[yY]")
}

function Stop-Bridge {
    # stop the bridge and the ffmpeg / yt-dlp processes it started
    Get-CimInstance Win32_Process -Filter "Name like 'python%.exe'" |
        Where-Object { $_.CommandLine -like "*spelunkr2jellyfin.py*" } |
        ForEach-Object { & taskkill.exe /T /F /PID $_.ProcessId 2>&1 | Out-Null }
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop } catch {}
}

if ($Uninstall) {
    Step "Removing Spelunkr Bridge"
    Stop-Bridge
    try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop; Say "Background task removed." "Green" }
    catch { Say "No background task was set up." }
    Say "Python, yt-dlp and Node.js were left installed. Delete this folder to remove the rest."
    exit 0
}

if (-not (Test-Path $Script)) { Say "Can't find spelunkr2jellyfin.py next to this installer." "Red"; exit 1 }

# ---------------------------------------------------------------- Python
Step "Python"
function Find-Python {
    $tries = @(@("py", "-3"), @("python"))
    foreach ($t in $tries) {
        if (-not (Get-Command $t[0] -ErrorAction SilentlyContinue)) { continue }
        try {
            $args2 = @($t | Select-Object -Skip 1) + @("-c", "import sys; print(sys.version_info >= (3, 8)); print(sys.executable)")
            $out = & $t[0] @args2 2>$null
            if ($LASTEXITCODE -eq 0 -and $out -and $out[0] -eq "True") { return $out[1] }
        } catch {}
    }
    foreach ($p in (Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue)) {
        return $p.FullName
    }
    return $null
}
$Python = Find-Python
if (-not $Python) {
    Say "Python 3 isn't installed."
    if ((Get-Command winget -ErrorAction SilentlyContinue) -and (Ask "Install Python 3.12 now with winget?")) {
        winget install -e --id Python.Python.3.12 --scope user
        $Python = Find-Python
    }
    if (-not $Python) { Say "Install Python 3 from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run this again." "Red"; exit 1 }
}
$PythonW = Join-Path (Split-Path $Python) "pythonw.exe"
if (-not (Test-Path $PythonW)) { $PythonW = $Python }
Say "Using $Python" "Green"

# ---------------------------------------------------------------- yt-dlp
Step "yt-dlp (for the YouTube channels)"
& $Python -m pip install -q -U --disable-pip-version-check "yt-dlp[default]"
if ($LASTEXITCODE -ne 0) { & $Python -m pip install -q -U --user --disable-pip-version-check "yt-dlp[default]" }
if ($LASTEXITCODE -ne 0) { Say "Couldn't install yt-dlp. Hack Fraud TV and EISTV won't play; the Spelunkr channels still will." "Yellow" }
else { Say ("yt-dlp " + (& $Python -m yt_dlp --version)) "Green" }

# ---------------------------------------------------------------- JavaScript runtime
Step "Node.js (YouTube needs it to unlock video streams)"
if (Get-Command node, deno -ErrorAction SilentlyContinue) { Say "Found." "Green" }
elseif ((Get-Command winget -ErrorAction SilentlyContinue) -and (Ask "Node.js isn't installed. Install it now with winget?")) {
    winget install -e --id OpenJS.NodeJS.LTS
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
} else { Say "Skipped. Some YouTube videos may fail to play without it." "Yellow" }

# ---------------------------------------------------------------- ffmpeg
Step "ffmpeg"
$ff = & $Python -c "import importlib.util as u; s=u.spec_from_file_location('s', r'$Script'); m=u.module_from_spec(s); s.loader.exec_module(m); print(m.find_tool('ffmpeg'))"
if ($ff -and ((Test-Path $ff) -or (Get-Command $ff -ErrorAction SilentlyContinue))) { Say "Using $ff" "Green" }
else {
    Say "Can't find ffmpeg. Install Jellyfin (it comes with ffmpeg) or put ffmpeg.exe and ffprobe.exe on PATH." "Red"
    exit 1
}

if ($NoAutostart) {
    Step "Done"
    Say "Requirements are installed. Start it with start-spelunkr.bat."
    exit 0
}

# ---------------------------------------------------------------- background task
Step "Starting in the background"
Stop-Bridge
$hostArg = if ($Lan) { "0.0.0.0" } else { "127.0.0.1" }
$argLine = "`"$Script`" --host $hostArg --port $Port --log-file `"$LogFile`""
$action = New-ScheduledTaskAction -Execute $PythonW -Argument $argLine -WorkingDirectory $Here
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Serves spelunkr.tv channels to Jellyfin (M3U + XMLTV). Log: $LogFile" -Force | Out-Null
if (Test-Path $LogFile) { Remove-Item $LogFile -Force -ErrorAction SilentlyContinue }
Start-ScheduledTask -TaskName $TaskName
Say "Task '$TaskName' will start it every time you log in." "Green"

Say "Loading the channel lineup (the first start takes a few minutes while it reads the YouTube playlists)..."
$url = "http://127.0.0.1:$Port"
$ok = $false
for ($i = 0; $i -lt 120; $i++) {
    Start-Sleep -Seconds 3
    try { Invoke-WebRequest "$url/playlist.m3u" -UseBasicParsing -TimeoutSec 5 | Out-Null; $ok = $true; break } catch {}
    $t = (Get-ScheduledTask -TaskName $TaskName).State
    if ($t -ne "Running" -and $i -gt 3) { break }
    if ($i % 10 -eq 9) { Write-Host "  still loading..." }
}
if (-not $ok) {
    Say "It didn't come up. Last lines of $($LogFile):" "Red"
    if (Test-Path $LogFile) { Get-Content $LogFile -Tail 15 }
    exit 1
}

Step "Ready - now add it to Jellyfin"
Say "In Jellyfin: Dashboard > Live TV"
Say "  1. Tuner Devices > +  > type 'M3U Tuner', File or URL:"
Say "       http://localhost:$Port/playlist.m3u" "Yellow"
Say "  2. TV Guide Data Providers > +  > XMLTV, File or URL:"
Say "       http://localhost:$Port/epg.xml" "Yellow"
Say "  3. Back on the Live TV page, click 'Refresh Guide Data'."
Say ""
Say "What's on now: $url    Log: $LogFile"
Say "To stop it for good: install-windows.bat -Uninstall"
