param(
    [string]$Project = "C:\Users\dasr3\Downloads\Dynamic_Profits_Starter\dynamic-profits-starter"
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $Project
if (-not (Test-Path -LiteralPath '.git')) { throw 'Run against your existing Git project.' }
$branch = git branch --show-current
if ($LASTEXITCODE -ne 0 -or $branch -ne 'main') { throw 'Expected your main branch.' }
git diff --quiet
if ($LASTEXITCODE -ne 0) { throw 'Existing tracked changes need review before installation.' }
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) { throw 'Existing staged changes need review before installation.' }
git pull --ff-only origin main
if ($LASTEXITCODE -ne 0) { throw 'Git pull failed. No release files copied.' }
py (Join-Path $PSScriptRoot 'final_release.py') --base-only --project $Project
if ($LASTEXITCODE -ne 0) { throw 'Installed V3 files differ from the supported migration source.' }

$manifest = Join-Path $PSScriptRoot 'FINAL_FILES.sha256'
$releaseFiles = @(Get-Content -LiteralPath $manifest | Where-Object { $_.Trim() } | ForEach-Object { ($_ -split '  ', 2)[1] })
$releaseFiles += 'FINAL_FILES.sha256'
foreach ($name in $releaseFiles) {
    if ([IO.Path]::IsPathRooted($name) -or (($name -split '[/\\]') -contains '..')) { throw 'Unsafe package path.' }
    $source = Join-Path $PSScriptRoot $name
    $target = Join-Path $Project $name
    if (-not (Test-Path -LiteralPath $source)) { throw "Package file missing: $name" }
    if (Test-Path -LiteralPath $target) {
        $existing = [IO.File]::ReadAllText($target).Replace("`r`n", "`n")
        $packaged = [IO.File]::ReadAllText($source).Replace("`r`n", "`n")
        if ($existing -cne $packaged) { throw "A different $name already exists. Preserve it and inspect before replacing." }
    }
}
foreach ($name in $releaseFiles) {
    $target = Join-Path $Project $name
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination $target -Force
}
py final_release.py --verify
if ($LASTEXITCODE -ne 0) { throw 'Release verification failed.' }
py -m unittest test_competition_v31 test_v31_deploy -q
if ($LASTEXITCODE -ne 0) { throw 'Final release tests failed. No commit or push performed.' }
git add -- $releaseFiles
if ($LASTEXITCODE -ne 0) { throw 'Git add failed.' }
git diff --cached --quiet
$stagedResult = $LASTEXITCODE
if ($stagedResult -eq 1) {
    git commit -m "Finalize V2.1 strategy with durable execution and V3 migration"
    if ($LASTEXITCODE -ne 0) { throw 'Git commit failed.' }
} elseif ($stagedResult -ne 0) { throw 'Could not inspect staged changes.' }
git push origin main
if ($LASTEXITCODE -ne 0) { throw 'Git push failed; keep the local commit and retry the push.' }
Write-Host 'FINAL RELEASE COMMITTED AND PUSHED. Next run DEPLOY_FINAL_AWS.sh on AWS.'
