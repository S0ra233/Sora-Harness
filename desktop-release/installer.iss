; 安装程序只管理程序目录，不部署、覆盖或卸载用户的配置与对话。
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef BundleDir
  #error 缺少BundleDir，请使用build.ps1生成安装包。
#endif
#ifndef InstallerDir
  #error 缺少InstallerDir，请使用build.ps1生成安装包。
#endif

[Setup]
AppId={{C2486D71-87CE-4E76-B2D6-C3C9BD041FB8}
AppName=模型工作平台
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\DesktopHarness
DefaultGroupName=模型工作平台
PrivilegesRequired=lowest
MinVersion=10.0.17763
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#InstallerDir}
OutputBaseFilename=DesktopHarness-Setup-{#AppVersion}
UninstallDisplayIcon={app}\DesktopHarness.exe
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
CloseApplications=no
RestartApplications=no
DisableProgramGroupPage=yes

[Tasks]
Name: desktopicon; Description: "创建桌面快捷方式"; Flags: unchecked

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; 仅首次部署到扩展目录；已有插件代码不覆盖，配置/语料由插件单独保存。
Source: "{#BundleDir}\extension-packages\rag\*"; DestDir: "{localappdata}\DesktopHarness\extensions\rag"; Flags: recursesubdirs createallsubdirs uninsneveruninstall; Check: InstallRag

[Icons]
Name: "{group}\模型工作平台"; Filename: "{app}\DesktopHarness.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\模型工作平台"; Filename: "{app}\DesktopHarness.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Code]
var
  RagTargetAbsent: Boolean;

function InitializeSetup(): Boolean;
begin
  RagTargetAbsent := not DirExists(ExpandConstant('{localappdata}\DesktopHarness\extensions\rag'));
  Result := True;
end;

function InstallRag(): Boolean;
begin
  Result := RagTargetAbsent;
end;
