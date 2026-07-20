using System.Net;
using System.Net.Sockets;
using NAudio.CoreAudioApi;
using NAudio.Wave;

namespace BedCast.Server;

/// <summary>
/// BedCast v0 server — WASAPI loopback capture → raw PCM over TCP.
///
/// v0 wire format: 16-byte header on connect, then endless S16LE interleaved PCM.
///   [0..7]  magic  "BEDCAST0" (ASCII)
///   [8..11] sample rate  (uint32 LE)
///   [12]    channels     (byte)
///   [13]    bits/sample  (byte, always 16 in v0)
///   [14..15] reserved    (zero)
///
/// Receiver (Termux) v0:
///   mpv --demuxer=rawaudio --demuxer-rawaudio-rate=48000 \
///       --demuxer-rawaudio-channels=2 --demuxer-rawaudio-format=s16le tcp://PC_IP:48100
///   (strip the 16-byte header first, or use the provided receiver script)
/// </summary>
internal static class Program
{
    private const int Port = 48100;
    private static readonly byte[] Magic = "BEDCAST0"u8.ToArray();

    private static int Main(string[] args)
    {
        if (args.Contains("--help") || args.Contains("-h"))
        {
            Console.WriteLine("bedcast-server [--port N] [--device NAME_SUBSTRING] [--list-devices] [--smoke-test SECONDS OUT.raw]");
            return 0;
        }

        using var enumerator = new MMDeviceEnumerator();

        if (args.Contains("--list-devices"))
        {
            foreach (var d in enumerator.EnumerateAudioEndPoints(DataFlow.Render, DeviceState.Active))
                Console.WriteLine($"  {d.FriendlyName}");
            return 0;
        }

        int port = ArgValue(args, "--port") is { } p ? int.Parse(p) : Port;

        // Capture the chosen render device — default, or first active one matching --device substring.
        using var device = ArgValue(args, "--device") is { } wanted
            ? enumerator.EnumerateAudioEndPoints(DataFlow.Render, DeviceState.Active)
                  .FirstOrDefault(d => d.FriendlyName.Contains(wanted, StringComparison.OrdinalIgnoreCase))
              ?? throw new ArgumentException($"no active render device matches '{wanted}' (try --list-devices)")
            : enumerator.GetDefaultAudioEndpoint(DataFlow.Render, Role.Multimedia);
        using var capture = new WasapiLoopbackCapture(device);

        var src = capture.WaveFormat; // typically IEEE float 32, mix rate/channels
        Console.WriteLine($"[bedcast] device: {device.FriendlyName}");
        Console.WriteLine($"[bedcast] capture format: {src.SampleRate} Hz, {src.Channels} ch, {src.BitsPerSample}-bit {src.Encoding}");
        Console.WriteLine($"[bedcast] wire format:    {src.SampleRate} Hz, {src.Channels} ch, 16-bit S16LE  (~{src.SampleRate * src.Channels * 2 / 1000} kB/s)");

        // --smoke-test: capture N seconds to a file instead of serving TCP.
        if (ArgValue(args, "--smoke-test") is { } secStr)
        {
            var outPath = args.Last();
            return SmokeTest(capture, int.Parse(secStr), outPath);
        }

        var listener = new TcpListener(IPAddress.Any, port);
        listener.Start();
        Console.WriteLine($"[bedcast] listening on 0.0.0.0:{port} — connect the receiver, then play audio.");

        while (true)
        {
            using var client = listener.AcceptTcpClient();
            client.NoDelay = true;
            var remote = client.Client.RemoteEndPoint;
            Console.WriteLine($"[bedcast] client connected: {remote}");
            try
            {
                ServeClient(client.GetStream(), capture, src);
            }
            catch (IOException)
            {
                Console.WriteLine($"[bedcast] client disconnected: {remote}");
            }
        }
    }

