# Unity 字体定位与替换 / Font assets

先运行只读扫描：

```text
python run.py fonts GAME fonts.json --characters "译文中需要的字符"
python run.py locate GAME "游戏实际显示的一句文字"
```

`fonts.json` 包含字体、文本组件、相关材质／图集、对象 SHA-256 和资源引用。
`missing_from_character_table` 仅比较当前 TMP 字符表，不计算动态字库、回退字体
或实际排版。`locate` 搜索全部已解析候选，不限于每组的前三条预览；返回的
`group_id` 可用于候选选择，但查询不会自动选择或创建 Task。

建立独立的 `font-plan.json`，不改扫描报告。`target` 直接使用报告中的完整字体记录：

```json
{
  "schema_version": 1,
  "game_dir": "D:/Games/Example",
  "replacements": [
    {
      "target": {"file": "Example_Data/sharedassets0.assets", "asset": {"file": "sharedassets0.assets", "path_id": 123, "name": "ExampleFont", "kind": "Font"}, "object_hash": "报告中的对象摘要"},
      "font_file": "D:/Fonts/Example.ttf",
      "font_hash": "字体文件的 SHA-256"
    }
  ]
}
```

Windows 可用 `Get-FileHash -Algorithm SHA256 FONT` 取得摘要，填入小写形式。
直接 TTF/OTF 替换只适用于包含字体数据的 Unity Font 对象，不支持 TTC/WOFF。
原字体对象的名字、材质和引用保留。此方式不能直接用于 TMP_FontAsset。

```text
python run.py build WORK OUTPUT --font-plan font-plan.json
```

TMP／NGUI 使用制作好的 Unity 字体资源：给清单增加 `donor_game_dir`，每个
replacement 使用 `target` 和 `source` 两条扫描记录，替代 `font_file/font_hash`。
新旧对象需相同 Unity 版本、脚本类型、顶层字段结构；平台和字体插件版本也应匹配。
字体、对应图集和材质都要列出，非空来源引用须能映射到目标资源；目标文件还需
已有相应外部文件引用。不自动增加资产或文件引用表。脚本／GameObject 引用保留；
材质仅复用字节一致的目标 Shader，不自动更换共享 Shader。

准备素材时，按原字体的生成设置制作兼容字体；不要仅凭名字或 `SDF` 后缀选对象。
NGUI 的 UIFont 字体对象可走同一显式资源替换路线，但保留 `.fnt` 的 TextAsset
方案尚未接入本清单，也没有 NGUI 实机验证。错误会停止构建并保留副本。

已经在本地资源副本验证 TTF 替换，以及 TMP 字体、材质、图集三对象映射替换。
图集流数据会嵌入目标 Texture2D，不覆盖原 `.resS`。最终仍须进入相关界面检查缺字、
行高、换行、材质与截断；不能用字符表检查代替实际显示。

参考思路：[Unity 汉化总笔记及①—⑦](https://www.cnblogs.com/guobaoxu/p/12055930.html)。
代码基于现有 UnityPy 读写，不硬编码笔记中旧 Unity 版本的二进制偏移。

English: `fonts` inventories font references; `locate` searches all parsed text
candidates. `build --font-plan` accepts explicit target object records and either
a hashed TTF/OTF file (embedded Unity Font only) or matching donor asset records
from `donor_game_dir`. Donor TMP/UIFont assets require explicit mappings for
their textures, materials and other non-null references. Matching Unity version,
layout and shader are required; use matching platform/plugin versions too. No
new asset IDs, external-reference entries, runtime plugins or font downloads are
created. Changes are applied to the build copy and read back before publication.
Static character coverage and successful file writes do not establish runtime
rendering. Material presets, dynamically loaded fonts and unsupported layouts
may require further investigation.
