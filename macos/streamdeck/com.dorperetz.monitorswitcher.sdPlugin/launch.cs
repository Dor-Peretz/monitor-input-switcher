// Windowless host for the Stream Deck plugin on Windows.
// Stream Deck used to start launch.bat, which leaves a console open for the
// whole time the deck is on this PC.
using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

internal static class Program
{
    [STAThread]
    static int Main(string[] args)
    {
        string dir = AppDomain.CurrentDomain.BaseDirectory;
        string script = Path.Combine(dir, "plugin.py");
        if (!File.Exists(script))
        {
            Log(dir, "plugin.py not found");
            return 1;
        }

        string python = FindPython();
        if (python == null)
        {
            Log(dir, "No Python 3 found (pythonw.exe / py -3 / python.exe)");
            return 1;
        }

        var psi = new ProcessStartInfo
        {
            FileName = python,
            Arguments = QuoteArg(script) + PrefixArgs(args),
            WorkingDirectory = dir,
            UseShellExecute = false,
            CreateNoWindow = true,
        };

        IntPtr job = Job.CreateKillOnClose();
        try
        {
            using (Process process = Process.Start(psi))
            {
                if (process == null)
                {
                    Log(dir, "failed to start " + python);
                    return 1;
                }
                Job.Assign(job, process);
                process.WaitForExit();
                return process.ExitCode;
            }
        }
        catch (Exception ex)
        {
            Log(dir, ex.Message);
            return 1;
        }
        finally
        {
            Job.Close(job);
        }
    }

    static string PrefixArgs(string[] args)
    {
        var sb = new StringBuilder();
        for (int i = 0; i < args.Length; i++)
        {
            sb.Append(' ');
            sb.Append(QuoteArg(args[i]));
        }
        return sb.ToString();
    }

    static string QuoteArg(string arg)
    {
        if (arg.Length != 0 && arg.IndexOfAny(new[] { ' ', '\t', '\n', '\v', '"' }) < 0)
            return arg;
        var sb = new StringBuilder();
        sb.Append('"');
        for (int i = 0; ; i++)
        {
            int slashes = 0;
            while (i < arg.Length && arg[i] == '\\')
            {
                slashes++;
                i++;
            }
            if (i == arg.Length)
            {
                sb.Append('\\', slashes * 2);
                break;
            }
            if (arg[i] == '"')
            {
                sb.Append('\\', slashes * 2 + 1);
                sb.Append('"');
            }
            else
            {
                sb.Append('\\', slashes);
                sb.Append(arg[i]);
            }
        }
        sb.Append('"');
        return sb.ToString();
    }

    static string FindPython()
    {
        string fromPy = FromPyLauncher();
        if (fromPy != null)
            return fromPy;

        string onPath = FindOnPath("pythonw.exe") ?? FindOnPath("python.exe");
        if (onPath != null)
            return onPath;

        foreach (string root in new[]
        {
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "Python"),
            Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
            Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86),
        })
        {
            string found = NewestPythonW(root);
            if (found != null)
                return found;
        }
        return null;
    }

    static string FromPyLauncher()
    {
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = "py",
                Arguments = "-3 -c \"import os,sys; print(sys.executable)\"",
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
            };
            using (Process process = Process.Start(psi))
            {
                string output = process.StandardOutput.ReadToEnd().Trim();
                process.WaitForExit(8000);
                if (process.ExitCode != 0 || string.IsNullOrEmpty(output) || !File.Exists(output))
                    return null;
                string pythonw = Path.Combine(Path.GetDirectoryName(output), "pythonw.exe");
                if (File.Exists(pythonw))
                    return pythonw;
                return output;
            }
        }
        catch
        {
            return null;
        }
    }

    static string FindOnPath(string name)
    {
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = "where",
                Arguments = name,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
            };
            using (Process process = Process.Start(psi))
            {
                string line = process.StandardOutput.ReadLine();
                process.WaitForExit(4000);
                if (!string.IsNullOrEmpty(line) && File.Exists(line.Trim()))
                    return line.Trim();
            }
        }
        catch
        {
        }
        return null;
    }

    static string NewestPythonW(string root)
    {
        try
        {
            if (string.IsNullOrEmpty(root) || !Directory.Exists(root))
                return null;
            string direct = Path.Combine(root, "pythonw.exe");
            if (File.Exists(direct))
                return direct;
            string best = null;
            foreach (string pythonDir in Directory.GetDirectories(root, "Python*"))
            {
                string candidate = Path.Combine(pythonDir, "pythonw.exe");
                if (!File.Exists(candidate))
                    continue;
                if (best == null || string.Compare(candidate, best, StringComparison.OrdinalIgnoreCase) > 0)
                    best = candidate;
            }
            return best;
        }
        catch
        {
            return null;
        }
    }

    static void Log(string dir, string message)
    {
        try
        {
            File.AppendAllText(
                Path.Combine(dir, "launch-win.log"),
                DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + message + Environment.NewLine);
        }
        catch
        {
        }
    }
}

internal static class Job
{
    const int JobObjectExtendedLimitInformation = 9;
    const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000;

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern IntPtr CreateJobObject(IntPtr lpJobAttributes, string lpName);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool SetInformationJobObject(
        IntPtr hJob, int infoClass, IntPtr lpJobObjectInfo, uint cbJobObjectInfoLength);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool CloseHandle(IntPtr hObject);

    [StructLayout(LayoutKind.Sequential)]
    struct IO_COUNTERS
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct JOBOBJECT_BASIC_LIMIT_INFORMATION
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public IntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    public static IntPtr CreateKillOnClose()
    {
        IntPtr handle = CreateJobObject(IntPtr.Zero, null);
        if (handle == IntPtr.Zero)
            return IntPtr.Zero;
        var info = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        int length = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
        IntPtr ptr = Marshal.AllocHGlobal(length);
        try
        {
            Marshal.StructureToPtr(info, ptr, false);
            SetInformationJobObject(handle, JobObjectExtendedLimitInformation, ptr, (uint)length);
        }
        finally
        {
            Marshal.FreeHGlobal(ptr);
        }
        return handle;
    }

    public static void Assign(IntPtr job, Process process)
    {
        if (job == IntPtr.Zero || process == null)
            return;
        try
        {
            AssignProcessToJobObject(job, process.Handle);
        }
        catch
        {
        }
    }

    public static void Close(IntPtr job)
    {
        if (job != IntPtr.Zero)
            CloseHandle(job);
    }
}
