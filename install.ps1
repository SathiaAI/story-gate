# story-gate installer for Windows (PowerShell):
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/SathiaAI/story-gate/v0.8.0/install.ps1 | iex"
# It installs uv (Astral's Python tool manager) if you don't have it, then story-gate at the pinned
# release, puts it on your PATH, and runs nothing else. Read it before you run it: it's short.
$ErrorActionPreference = "Stop"
$ref = if ($env:STORY_GATE_REF) { $env:STORY_GATE_REF } else { "v0.8.0" }
$src = "git+https://github.com/SathiaAI/story-gate@$ref"
function Say($m) { Write-Host "story-gate install: $m" }

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Say "git is needed and isn't installed. Install git (https://git-scm.com/downloads), then run this again."
    exit 1
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Say "installing uv from https://astral.sh/uv (the official installer)"
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    if ($LASTEXITCODE -ne 0) { Say "the uv installer failed; see the messages above, then run this again"; exit 1 }
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
Say "installing story-gate $ref"
uv tool install --force --python 3.12 $src
if ($LASTEXITCODE -ne 0) { Say "uv couldn't install story-gate; see the messages above"; exit 1 }
$bin = (uv tool dir --bin).Trim()
uv tool update-shell | Out-Null
$pathNote = ($LASTEXITCODE -ne 0)
if ($pathNote) { Say "couldn't add story-gate to PATH for new terminals. Add this folder to your PATH yourself: $bin" }
$env:Path = "$bin;$env:Path"
story-gate *> $null
if ($LASTEXITCODE -ne 0) { Say "story-gate didn't start; see the messages above"; exit 1 }
if ($pathNote) { Say "done, except PATH (see above). In this window, run: story-gate try" } else { Say "done. Run: story-gate try   (new terminals find story-gate on their own)" }
