[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$Server,
    [string]$User,
    [string]$RemotePath = "/opt/iscan/backend",
    [int]$SshPort = 22,
    [string]$IdentityFile,
    [switch]$FingerprintOnly,
    [switch]$Deploy,
    [switch]$HotUpdate,
    [string]$ContainerName = "scan-convert-api"
)

$ErrorActionPreference = "Stop"

if ($Deploy -and [string]::IsNullOrWhiteSpace($Server)) {
    throw "使用 -Deploy 时必须提供 -Server，例如 -Server example.com"
}
if ($HotUpdate -and -not $Deploy) {
    throw "使用 -HotUpdate 时必须同时指定 -Deploy"
}
if ($ContainerName -notmatch '^[A-Za-z0-9_.-]+$') {
    throw "ContainerName 只能包含字母、数字、点、下划线和短横线"
}
if ($RemotePath -notmatch '^[A-Za-z0-9_./-]+$') {
    throw "RemotePath 只能包含字母、数字、下划线、点、斜杠和短横线"
}
if ($Deploy -and $IdentityFile -and -not (Test-Path -LiteralPath $IdentityFile -PathType Leaf)) {
    throw "SSH 私钥不存在：$IdentityFile"
}

function Get-BackendSourceFingerprint {
    $backendRoot = $PSScriptRoot
    $appRoot = Join-Path $backendRoot "app"
    $relativePaths = @(
        Get-ChildItem -LiteralPath $appRoot -File -Filter "*.py" -Recurse |
            ForEach-Object {
                $relativePath = $_.FullName.Substring($backendRoot.Length)
                $relativePath.TrimStart([char[]]@('\', '/')).Replace('\', '/')
            }
    )
    [Array]::Sort($relativePaths, [StringComparer]::Ordinal)
    $relativePaths += "requirements.txt"
    $fingerprint = [Security.Cryptography.IncrementalHash]::CreateHash(
        [Security.Cryptography.HashAlgorithmName]::SHA256
    )
    $separator = [byte[]]@(0)

    try {
        foreach ($relativePath in $relativePaths) {
            $sourcePath = Join-Path $backendRoot ($relativePath.Replace('/', [IO.Path]::DirectorySeparatorChar))
            $normalizedSource = [IO.File]::ReadAllText($sourcePath, [Text.Encoding]::UTF8).Replace("`r`n", "`n")
            $fingerprint.AppendData([Text.Encoding]::UTF8.GetBytes($relativePath))
            $fingerprint.AppendData($separator)
            $fingerprint.AppendData([Text.Encoding]::UTF8.GetBytes($normalizedSource))
            $fingerprint.AppendData($separator)
        }
        $digest = $fingerprint.GetHashAndReset()
        return [BitConverter]::ToString($digest).Replace('-', '').ToLowerInvariant().Substring(0, 16)
    } finally {
        $fingerprint.Dispose()
    }
}

$expectedFingerprint = Get-BackendSourceFingerprint
if ($FingerprintOnly) {
    Write-Output $expectedFingerprint
    return
}

$backendRoot = $PSScriptRoot
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$artifactDirectory = Join-Path $backendRoot "deploy"
$stagingDirectory = Join-Path ([IO.Path]::GetTempPath()) "scan-convert-backend-$([guid]::NewGuid().ToString('N'))"
$archive = Join-Path $artifactDirectory "scan-convert-backend-$stamp.tar.gz"
$hashFile = "$archive.sha256"
$files = @(
    "app",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.production.yml",
    "requirements.txt",
    "run.py",
    "README.md"
)

try {
    New-Item -ItemType Directory -Path $artifactDirectory -Force | Out-Null
    New-Item -ItemType Directory -Path $stagingDirectory -Force | Out-Null

    foreach ($relativePath in $files) {
        $source = Join-Path $backendRoot $relativePath
        if (-not (Test-Path -LiteralPath $source)) {
            throw "部署文件不存在：$relativePath"
        }
        Copy-Item -LiteralPath $source -Destination (Join-Path $stagingDirectory $relativePath) -Recurse -Force
    }

    Get-ChildItem -LiteralPath $stagingDirectory -Directory -Filter "__pycache__" -Recurse -Force |
        Remove-Item -Recurse -Force

    if (-not (Get-Command tar.exe -ErrorAction SilentlyContinue)) {
        throw "未找到 tar.exe，无法生成 Linux 部署包"
    }
    tar.exe -czf $archive -C $stagingDirectory .
    if ($LASTEXITCODE -ne 0) {
        throw "生成后端部署包失败"
    }
    (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() |
        Set-Content -LiteralPath $hashFile -Encoding ascii

    Write-Host "部署包：$archive"
    Write-Host "校验文件：$hashFile"
    Write-Host "预期后端源码指纹：$expectedFingerprint"
    Write-Host "包内不包含项目根目录 .env、storage、storage_metadata 或虚拟环境。"

    if (-not $Deploy) {
        Write-Host "仅生成部署包；如需执行远程更新，请重新加 -Deploy -Server <host> -User <user>。"
        return
    }

    $remote = "$User@$Server"
    $remoteArchive = "$RemotePath/$(Split-Path -Leaf $archive)"
    if ($PSCmdlet.ShouldProcess($remote, "上传后端并重建 Docker 服务")) {
        $scpOptions = @("-P", "$SshPort")
        $sshOptions = @("-p", "$SshPort")
        if ($IdentityFile) {
            $scpOptions += @("-i", $IdentityFile)
            $sshOptions += @("-i", $IdentityFile)
        }
        & ssh @sshOptions $remote "mkdir -p '$RemotePath'"
        if ($LASTEXITCODE -ne 0) {
            throw "无法创建远程部署目录"
        }
        & scp @scpOptions $archive "$remote`:$RemotePath/"
        if ($LASTEXITCODE -ne 0) {
            throw "scp 上传失败"
        }

        $remoteCommand = if ($HotUpdate) { @"
set -e
mkdir -p '$RemotePath'
tar -xzf '$remoteArchive' -C '$RemotePath'
docker inspect '${ContainerName}' >/dev/null
docker cp '$RemotePath/app/.' '${ContainerName}:/app/app/'
docker cp '$RemotePath/run.py' '${ContainerName}:/app/run.py'
docker restart '${ContainerName}' >/dev/null
"@ } else { @"
set -e
mkdir -p '$RemotePath'
tar -xzf '$remoteArchive' -C '$RemotePath'
cd '$RemotePath'
docker compose -f docker-compose.production.yml up -d --build scan-convert-api
"@
        }
        $remoteCommand += "`n"
        $remoteCommand += @"
set +e
for attempt in `$(seq 1 30); do
    health=`$(curl -fsS http://127.0.0.1:5000/health 2>/dev/null)
    if [ `$? -eq 0 ]; then
        if printf '%s' "`$health" | grep -Eq '"source_fingerprint"[[:space:]]*:[[:space:]]*"'$expectedFingerprint'"'; then
            printf '%s\n' "`$health"
            rm -f '$remoteArchive'
            exit 0
        fi
    fi
    sleep 1
done
echo '后端健康检查超时，或源码指纹与本次部署不匹配' >&2
exit 1
"@
        Write-Verbose "Remote deploy command:`n$remoteCommand"
        & ssh @sshOptions $remote $remoteCommand
        if ($LASTEXITCODE -ne 0) {
            throw "远程部署失败"
        }
        Write-Host "远程部署完成，健康检查通过。"
    }
}
finally {
    if (Test-Path -LiteralPath $stagingDirectory) {
        Remove-Item -LiteralPath $stagingDirectory -Recurse -Force
    }
}
