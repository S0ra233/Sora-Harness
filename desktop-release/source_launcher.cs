// 原项目只放轻量启动器；实际程序位于独立发布目录，配置仍取desktop/local。
using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

internal static class SourceLauncher
{
    [STAThread]
    private static void Main()
    {
        try
        {
            string root = AppDomain.CurrentDomain.BaseDirectory;
            string release = Path.Combine(root, "desktop-release");
            string relative = File.ReadAllText(Path.Combine(release, "current-build.txt")).Trim();
            if (Path.IsPathRooted(relative))
                throw new IOException("构建索引应为发布目录中的相对路径。");
            string executable = Path.GetFullPath(Path.Combine(release, relative));
            string artifacts = Path.GetFullPath(Path.Combine(release, "artifacts")) + Path.DirectorySeparatorChar;
            if (!executable.StartsWith(artifacts, StringComparison.OrdinalIgnoreCase) || !File.Exists(executable))
                throw new IOException("尚未找到完整构建，请先运行desktop-release/build.cmd。");
            string data = Path.Combine(root, "desktop", "local");
            Process.Start(new ProcessStartInfo {
                FileName = executable,
                Arguments = "--data-dir \"" + data + "\"",
                WorkingDirectory = Path.GetDirectoryName(executable),
                UseShellExecute = false
            });
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "模型工作平台启动失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }
}
