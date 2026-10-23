# 扩展示例

`reference_page`是最小v1页面插件。插件管理页选择“从目录安装”，选中reference_page目录，安装后启用并重启。聊天输入`/`选择“资料页示例”，指令先进入输入框，发送后打开辅助页；没有主模型请求或文件写入。

插件提供`UiPageDefinition → create_page(context) → UiContribution`；宿主负责标题、圆角、分栏、关闭和可用性。业务代码使用`plugin_api.core`，界面使用`plugin_api.ui`，耗时工作不能在创建函数里进行。

v1继续兼容；使用本批文档转换、资料范围或DisplayRow公共入口时声明v2。版本目录与插件数据分开，不将密钥、模型配置、私人对话或语料放进插件包。具体契约见desktop/docs/EXTENSION_API.md。
