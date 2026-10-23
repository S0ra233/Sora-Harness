param(
    [string]$Python = "",
    [string]$ISCC = "",
    [ValidatePattern('^\d+\.\d+\.\d+(\.\d+)?$')][string]$Version = "0.1.0",
    [switch]$ExeOnly,
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$projectDirectory = Split-Path -Parent $PSScriptRoot
$batchName = (Get-Date -Format "yyyyMMdd-HHmmss") + "-" + [Guid]::NewGuid().ToString("N").Substring(0, 8)
$batchDirectory = Join-Path $PSScriptRoot ("artifacts\" + $batchName)
$null = New-Item -ItemType Directory -Path $batchDirectory
$logFile = Join-Path $batchDirectory "build.log"
$oldPythonUtf8 = $env:PYTHONUTF8
$oldInstallerCache = $env:PYINSTALLER_CONFIG_DIR
$env:PYTHONUTF8 = "1"
$env:PYINSTALLER_CONFIG_DIR = Join-Path $PSScriptRoot "cache"

function Invoke-Native([string]$Executable, [string[]]$Arguments) {
    $previous = $ErrorActionPreference
    try {
        # Windows PowerShell把正常stderr包装为错误记录；按真实退出码判断构建。
        $ErrorActionPreference = "Continue"
        & $Executable @Arguments 2>&1 | ForEach-Object {
            $line = $_.ToString()
            Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8
            Write-Host $line
        }
        $code = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $previous }
    if ($code -ne 0) { throw "构建命令失败，退出码 $code；记录保留在 $logFile" }
}

function Find-Python {
    if ($Python) {
        if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "指定的Python不存在：$Python" }
        return (Resolve-Path -LiteralPath $Python).Path
    }
    $conda = Join-Path $env:USERPROFILE "miniconda3\python.exe"
    if (Test-Path -LiteralPath $conda -PathType Leaf) { return $conda }
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($command -and $command.Source -notmatch '\\WindowsApps\\') { return $command.Source }
    throw "未找到构建用Python，请使用build.ps1 -Python 对应的python.exe路径。"
}

function Find-Inno {
    if ($ISCC) {
        if (-not (Test-Path -LiteralPath $ISCC -PathType Leaf)) { throw "指定的ISCC不存在：$ISCC" }
        return (Resolve-Path -LiteralPath $ISCC).Path
    }
    $pointer = Join-Path $PSScriptRoot "toolchain\compiler-path.txt"
    if (Test-Path -LiteralPath $pointer -PathType Leaf) {
        $candidate = (Get-Content -LiteralPath $pointer -Raw).Trim()
        $toolchainRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "toolchain")) + [IO.Path]::DirectorySeparatorChar
        if ([IO.Path]::GetFullPath($candidate).StartsWith($toolchainRoot, [StringComparison]::OrdinalIgnoreCase) -and
            (Test-Path -LiteralPath $candidate -PathType Leaf)) { return $candidate }
    }
    $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    foreach ($programDirectory in @(${env:ProgramFiles(x86)}, $env:ProgramFiles)) {
        if (-not $programDirectory) { continue }
        foreach ($edition in @("Inno Setup 7", "Inno Setup 6")) {
            $candidate = Join-Path $programDirectory ($edition + "\ISCC.exe")
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
        }
    }
    if ($SkipInstall) { throw "未找到Inno Setup，请指定-ISCC；只生成EXE可用-ExeOnly。" }
    Write-Host "准备官方Inno Setup便携编译器；不登记快捷方式、文件关联或卸载项。"
    $compilerDirectory = Join-Path $PSScriptRoot ("toolchain\inno-6.7.3-" + $batchName)
    $null = New-Item -ItemType Directory -Path $compilerDirectory
    $download = Join-Path $batchDirectory "innosetup-6.7.3.exe"
    Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/jrsoftware/issrc/releases/download/is-6_7_3/innosetup-6.7.3.exe" -OutFile $download
    $signature = Get-AuthenticodeSignature -LiteralPath $download
    if ($signature.Status -ne "Valid" -or $signature.SignerCertificate.Subject -notmatch 'Pyrsys B\.V\.') {
        throw "下载的Inno Setup签名无法确认，未执行；文件保留：$download"
    }
    # 每次准备都使用新目录，避免编译器安装程序清理任何已有文件。
    $installArguments = @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/PORTABLE=1", "/CURRENTUSER", "/NOICONS",
        ('/DIR="{0}"' -f $compilerDirectory), ('/LOG="{0}"' -f (Join-Path $batchDirectory "compiler-setup.log")))
    $process = Start-Process -FilePath $download -ArgumentList $installArguments -WindowStyle Hidden -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "准备Inno编译器失败，退出码 $($process.ExitCode)，目录和日志保留。" }
    $candidate = Join-Path $compilerDirectory "ISCC.exe"
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { throw "Inno编译器没有生成：$candidate" }
    [IO.File]::WriteAllText($pointer, $candidate, (New-Object Text.UTF8Encoding($false)))
    return $candidate
}

