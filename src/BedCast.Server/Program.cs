using System.Net;
using System.Net.Sockets;
using NAudio.CoreAudioApi;
using NAudio.Wave;

namespace BedCast.Server;

/// <summary>
/// BedCast server — WASAPI loopback capture → PCM over TCP.
///
/// Speaks TWO wire protocols, auto-negotiated at connect:
///
/// v1 (BEDCAST1, timestamped — the receiver announces itself):
///   receiver → "BC1H" + t0_us(u64)            ping, up to 8 rounds
///   server   → "BC1R" + t0_us + t_server_us   echo per ping
///   receiver → "BC1G" + 0(u64)                go
///   server   → 16-byte header (magic BEDCAST1) then framed packets:
///              seq(u32) | capture_ts_us(u64, unix epoch) | len(u32) | S16LE payload
///
/// v0 (BEDCAST0, legacy dumb pipe — receiver sends nothing within 700 ms):
///   server   → 16-byte header (magic BEDCAST0) then endless raw S16LE.
///
/// Header layout (both): magic(8) | rate u32 LE | channels u8 | bits u8 | reserved u16.
/// </summary>
internal static class Program
{
    private const int DefaultPort = 48100;

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

        int port = ArgValue(args, "--port") is { } p ? int.Parse(p) : DefaultPort;

        using var device = ArgValue(args, "--device") is { } wanted
            ? enumerator.EnumerateAudioEndPoints(DataFlow.Render, DeviceState.Active)
                  .FirstOrDefault(d => d.FriendlyName.Contains(wanted, StringComparison.OrdinalIgnoreCase))
              ?? throw new ArgumentException($"no active render device matches '{wanted}' (try --list-devices)")
            : enumerator.GetDefaultAudioEndpoint(DataFlow.Render, Role.Multimedia);
        using var capture = new WasapiLoopbackCapture(device);

        var src = capture.WaveFormat;
        Console.WriteLine($"[bedcast] device: {device.FriendlyName}");
        Console.WriteLine($"[bedcast] capture format: {src.SampleRate} Hz, {src.Channels} ch, {src.BitsPerSample}-bit {src.Encoding}");
        Console.WriteLine($"[bedcast] wire format:    {src.SampleRate} Hz, {src.Channels} ch, 16-bit S16LE  (~{src.SampleRate * src.Channels * 2 / 1000} kB/s)");

        if (ArgValue(args, "--smoke-test") is { } secStr)
            return SmokeTest(capture, int.Parse(secStr), args.Last());

        // Capture runs always-on; every client subscribes via the broadcast list.
        // Multiple simultaneous receivers are first-class (phone + tablet + tests).
        var clients = new List<ClientConn>();
        double usPerByte = 1_000_000.0 / (src.SampleRate * src.Channels * 2);

        capture.DataAvailable += (_, e) =>
        {
            if (e.BytesRecorded == 0) return;
            var pcm = ToS16Le(e.Buffer, e.BytesRecorded, src);
            long ts = NowUs() - (long)(pcm.Length * usPerByte);
            lock (clients)
            {
                foreach (var c in clients) c.Send(pcm, ts);
                clients.RemoveAll(c => c.Failed);
            }
        };
        capture.StartRecording();

        var listener = new TcpListener(IPAddress.Any, port);
        listener.Start();
        Console.WriteLine($"[bedcast] listening on 0.0.0.0:{port} (protocols: BEDCAST1 + legacy BEDCAST0, multi-client)");

