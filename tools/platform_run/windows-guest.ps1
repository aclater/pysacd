# windows-guest.ps1: runs inside a Windows VM; windows-vm.sh sends it with the files it uses.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File windows-guest.ps1 -Version 0.5.5 `
#       -PythonVersion 3.13.17 -PythonSha256 <SHA-256 of python.org's amd64 installer>
#
# Expects tree.tar (the repository at the tag, without src/) and album.iso next to it, as
# lib.sh's make_payload writes them; works in its own folder. Needs no administrator rights:
# when the VM has no Python 3.10 or later, python.org's installer (checked by SHA-256) installs
# one for this user only, into a python folder here. Prints every step; the exit status is the
# number of steps that failed.
param([Parameter(Mandatory)][string]$Version, [Parameter(Mandatory)][string]$PythonVersion,
      [Parameter(Mandatory)][string]$PythonSha256)
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
Set-Location $PSScriptRoot
$os = Get-CimInstance Win32_OperatingSystem
"== $($os.Caption) $($os.Version) $env:PROCESSOR_ARCHITECTURE, $env:NUMBER_OF_PROCESSORS CPUs"
function Ok($p) { & $p -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>$null; return $LASTEXITCODE -eq 0 }
$py = Get-ChildItem "$env:ProgramFiles\Python3*\python.exe", "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending | Where-Object { Ok $_.FullName } | Select-Object -First 1 -ExpandProperty FullName
if (-not $py) {
    "== no Python 3.10 or later: installing python.org's $PythonVersion for this user"
    $exe = "python-$PythonVersion-amd64.exe"
    Invoke-WebRequest "https://www.python.org/ftp/python/$PythonVersion/$exe" -OutFile $exe -UseBasicParsing
    if ((Get-FileHash $exe -Algorithm SHA256).Hash -ne $PythonSha256) { "SHA-256 mismatch for $exe"; exit 1 }
    Start-Process -Wait ".\$exe" "/quiet InstallAllUsers=0 PrependPath=0 Include_test=0 Include_launcher=0 TargetDir=$PSScriptRoot\python"
    $py = "$PSScriptRoot\python\python.exe"
}
"== $(& $py -V)"
New-Item -ItemType Directory tree, work | Out-Null
tar -xf tree.tar -C tree; Move-Item album.iso work\
& $py -m venv venv
. .\venv\Scripts\Activate.ps1
python -m pip -q install --only-binary :all: "pysacd==$Version"; if ($LASTEXITCODE) { exit 1 }
python -m pip -q install --require-hashes -r tree\requirements-test.txt; if ($LASTEXITCODE) { exit 1 }
python -m pip list 2>$null | Select-String pysacd | ForEach-Object { "$_" }
$fails = 0
"== the $Version suite against the installed wheel"
Push-Location tree
$env:PYSACD_TEST_REPORT = "$PSScriptRoot\report.json"
# the one test that reads src/, which is left out so that the wheel is what's tested
$desel = 'tests/test_check_release_tag.py::CheckReleaseTag::test_this_repository_agrees_with_itself'
$out = python -m pytest -q -p no:cacheprovider "--junitxml=$PSScriptRoot\junit.xml" -rfEs --deselect $desel tests 2>&1
if ($LASTEXITCODE) { $fails++ }
$out | Select-Object -Last 80 | ForEach-Object { "$_" }
Remove-Item Env:PYSACD_TEST_REPORT
Pop-Location
"== tests/wheel_smoke.py"
Push-Location work
python -I ..\tree\tests\wheel_smoke.py 2>&1 | ForEach-Object { "$_" }
if ($LASTEXITCODE) { $fails++ }
Pop-Location
deactivate
# The README's Quick start is written for bash; these are its steps in PowerShell, with the same
# "You should see" checks.
"== README Quick start, in PowerShell"
Push-Location work
$before = @(Get-ChildItem -Directory | ForEach-Object Name)
& $py -m venv pysacd-env
. .\pysacd-env\Scripts\Activate.ps1
pip -q install "pysacd==$Version"
$toc = pysacd toc album.iso 2>&1 | ForEach-Object { "$_" }; $toc
$ext = pysacd-extract -2 -s -i album.iso 2>&1 | ForEach-Object { "$_" }; $ext
$new = @(Get-ChildItem -Directory | Where-Object { $before -notcontains $_.Name -and $_.Name -ne 'pysacd-env' })
$dsf = @($new | ForEach-Object { Get-ChildItem $_.FullName -Filter *.dsf })
$seen = 0
if ($toc -match '^Disc:') { $seen++ } else { "missing: Disc:" }
if ($ext -match 'Exporting DSF output in folder:') { $seen++ } else { "missing: Exporting DSF output in folder:" }
if ($new.Count -eq 1 -and $dsf.Count -eq 3) { $seen++ } else { "expected one new folder with 3 .dsf files, got $($new.Count) folders, $($dsf.Count) files" }
"README Quick start (PowerShell): checked $seen of 3 'You should see' items"
if ($seen -ne 3) { $fails++ }
deactivate
Pop-Location
"== failed steps: $fails"
exit $fails
