param(
    [Parameter(Mandatory = $true)][string]$DnlibPath,
    [string]$OutputDirectory = (Join-Path $PSScriptRoot 'bin')
)
$ErrorActionPreference = 'Stop'
$compilerPath = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
if (!(Test-Path -LiteralPath $compilerPath)) { throw '需要 Windows .NET Framework C# 编译器' }
$libraryPath = (Resolve-Path -LiteralPath $DnlibPath).Path
$outputPath = [IO.Path]::GetFullPath($OutputDirectory)
if (Test-Path -LiteralPath $outputPath) { throw '输出目录已存在，请指定新的构建目录，保留旧构建' }
New-Item -ItemType Directory -Path $outputPath | Out-Null
$executablePath = Join-Path $outputPath 'UnityMono.exe'
& $compilerPath /nologo /optimize+ /target:exe "/out:$executablePath" "/reference:$libraryPath" /reference:System.Web.Extensions.dll (Join-Path $PSScriptRoot 'Program.cs')
if ($LASTEXITCODE -ne 0) { throw '编译失败，产物保留在输出目录' }
Copy-Item -LiteralPath $libraryPath -Destination (Join-Path $outputPath 'dnlib.dll')
Write-Output $executablePath