    private static void ServeClient(NetworkStream net, WasapiLoopbackCapture capture, WaveFormat src)
    {
        // Header
        var header = new byte[16];
        Magic.CopyTo(header, 0);
        BitConverter.GetBytes((uint)src.SampleRate).CopyTo(header, 8);
        header[12] = (byte)src.Channels;
        header[13] = 16;
        net.Write(header);

        var failed = false;
        void OnData(object? _, WaveInEventArgs e)
        {
            if (failed) return;
            try
            {
                var pcm = ToS16Le(e.Buffer, e.BytesRecorded, src);
                net.Write(pcm, 0, pcm.Length);
            }
            catch (IOException) { failed = true; }
            catch (ObjectDisposedException) { failed = true; }
        }

        capture.DataAvailable += OnData;
        capture.StartRecording();
        try
        {
            // Block until the client goes away (poll: a dead socket reports readable+0 bytes).
            var sock = ((NetworkStream)net).Socket;
            while (!failed)
            {
                if (sock.Poll(500_000, SelectMode.SelectRead) && sock.Available == 0) break;
            }
        }
        finally
        {
            capture.StopRecording();
            capture.DataAvailable -= OnData;
        }
        throw new IOException("client gone");
    }

    /// <summary>Convert the WASAPI mix buffer (usually float32) to S16LE. Halves bandwidth, matches every receiver.</summary>
    private static byte[] ToS16Le(byte[] buffer, int bytes, WaveFormat src)
    {
        if (src.Encoding == WaveFormatEncoding.IeeeFloat && src.BitsPerSample == 32)
        {
            int samples = bytes / 4;
            var outBuf = new byte[samples * 2];
            for (int i = 0; i < samples; i++)
            {
                float f = BitConverter.ToSingle(buffer, i * 4);
                int s = (int)(Math.Clamp(f, -1f, 1f) * short.MaxValue);
                outBuf[i * 2] = (byte)s;
                outBuf[i * 2 + 1] = (byte)(s >> 8);
            }
            return outBuf;
        }
        if (src.Encoding == WaveFormatEncoding.Pcm && src.BitsPerSample == 16)
        {
            var outBuf = new byte[bytes];
            Array.Copy(buffer, outBuf, bytes);
            return outBuf;
        }
        throw new NotSupportedException($"Unhandled capture format: {src.Encoding} {src.BitsPerSample}-bit");
    }

    /// <summary>Capture N seconds to a raw file and report signal stats. Proves capture works without any network.</summary>
    private static int SmokeTest(WasapiLoopbackCapture capture, int seconds, string outPath)
    {
        var src = capture.WaveFormat;
        using var fs = File.Create(outPath);
        long total = 0;
        long nonZero = 0;
        capture.DataAvailable += (_, e) =>
        {
            var pcm = ToS16Le(e.Buffer, e.BytesRecorded, src);
            fs.Write(pcm, 0, pcm.Length);
            total += pcm.Length;
            for (int i = 0; i < pcm.Length; i += 2)
                if (pcm[i] != 0 || pcm[i + 1] != 0) nonZero++;
        };
        capture.StartRecording();
        Thread.Sleep(seconds * 1000);
        capture.StopRecording();
        Thread.Sleep(200); // drain

        long expected = (long)src.SampleRate * src.Channels * 2 * seconds;
        double pctNonZero = total > 0 ? 100.0 * nonZero / (total / 2.0) : 0;
        Console.WriteLine($"[smoke] wrote {total} bytes (expected ~{expected}), non-zero samples: {pctNonZero:F1}%");
        Console.WriteLine($"[smoke] verdict: {(total > expected * 0.9 && pctNonZero > 1 ? "PASS" : "FAIL")} " +
                          $"(byte-rate {(total > expected * 0.9 ? "ok" : "LOW")}, signal {(pctNonZero > 1 ? "present" : "ABSENT — is audio playing?")})");
        return total > expected * 0.9 && pctNonZero > 1 ? 0 : 1;
    }

    private static string? ArgValue(string[] args, string name)
    {
        int i = Array.IndexOf(args, name);
        return i >= 0 && i + 1 < args.Length ? args[i + 1] : null;
    }
}
