# audio.ps1 - enumerate / switch default playback endpoint (zero dependency)
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File audio.ps1 list
#   powershell -NoProfile -ExecutionPolicy Bypass -File audio.ps1 set "<device id>"
# Uses MMDeviceEnumerator + IPolicyConfig COM (works on Win10/11, no modules).

param([Parameter(Position=0)][string]$Cmd = "list",
      [Parameter(Position=1)][string]$TargetId = "")

$src = @"
using System;
using System.Runtime.InteropServices;

namespace AudioSwitch {
  [ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
  public class MMDeviceEnumeratorComObject {}

  [Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IMMDeviceEnumerator {
    int EnumAudioEndpoints(int dataFlow, int stateMask, out IMMDeviceCollection devices);
    int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice endpoint);
    int GetDevice(string id, out IMMDevice device);
    int RegisterEndpointNotificationCallback(IntPtr client);
    int UnregisterEndpointNotificationCallback(IntPtr client);
  }

  [Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IMMDevice {
    int Activate(ref Guid iid, int clsCtx, IntPtr activationParams,
        [MarshalAs(UnmanagedType.IUnknown)] out object iface);
    int OpenPropertyStore(int access, out IntPtr properties);
    int GetId([MarshalAs(UnmanagedType.LPWStr)] out string id);
    int GetState(out int state);
  }

  [Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IPropertyStore {
    int GetCount(out int count);
    int GetAt(int i, out PROPERTYKEY key);
    int GetValue(ref PROPERTYKEY key, out PROPVARIANT value);
    int SetValue(ref PROPERTYKEY key, ref PROPVARIANT value);
    int Commit();
  }

  [StructLayout(LayoutKind.Sequential)]
  public struct PROPERTYKEY { public Guid fmtid; public int pid; }

  [StructLayout(LayoutKind.Sequential)]
  public struct PROPVARIANT {
    public ushort vt;
    public ushort r1, r2, r3;
    public IntPtr p;
    public IntPtr p2;
  }

  [Guid("f8679f50-850a-41cf-9c72-430f290290c8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IPolicyConfig {
    int GetMixFormat(string id, IntPtr fmt);
    int GetDeviceFormat(string id, int bDefault, IntPtr fmt);
    int ResetDeviceFormat(string id);
    int SetDeviceFormat(string id, IntPtr ep, IntPtr mix);
    int GetProcessingPeriod(string id, int bDefault, IntPtr def, IntPtr min);
    int SetProcessingPeriod(string id, IntPtr period);
    int GetShareMode(string id, IntPtr mode);
    int SetShareMode(string id, IntPtr mode);
    int GetPropertyValue(string id, bool bFx, ref PROPERTYKEY key, out PROPVARIANT val);
    int SetPropertyValue(string id, bool bFx, ref PROPERTYKEY key, ref PROPVARIANT val);
    int SetDefaultEndpoint([MarshalAs(UnmanagedType.LPWStr)] string id, int role);
    int SetEndpointVisibility(string id, int visible);
  }

  [ComImport, Guid("870AF99C-171D-4F9E-AF0D-E63DF40C2BC9")]
  public class PolicyConfigClient {}

  public class Switcher {
    const int eRender = 0;
    const int DEVICE_STATE_ACTIVE = 1;
    static string NameOf(IMMDevice dev) {
      var key = new PROPERTYKEY { // PKEY_Device_FriendlyName
        fmtid = new Guid("a45c254e-df1c-4efd-8020-67d146a850e0"), pid = 14 };
      IntPtr pStore;
      dev.OpenPropertyStore(0, out pStore);
      IPropertyStore store = (IPropertyStore)Marshal.GetObjectForIUnknown(pStore);
      PROPVARIANT pv;
      store.GetValue(ref key, out pv);
      string name = Marshal.PtrToStringUni(pv.p);
      Marshal.ReleaseComObject(store);
      Marshal.Release(pStore);
      return name;
    }

    public static string ListJson() {
      var en = (IMMDeviceEnumerator)(new MMDeviceEnumeratorComObject());
      IMMDevice def;
      string defId = "";
      if (en.GetDefaultAudioEndpoint(eRender, 1 /*eMultimedia*/, out def) == 0) {
        def.GetId(out defId);
        Marshal.ReleaseComObject(def);
      }
      IMMDeviceCollection dc;
      en.EnumAudioEndpoints(eRender, 0xF /*all states*/, out dc);
      int count;
      dc.GetCount(out count);
      var sb = new System.Text.StringBuilder();
      sb.Append("[");
      int emitted = 0;
      for (int i = 0; i < count; i++) {
        try {
        IMMDevice d;
        dc.Item(i, out d);
        string id;
        int st;
        d.GetId(out id);
        d.GetState(out st);
        string name;
        try { name = NameOf(d); } catch { name = ""; }
        if (emitted++ > 0) sb.Append(",");
        sb.Append("{\"id\":\"").Append(Escape(id)).Append("\",\"name\":\"")
          .Append(Escape(name)).Append("\",\"state\":").Append(st)
          .Append(",\"default\":")
          .Append(id == defId ? "true" : "false").Append("}");
        Marshal.ReleaseComObject(d);
        } catch { }
      }
      sb.Append("]");
      Marshal.ReleaseComObject(dc);
      Marshal.ReleaseComObject(en);
      return sb.ToString();
    }

    public static void SetDefault(string id) {
      var pc = (IPolicyConfig)(new PolicyConfigClient());
      // set for all three roles: console, multimedia, communications
      pc.SetDefaultEndpoint(id, 0);
      pc.SetDefaultEndpoint(id, 1);
      pc.SetDefaultEndpoint(id, 2);
      Marshal.ReleaseComObject(pc);
    }

    static string Escape(string s) {
      return s.Replace("\\", "\\\\").Replace("\"", "\\\"");
    }
  }

  [Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IMMDeviceCollection {
    int GetCount(out int count);
    int Item(int i, out IMMDevice dev);
  }
}
"@

Add-Type -TypeDefinition $src -Language CSharp

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
if ($Cmd -eq "set") {
  if (-not $TargetId) { Write-Error "missing device id"; exit 2 }
  [AudioSwitch.Switcher]::SetDefault($TargetId)
  Write-Output "OK"
} else {
  Write-Output ([AudioSwitch.Switcher]::ListJson())
}
