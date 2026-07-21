using System.Runtime.InteropServices;

namespace BedCast.Server;

/// <summary>
/// Default-audio-device switching via the undocumented-but-industry-standard
/// IPolicyConfig COM interface (the same mechanism AudioSwitcher, SoundSwitch
/// and NirSoft tools use — Windows offers no documented API for this).
/// Only SetDefaultEndpoint is called; every earlier vtable slot is declared
/// (in order) because COM dispatches by slot position.
/// </summary>
[ComImport, Guid("F8679F50-850A-41CF-9C72-430F290290C8"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IPolicyConfig
{
    int GetMixFormat(string pszDeviceName, nint ppFormat);
    int GetDeviceFormat(string pszDeviceName, bool bDefault, nint ppFormat);
    int ResetDeviceFormat(string pszDeviceName);
    int SetDeviceFormat(string pszDeviceName, nint pEndpointFormat, nint pMixFormat);
    int GetProcessingPeriod(string pszDeviceName, bool bDefault, nint pmftDefaultPeriod, nint pmftMinimumPeriod);
    int SetProcessingPeriod(string pszDeviceName, nint pmftPeriod);
    int GetShareMode(string pszDeviceName, nint pMode);
    int SetShareMode(string pszDeviceName, nint mode);
    int GetPropertyValue(string pszDeviceName, bool bFxStore, nint key, nint pv);
    int SetPropertyValue(string pszDeviceName, bool bFxStore, nint key, nint pv);
    int SetDefaultEndpoint(string pszDeviceName, uint role);
    int SetEndpointVisibility(string pszDeviceName, bool bVisible);
}

[ComImport, Guid("870AF99C-171D-4F9E-AF0D-E63DF40C2BC9")]
internal class PolicyConfigClient
{
}

internal static class DefaultDevice
{
    // ERole: 0 = eConsole, 1 = eMultimedia, 2 = eCommunications
    public static void Set(string deviceId)
    {
        var pc = (IPolicyConfig)new PolicyConfigClient();
        Marshal.ThrowExceptionForHR(pc.SetDefaultEndpoint(deviceId, 0));
        Marshal.ThrowExceptionForHR(pc.SetDefaultEndpoint(deviceId, 1));
        // eCommunications deliberately untouched — calls keep their device.
    }
}
