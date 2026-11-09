$ErrorActionPreference = "Stop"

$backendRoot = Split-Path $PSScriptRoot -Parent
$deployScript = Join-Path $backendRoot "deploy.ps1"
$artifactDirectory = Join-Path $backendRoot "deploy"
$python = if (Test-Path -LiteralPath (Join-Path $backendRoot ".venv/Scripts/python.exe") -PathType Leaf) {
    Join-Path $backendRoot ".venv/Scripts/python.exe"
} else {
    "python"
}
$startedAt = [DateTime]::UtcNow.AddSeconds(-1)
$tokens = $null
$parseErrors = $null
[System.Management.Automation.Language.Parser]::ParseFile(
    $deployScript,
    [ref]$tokens,
    [ref]$parseErrors
) | Out-Null
if ($parseErrors.Count -gt 0) {
    throw "部署脚本语法错误：$($parseErrors[0].Message)"
}

Push-Location $backendRoot
try {
    $pythonFingerprint = (& $python -c "from app.source_fingerprint import backend_source_fingerprint; print(backend_source_fingerprint())").Trim()
} finally {
    Pop-Location
}
if ($LASTEXITCODE -ne 0 -or $pythonFingerprint -notmatch '^[a-f\d]{16}$') {
    throw "Python 无法计算有效的后端源码指纹"
}
$scriptFingerprint = (& $deployScript -FingerprintOnly).Trim()
if ($scriptFingerprint -ne $pythonFingerprint) {
    throw "Python 与部署脚本的源码指纹不一致：$pythonFingerprint / $scriptFingerprint"
}

Push-Location (Join-Path $backendRoot "../web")
try {
    $viteConfigScript = "import('./vite.config.js').then(({default: config}) => process.stdout.write(JSON.parse(config.define.__ISCAN_BACKEND_FINGERPRINT__)))"
    $webFingerprint = (& node --input-type=module -e $viteConfigScript).Trim()
} finally {
    Pop-Location
}
if ($LASTEXITCODE -ne 0 -or $webFingerprint -notmatch '^[a-f\d]{16}$') {
    throw "Vite 无法计算有效的后端源码指纹"
}
if ($webFingerprint -ne $pythonFingerprint) {
    throw "Python 与 Vite 的源码指纹不一致：$pythonFingerprint / $webFingerprint"
}

& $deployScript
if (-not $?) {
    throw "部署脚本未能生成更新包"
}

$archive = Get-ChildItem -LiteralPath $artifactDirectory -File -Filter "scan-convert-backend-*.tar.gz" |
    Where-Object { $_.LastWriteTimeUtc -ge $startedAt } |
    Sort-Object LastWriteTimeUtc -Descending |
    Select-Object -First 1
if (-not $archive) {
    throw "未找到本次生成的后端更新包"
}

$checksumPath = "$($archive.FullName).sha256"
if (-not (Test-Path -LiteralPath $checksumPath -PathType Leaf)) {
    throw "部署包缺少 SHA-256 校验文件"
}
$checksum = (Get-Content -LiteralPath $checksumPath -Raw).Trim()
if ($checksum -notmatch '^[a-f\d]{64}$') {
    throw "部署包的 SHA-256 校验值无效"
}

$entries = tar.exe -tzf $archive.FullName
if ($LASTEXITCODE -ne 0) {
    throw "无法读取后端部署包"
}
if ($entries -notcontains "./app/main.py") {
    throw "部署包缺少后端应用源码"
}
$forbiddenEntries = @($entries | Where-Object {
    $_ -match '(^|/)(\.env[^/]*|storage|storage_metadata|__pycache__)(/|$)'
})
if ($forbiddenEntries.Count -gt 0) {
    throw "部署包包含环境或数据文件：$($forbiddenEntries -join ', ')"
}

Write-Host "部署包验证通过：$($archive.Name)"
Write-Host "Python、Vite 和部署脚本的源码指纹一致：$pythonFingerprint"