        while (true)
        {
            var client = listener.AcceptTcpClient();
            client.NoDelay = true;
            new Thread(() => HandleClient(client, clients, src)) { IsBackground = true }.Start();
        }
    }

    private static void HandleClient(TcpClient client, List<ClientConn> clients, WaveFormat src)
    {
        var remote = client.Client.RemoteEndPoint;
        Console.WriteLine($"[bedcast] client connected: {remote}");
        try
        {
            using (client)
            {
                var net = client.GetStream();
                bool v1 = NegotiateV1(net);
                Console.WriteLine($"[bedcast] protocol({remote}): {(v1 ? "BEDCAST1 (timestamped)" : "BEDCAST0 (legacy)")}");

                var header = new byte[16];
                (v1 ? "BEDCAST1"u8 : "BEDCAST0"u8).ToArray().CopyTo(header, 0);
                BitConverter.GetBytes((uint)src.SampleRate).CopyTo(header, 8);
                header[12] = (byte)src.Channels;
                header[13] = 16;
                net.Write(header);

                var conn = new ClientConn(net, v1);
                lock (clients) clients.Add(conn);

                var sock = client.Client;
                while (!conn.Failed)
                {
                    if (sock.Poll(500_000, SelectMode.SelectRead) && sock.Available == 0) break;
                }
                lock (clients) clients.Remove(conn);
            }
        }
        catch (IOException) { }
        catch (SocketException) { }
        Console.WriteLine($"[bedcast] client disconnected: {remote}");
    }

    /// <summary>One connected receiver: owns its protocol flavor, sequence counter, and failure state.</summary>
    private sealed class ClientConn(NetworkStream net, bool v1)
    {
        private uint _seq;
        public volatile bool Failed;

        public void Send(byte[] pcm, long ts)
        {
            if (Failed) return;
            try
            {
                if (v1)
                {
                    var frame = new byte[16 + pcm.Length];
                    BitConverter.GetBytes(_seq++).CopyTo(frame, 0);
                    BitConverter.GetBytes(ts).CopyTo(frame, 4);
                    BitConverter.GetBytes((uint)pcm.Length).CopyTo(frame, 12);
                    pcm.CopyTo(frame, 16);
                    net.Write(frame, 0, frame.Length);
                }
                else
                {
                    net.Write(pcm, 0, pcm.Length);
                }
            }
            catch (IOException) { Failed = true; }
            catch (ObjectDisposedException) { Failed = true; }
        }
    }

    /// <summary>Handshake: answer BC1H pings with BC1R echoes until BC1G. No traffic in 700 ms → v0.</summary>
    private static bool NegotiateV1(NetworkStream net)
    {
        net.ReadTimeout = 700;
        var msg = new byte[12];
        try
        {
            for (int round = 0; round < 32; round++)
            {
                if (!ReadExactly(net, msg, 12)) return false;
                var tag = System.Text.Encoding.ASCII.GetString(msg, 0, 4);
                if (tag == "BC1G") { net.ReadTimeout = Timeout.Infinite; return true; }
                if (tag != "BC1H") return false; // unknown chatter → treat as v0
                var reply = new byte[20];
                "BC1R"u8.ToArray().CopyTo(reply, 0);
                Array.Copy(msg, 4, reply, 4, 8);                     // echo t0
                BitConverter.GetBytes(NowUs()).CopyTo(reply, 12);    // server clock
                net.Write(reply);
            }
            return false;
        }
        catch (IOException) { return false; }   // read timeout → v0 client
        finally { net.ReadTimeout = Timeout.Infinite; }
    }

    /// <summary>Microseconds since unix epoch. .NET Core's UtcNow is backed by
    /// GetSystemTimePreciseAsFileTime — sub-microsecond resolution.</summary>
    private static long NowUs() => (DateTimeOffset.UtcNow.UtcTicks - DateTimeOffset.UnixEpoch.UtcTicks) / 10;

    private static bool ReadExactly(NetworkStream net, byte[] buf, int n)
    {
        int got = 0;
        while (got < n)
        {
            int r = net.Read(buf, got, n - got);
            if (r <= 0) return false;
            got += r;
        }
        return true;
    }

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

    private static int SmokeTest(WasapiLoopbackCapture capture, int seconds, string outPath)
    {
        var src = capture.WaveFormat;
        using var fs = File.Create(outPath);
        long total = 0, nonZero = 0;
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
        Thread.Sleep(200);

        long expected = (long)src.SampleRate * src.Channels * 2 * seconds;
        double pctNonZero = total > 0 ? 100.0 * nonZero / (total / 2.0) : 0;
        bool pass = total > expected * 0.9 && pctNonZero > 1;
        Console.WriteLine($"[smoke] wrote {total} bytes (expected ~{expected}), non-zero samples: {pctNonZero:F1}%");
        Console.WriteLine($"[smoke] verdict: {(pass ? "PASS" : "FAIL")}");
        return pass ? 0 : 1;
    }

    private static string? ArgValue(string[] args, string name)
    {
        int i = Array.IndexOf(args, name);
        return i >= 0 && i + 1 < args.Length ? args[i + 1] : null;
    }
}
