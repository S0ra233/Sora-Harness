# Mono 字符串辅助程序

此可选工具使用 [dnlib 4.5.0](https://www.nuget.org/packages/dnlib/4.5.0) 静态读取与修改 Mono 程序集的 `ldstr` 指令，不执行游戏程序集。dnlib 使用 [MIT 许可证](https://github.com/0xd4d/dnlib/blob/master/LICENSE.txt)，依赖源码和二进制不随本仓库分发。

当前构建方式面向 Windows，需要系统的 .NET Framework 编译器，不需要 .NET SDK。其他平台尚未提供启动与构建方式。

1. 下载上述 NuGet 包（`.nupkg` 是 ZIP 格式），解压到自己选择的依赖目录。使用其中的 `lib/net45/dnlib.dll`。
2. 在项目根目录运行：

```powershell
./tools/unity_mono/build.ps1 -DnlibPath 'D:/依赖目录/dnlib/lib/net45/dnlib.dll'
```

默认生成 `tools/unity_mono/bin/UnityMono.exe` 和 `dnlib.dll`，已被 Git 忽略。Python 会自动找到该位置。构建不会覆盖已有目录；更新时使用新的输出目录：

```powershell
./tools/unity_mono/build.ps1 -DnlibPath 'D:/依赖目录/dnlib/lib/net45/dnlib.dll' -OutputDirectory 'D:/新工具目录'
$env:GAME_TRANSLATION_MONO_HELPER = 'D:/新工具目录/UnityMono.exe'
```

然后使用原有 `run.py candidates / prepare / package / apply / build` 流程。候选按指令位置分别选择，同一段原文出现在不同指令时不会自动一起替换。方法内部可能包含条件和分支，字符串顺序不代表剧情顺序。提取只扫描 `Managed` 中非框架 DLL；跳过的框架文件会列在来源报告中。程序集资源、动态拼接、运行时解密不属于当前 `ldstr` 路线。

签名或混合模式程序集只报告候选，不能写回。普通程序集写回前检查 MVID、类型、方法、指令位置和原文；保存前重新解析全部字符串指令，检查未选项不变。此检查不代表实机验证。

Python 使用标准输入／输出 JSON 调用工具，不需要手工处理协议。`inspect` 返回模块身份与字符串指令；`write` 返回修改后的程序集字节，由 Python 核验后保存到游戏副本。错误经标准错误输出返回 Core。

## English

Optional Windows helper for static Mono `ldstr` inspection and editing, using dnlib 4.5.0 (MIT). Game assemblies are never executed. Build with `build.ps1 -DnlibPath <extracted-package>/lib/net45/dnlib.dll`. The script uses the Windows .NET Framework compiler; no .NET SDK is needed. Output defaults to the ignored `bin/` directory. Set `GAME_TRANSLATION_MONO_HELPER` to use a different executable location. Signed and mixed-mode assemblies are read-only. Runtime behavior and non-literal text sources are not verified by this helper.
