# Unity Addressables helper

Python calls this optional .NET 8 helper with JSON on stdin. It reads catalog data;
it never executes game code or overwrites the input. Output is JSON, with rewritten
catalog bytes encoded as base64. Build calls it before copying and after all text
and font writes.

With a .NET 8 SDK, from the repository root (choose a fresh output directory):

```powershell
dotnet publish tools/unity_addressables/UnityAddressables.csproj -c Release -o tools/unity_addressables/bin
```

Running requires the .NET 8 runtime and `dotnet` on PATH. No SDK is needed to run
an already built helper. Set `GAME_TRANSLATION_ADDRESSABLES_HELPER` to the absolute
path of `UnityAddressables.dll` when using another build location. No automatic
download or global installation is performed by the Skill.

Pinned packages: AssetsTools.NET.Addressables 3.0.2, AssetsTools.NET 3.0.2,
System.IO.Hashing 9.0.4. The last reference is explicit because the Addressables
NuGet package does not declare the hashing dependency its binary requires.
Sources: [AddressablesTools](https://github.com/nesrak1/AddressablesTools),
[AssetsTools.NET](https://github.com/nesrak1/AssetsTools.NET), and
[System.IO.Hashing](https://www.nuget.org/packages/System.IO.Hashing/9.0.4).
They use MIT licenses; retain their notices when distributing their binaries.
Downloaded packages and compiled binaries are ignored by Git.

Enabled formats: JSON catalogs and binary v2. A writer must roundtrip the complete
parsed resource graph, keys, provider settings and dependencies without unintended
changes. Other binary versions, unsupported object types and mismatches fail.

Python resolves explicit RuntimePath/streamingAssetsPath variables to local files.
Only the standard AssetBundleProvider with local direct-file loading is enabled.
CRC is computed over decompressed UnityFS data blocks and checked against the
original catalog before editing. After writeback, only affected bundle entries
receive updated enabled CRCs and sizes. CRC=0 remains 0; enabled checks are not
disabled. Paths and Bundle Hash values remain unchanged for this local loading
route. A sibling catalog `.hash` receives an MD5-derived cache version token for
our rewritten catalog, not a reconstruction of Unity's original build hash.

Fallen's binary v2 catalogs passed local graph roundtrip checks; a Japanese
StringTable bundle was changed in a resource copy and its two matching catalogs
updated. This is a file-level check, not a claim of verified game startup, remote
updates, custom providers, caching behavior or compatibility with all catalogs.

中文：本模块只支持已明确映射的本地直读 Bundle。先验证原 CRC 和目录往返，
再在副本中完成文本／字体写回，更新对应目录。远程、WebRequest 缓存、自定义
Provider 等路线仍需单独适配。失败时保留构建目录，不能将文件读回成功当成实机成功。