try {
    Write-Host "[1/4] 准备独立构建环境"
    $buildPython = Join-Path $PSScriptRoot ".build-env\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $buildPython -PathType Leaf)) {
        Invoke-Native (Find-Python) @("-m", "venv", (Join-Path $PSScriptRoot ".build-env"))
    }
    if (-not $SkipInstall) {
        Invoke-Native $buildPython @("-m", "pip", "install", "--disable-pip-version-check", "--progress-bar", "off",
            "-r", (Join-Path $PSScriptRoot "requirements-build.txt"))
    }
    Write-Host "[2/4] 打包GUI和后台执行程序"
    Invoke-Native $buildPython @("-m", "PyInstaller", "--noconfirm", "--distpath", (Join-Path $batchDirectory "dist"),
        "--workpath", (Join-Path $batchDirectory "work"), (Join-Path $PSScriptRoot "desktop.spec"))
    $bundleDirectory = Join-Path $batchDirectory "dist\DesktopHarness"
    foreach ($name in @("DesktopHarness.exe", "DesktopHarnessWorker.exe")) {
        if (-not (Test-Path -LiteralPath (Join-Path $bundleDirectory $name) -PathType Leaf)) { throw "构建缺少$name。" }
    }
    # 随包提供独立扩展，不把RAG编进Harness工具登记表或复制个人数据。
    $ragSource = Join-Path $projectDirectory "desktop-extensions\rag"
    $extensionPackages = Join-Path $bundleDirectory "extension-packages"
    $null = New-Item -ItemType Directory -Path $extensionPackages
    Copy-Item -LiteralPath $ragSource -Destination (Join-Path $extensionPackages "rag") -Recurse
    Copy-Item -LiteralPath (Join-Path $projectDirectory "desktop-extensions\install-rag.ps1") -Destination $extensionPackages
    Write-Host "[3/4] 生成原项目启动器"
    $compiler = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
    if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) {
        $compiler = Join-Path $env:WINDIR "Microsoft.NET\Framework\v4.0.30319\csc.exe"
    }
    if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) { throw "Windows缺少启动器编译工具；完整EXE目录已生成。" }
    $launcher = Join-Path $batchDirectory "SourceLauncher.exe"
    Invoke-Native $compiler @("/nologo", "/target:winexe", "/codepage:65001", "/reference:System.Windows.Forms.dll",
        ("/out:" + $launcher), (Join-Path $PSScriptRoot "source_launcher.cs"))
    $rootLauncher = Join-Path $projectDirectory "DesktopHarness.exe"
    if (-not (Test-Path -LiteralPath $rootLauncher)) {
        $source = [IO.File]::OpenRead($launcher)
        try {
            $destination = [IO.File]::Open($rootLauncher, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
            try { $source.CopyTo($destination) } finally { $destination.Dispose() }
        }
        finally { $source.Dispose() }
    }
    else { Write-Host "原项目的DesktopHarness.exe已存在，保留原文件。" }
    $relativeExecutable = "artifacts\$batchName\dist\DesktopHarness\DesktopHarness.exe"
    if (-not $ExeOnly) {
        Write-Host "[4/4] 生成安装包"
        $installerCompiler = Find-Inno
        Invoke-Native $installerCompiler @(("/DAppVersion=" + $Version), ("/DBundleDir=" + $bundleDirectory),
            ("/DInstallerDir=" + (Join-Path $batchDirectory "installer")), (Join-Path $PSScriptRoot "installer.iss"))
        $setup = Join-Path $batchDirectory ("installer\DesktopHarness-Setup-" + $Version + ".exe")
        if (-not (Test-Path -LiteralPath $setup -PathType Leaf)) { throw "安装包未生成。" }
        Write-Host "安装包：$setup"
    }
    else { Write-Host "[4/4] 已按-ExeOnly保留EXE目录。" }
    # 全部目标产物完成后才发布启动指针；失败批次不影响现有可用版本。
    [IO.File]::WriteAllText((Join-Path $PSScriptRoot "current-build.txt"), $relativeExecutable, (New-Object Text.UTF8Encoding($false)))
    Write-Host "原配置启动入口：$rootLauncher"
    Write-Host "独立程序目录：$bundleDirectory"
    Write-Host "完成；没有运行GUI、安装本应用或修改desktop/local。"
}
catch {
    Add-Content -LiteralPath $logFile -Value $_.Exception.Message -Encoding UTF8
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host "已有产物与日志保留：$batchDirectory"
    exit 1
}
finally {
    [Environment]::SetEnvironmentVariable("PYTHONUTF8", $oldPythonUtf8, "Process")
    [Environment]::SetEnvironmentVariable("PYINSTALLER_CONFIG_DIR", $oldInstallerCache, "Process")
}
