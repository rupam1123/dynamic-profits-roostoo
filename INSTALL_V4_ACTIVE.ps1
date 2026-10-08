param([string]$Project = "$env:USERPROFILE\Downloads\Dynamic_Profits_Starter\dynamic-profits-starter")
$ErrorActionPreference = 'Stop'
$Source = $PSScriptRoot
if (-not (Test-Path (Join-Path $Project 'competition_v31\controller.py'))) { throw 'Existing V3.1 project required. Set -Project to its path.' }
Set-Location $Source
py .\verify_v4_active.py
if ($LASTEXITCODE -ne 0) { throw 'Release checksum verification failed.' }
$manifest = Get-Content (Join-Path $Source 'V4_ACTIVE_FILES.json') -Raw | ConvertFrom-Json
$files = @($manifest.PSObject.Properties.Name) + @('V4_ACTIVE_FILES.json')
# Validate existing legacy dependencies before copying anything into the project.
foreach ($file in $files) {
    if ($file.StartsWith('competition_v4/') -or $file.StartsWith('competition_v32/') -or $file.StartsWith('v3_tests/')) {
        $target = Join-Path $Project $file
        if (Test-Path $target) {
            $left = [IO.File]::ReadAllText($target).Replace("`r`n","`n")
            $right = [IO.File]::ReadAllText((Join-Path $Source $file)).Replace("`r`n","`n")
            if ($left -cne $right) { throw "Existing legacy dependency differs: $file. Preserve it; do not overwrite an active release." }
        }
    }
}
foreach ($file in $files) {
    $target = Join-Path $Project $file
    New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
    if ([IO.Path]::GetFullPath((Join-Path $Source $file)) -ne [IO.Path]::GetFullPath($target)) {
        Copy-Item -LiteralPath (Join-Path $Source $file) -Destination $target -Force
    }
}
Set-Location $Project
py .\verify_v4_active.py
if ($LASTEXITCODE -ne 0) { throw 'Installed release verification failed.' }
py -m unittest test_competition_v4a test_v4a_selection test_v4a_runtime test_v4a_deploy test_v4a_management test_v4a_fast_orders test_v4a_activity -q
if ($LASTEXITCODE -ne 0) { throw 'Tests failed; nothing pushed.' }
$branch = git branch --show-current
if ($LASTEXITCODE -ne 0 -or $branch.Trim() -ne 'main') { throw 'Run from the existing main branch; inspect Git state first.' }
$staged = git diff --cached --name-only
if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect Git index.' }
if ($staged) { throw 'Other staged files exist. Commit or unstage them first so the release commit is isolated.' }
git add -- $files
if ($LASTEXITCODE -ne 0) { throw 'Git add failed.' }
git diff --cached --quiet
if ($LASTEXITCODE -eq 1) {
    git commit -m "Update V4 entry capacity, cost targets and fresh-signal re-entry"
    if ($LASTEXITCODE -ne 0) { throw 'Commit failed.' }
} elseif ($LASTEXITCODE -ne 0) { throw 'Cannot inspect staged changes.' }
git push origin main
if ($LASTEXITCODE -ne 0) { throw 'Push failed. Preserve files and inspect output.' }
Write-Host 'V4 COMMITTED AND PUSHED. Next run CHECK_V4_ACTIVE_AWS.sh on AWS. The live service has not changed yet.'
