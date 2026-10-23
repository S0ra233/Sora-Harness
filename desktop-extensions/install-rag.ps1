param(
    [string]$DataDirectory,
    [switch]$Installed
)
$ErrorActionPreference = "Stop"
# 只安装插件代码，不启停其他插件，也不创建语料、密钥、预设或对话。
if (-not $DataDirectory) {
    if ($Installed) {
        $DataDirectory = Join-Path $env:LOCALAPPDATA "DesktopHarness"
    }
    else {
        $DataDirectory = Join-Path (Split-Path -Parent $PSScriptRoot) "desktop\local"
    }
}
$sourceDirectory = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "rag"))
$dataRoot = [IO.Path]::GetFullPath($DataDirectory)
$extensionDirectory = Join-Path $dataRoot "extensions"
$targetDirectory = Join-Path $extensionDirectory "rag"
if (-not (Test-Path -LiteralPath (Join-Path $sourceDirectory "plugin.json") -PathType Leaf)) {
    throw "没有找到RAG插件源目录。"
}
if (Test-Path -LiteralPath $targetDirectory) {
    foreach ($file in Get-ChildItem -LiteralPath $sourceDirectory -Recurse -File) {
        $relative = $file.FullName.Substring($sourceDirectory.Length).TrimStart([char[]]@('\', '/'))
        $target = Join-Path $targetDirectory $relative
        if (-not (Test-Path -LiteralPath $target -PathType Leaf) -or
            (Get-FileHash -LiteralPath $target).Hash -ne (Get-FileHash -LiteralPath $file.FullName).Hash) {
            throw "目标已存在另一版RAG代码，未覆盖：$targetDirectory。请先保留并处理旧扩展目录。"
        }
    }
    Write-Host "插件代码已一致，保留现有文件：$targetDirectory"
    return
}
$null = New-Item -ItemType Directory -Path $extensionDirectory -Force
$stagingRoot = Join-Path $dataRoot "extension-staging"
$null = New-Item -ItemType Directory -Path $stagingRoot -Force
$candidate = Join-Path $stagingRoot ("rag-" + [Guid]::NewGuid().ToString("N"))
Copy-Item -LiteralPath $sourceDirectory -Destination $candidate -Recurse
# 已确认候选和目标均属于本次数据目录；完整复制后才发布给插件扫描入口。
$candidateFull = [IO.Path]::GetFullPath($candidate)
$targetFull = [IO.Path]::GetFullPath($targetDirectory)
$prefix = $dataRoot.TrimEnd([char[]]@('\', '/')) + [IO.Path]::DirectorySeparatorChar
if (-not $candidateFull.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) -or
    -not $targetFull.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "插件发布路径不在本次数据目录内。"
}
[IO.Directory]::Move($candidateFull, $targetFull)
Write-Host "已安装独立RAG插件，默认启用。重启应用后在插件页配置：$targetDirectory"
