<#
  为开发态(electron.exe)注册一个带 AppUserModelID 的开始菜单快捷方式。
  Windows 11 任务栏按钮的名称/图标按「进程身份(AUMID) -> 快捷方式」解析,
  未打包的 electron.exe 没有带 AUMID 的快捷方式时,任务栏会显示 Electron 默认图标;
  注册本快捷方式后,`npm run dev` 的任务栏按钮即显示品牌图标与名称。

  用法(在仓库根目录): powershell -ExecutionPolicy Bypass -File scripts/install-dev-shortcut.ps1
  卸载: 删除 %APPDATA%\Microsoft\Windows\Start Menu\Programs\Dashboard (Dev).lnk
#>
$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
$electron = Join-Path $repo 'node_modules\electron\dist\electron.exe'
$icon = Join-Path $repo 'build\icon.ico'
$lnkDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$lnkPath = Join-Path $lnkDir 'Dashboard (Dev).lnk'
$appUserModelId = 'com.company.aibench.dashboard'

if (-not (Test-Path $electron)) { throw "未找到 electron.exe: $electron (先 npm install)" }
if (-not (Test-Path $icon)) { throw "未找到图标: $icon (先生成 build/icon.ico)" }

# 1) 基础快捷方式
$wsh = New-Object -ComObject WScript.Shell
$lnk = $wsh.CreateShortcut($lnkPath)
$lnk.TargetPath = $electron
$lnk.Arguments = '"' + $repo + '"'
$lnk.WorkingDirectory = $repo
$lnk.IconLocation = "$icon,0"
$lnk.Description = 'Dashboard 开发态(electron-vite)'
$lnk.Save()

# 2) 写入 AppUserModelID(IShellLink + IPropertyStore)
$code = @'
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;

public static class ShellLnk {
  [StructLayout(LayoutKind.Sequential, Pack = 4)]
  public struct PropertyKey { public Guid fmtid; public uint pid; }

  [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
  private class ShellLink {}

  [ComImport, Guid("000214F9-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  private interface IShellLinkW {
    void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder pszFile, int cch, IntPtr pfd, uint fFlags);
    void GetIDList(out IntPtr ppidl);
    void SetIDList(IntPtr pidl);
    void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder pszName, int cch);
    void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string pszName);
    void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder pszDir, int cch);
    void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string pszDir);
    void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder pszArgs, int cch);
    void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string pszArgs);
    void GetHotkey(out short pwHotkey);
    void SetHotkey(short wHotkey);
    void GetShowCmd(out int piShowCmd);
    void SetShowCmd(int iShowCmd);
    void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder pszIconPath, int cch, out int piIcon);
    void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string pszIconPath, int iIcon);
    void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string pszPathRel, uint dwReserved);
    void Resolve(IntPtr hwnd, uint fFlags);
    void SetPath([MarshalAs(UnmanagedType.LPWStr)] string pszFile);
  }

  [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  private interface IPropertyStore {
    void GetCount(out uint cProps);
    void GetAt(uint iProp, out PropertyKey pkey);
    void GetValue(ref PropertyKey key, out PropVariant pv);
    void SetValue(ref PropertyKey key, ref PropVariant pv);
    void Commit();
  }

  [StructLayout(LayoutKind.Explicit)]
  public struct PropVariant {
    [FieldOffset(0)] public ushort vt;
    [FieldOffset(8)] public IntPtr pointerValue;
  }

  [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
  private static extern void SHGetPropertyStoreFromParsingName(string pszPath, IntPtr pbc, int flags, ref Guid riid, [MarshalAs(UnmanagedType.Interface)] out IPropertyStore ppv);

  public static void SetAppUserModelId(string lnkPath, string aumid) {
    Guid iid = typeof(IPropertyStore).GUID;
    IPropertyStore store;
    SHGetPropertyStoreFromParsingName(lnkPath, IntPtr.Zero, 2 /* GPS_READWRITE */, ref iid, out store);
    var key = new PropertyKey {
      fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), // PKEY_AppUserModel_ID
      pid = 5
    };
    var pv = new PropVariant();
    pv.vt = 31; // VT_LPWSTR
    pv.pointerValue = Marshal.StringToCoTaskMemUni(aumid);
    try {
      store.SetValue(ref key, ref pv);
      store.Commit();
    } finally {
      Marshal.FreeCoTaskMem(pv.pointerValue);
      Marshal.ReleaseComObject(store);
    }
  }
}
'@
Add-Type -TypeDefinition $code -Language CSharp

[ShellLnk]::SetAppUserModelId($lnkPath, $appUserModelId)

Write-Output "  已写入快捷方式: $lnkPath"
Write-Output "  AppUserModelID: $appUserModelId"
Write-Output "  目标: $electron `"$repo`""
Write-Output "  图标: $icon"
Write-Output "  提示: 重启 npm run dev 后任务栏按钮将显示品牌图标(首次可能需几秒刷新)"
