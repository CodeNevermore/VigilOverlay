using Playnite.SDK;
using Playnite.SDK.Events;
using Playnite.SDK.Models;
using Playnite.SDK.Plugins;
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;

namespace VigilOverlayBridge
{
    public sealed class VigilOverlayBridgePlugin : GenericPlugin
    {
        private static readonly Guid PluginId = Guid.Parse("7c04ef12-67ae-4db7-ae4f-3af7fb227809");
        private readonly ILogger logger;
        private readonly object snapshotLock = new object();
        private FileSystemWatcher refreshWatcher;

        public override Guid Id => PluginId;

        public VigilOverlayBridgePlugin(IPlayniteAPI api) : base(api)
        {
            logger = LogManager.GetLogger();
        }

        public override void OnApplicationStarted(OnApplicationStartedEventArgs args)
        {
            RefreshSnapshot("application started");
            StartRefreshWatcher();
            HandlePendingRefreshRequest();
        }

        public override void OnApplicationStopped(OnApplicationStoppedEventArgs args)
        {
            if (refreshWatcher != null)
            {
                refreshWatcher.EnableRaisingEvents = false;
                refreshWatcher.Dispose();
                refreshWatcher = null;
            }
        }

        public override void OnLibraryUpdated(OnLibraryUpdatedEventArgs args)
        {
            RefreshSnapshot("library updated");
        }

        public override void OnGameStopped(OnGameStoppedEventArgs args)
        {
            RefreshSnapshot("game stopped");
        }

        public override void OnGameInstalled(OnGameInstalledEventArgs args)
        {
            RefreshSnapshot("game installed");
        }

        public override void OnGameUninstalled(OnGameUninstalledEventArgs args)
        {
            RefreshSnapshot("game uninstalled");
        }

        private void RefreshSnapshot(string reason, string refreshRequestId = null)
        {
            try
            {
                lock (snapshotLock)
                {
                    WriteSnapshot(refreshRequestId);
                }
            }
            catch (Exception ex)
            {
                logger.Error($"Vigil Overlay Bridge snapshot refresh failed after {reason}: {ex}");
            }
        }

        private string GetTargetDirectory()
        {
            return Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                "VigilOverlay",
                "data",
                "games");
        }

        private void StartRefreshWatcher()
        {
            var targetDirectory = GetTargetDirectory();
            Directory.CreateDirectory(targetDirectory);
            refreshWatcher = new FileSystemWatcher(
                targetDirectory,
                "playnite_refresh_request.json");
            refreshWatcher.NotifyFilter = NotifyFilters.FileName | NotifyFilters.LastWrite;
            refreshWatcher.Created += OnRefreshRequestChanged;
            refreshWatcher.Changed += OnRefreshRequestChanged;
            refreshWatcher.Renamed += OnRefreshRequestRenamed;
            refreshWatcher.EnableRaisingEvents = true;
        }

        private void OnRefreshRequestChanged(object sender, FileSystemEventArgs args)
        {
            PlayniteApi.MainView.UIDispatcher.BeginInvoke(
                new Action(HandlePendingRefreshRequest));
        }

        private void OnRefreshRequestRenamed(object sender, RenamedEventArgs args)
        {
            PlayniteApi.MainView.UIDispatcher.BeginInvoke(
                new Action(HandlePendingRefreshRequest));
        }

        private void HandlePendingRefreshRequest()
        {
            var requestPath = Path.Combine(
                GetTargetDirectory(),
                "playnite_refresh_request.json");
            var requestId = ReadRefreshRequestId(requestPath);
            if (!string.IsNullOrWhiteSpace(requestId))
            {
                RefreshSnapshot("Vigil refresh request", requestId);
            }
        }

        private string ReadRefreshRequestId(string requestPath)
        {
            try
            {
                using (var stream = new FileStream(
                    requestPath,
                    FileMode.Open,
                    FileAccess.Read,
                    FileShare.ReadWrite))
                {
                    var serializer = new DataContractJsonSerializer(
                        typeof(VigilRefreshRequest));
                    var request = serializer.ReadObject(stream) as VigilRefreshRequest;
                    Guid parsed;
                    return request != null &&
                        Guid.TryParse(request.RequestId, out parsed)
                        ? parsed.ToString("D")
                        : null;
                }
            }
            catch (IOException)
            {
                return null;
            }
            catch (SerializationException)
            {
                return null;
            }
        }

        private void WriteSnapshot(string refreshRequestId)
        {
            var targetDirectory = GetTargetDirectory();
            var targetPath = Path.Combine(targetDirectory, "playnite_bridge.json");
            var temporaryPath = Path.Combine(targetDirectory, ".playnite_bridge.json.tmp");

            Directory.CreateDirectory(targetDirectory);

            var records = new List<VigilGameRecord>();
            foreach (var game in PlayniteApi.Database.Games)
            {
                if (game == null || game.Hidden || string.IsNullOrWhiteSpace(game.Name))
                {
                    continue;
                }

                records.Add(new VigilGameRecord
                {
                    Id = game.Id.ToString("D"),
                    Title = game.Name,
                    IsInstalled = game.IsInstalled,
                    InstallDirectory = GetOptionalAbsolutePath(game.InstallDirectory),
                    Icon = GetLocalIconPath(game),
                    LastPlayedUtc = ToUtcTimestamp(game.LastActivity),
                });
            }

            var snapshot = new VigilSnapshot
            {
                SchemaVersion = 1,
                GeneratedAtUtc = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture),
                RefreshRequestId = refreshRequestId,
                Games = records,
            };

            try
            {
                using (var stream = new FileStream(
                    temporaryPath,
                    FileMode.Create,
                    FileAccess.Write,
                    FileShare.None))
                {
                    var serializer = new DataContractJsonSerializer(typeof(VigilSnapshot));
                    serializer.WriteObject(stream, snapshot);
                    stream.Flush(true);
                }

                ReplaceSnapshot(temporaryPath, targetPath);
            }
            finally
            {
                TryDelete(temporaryPath);
            }
        }

        private string GetLocalIconPath(Game game)
        {
            if (string.IsNullOrWhiteSpace(game.Icon))
            {
                return null;
            }

            try
            {
                var resolved = PlayniteApi.Database.GetFullFilePath(game.Icon);
                if (!string.IsNullOrWhiteSpace(resolved) &&
                    Path.IsPathRooted(resolved) &&
                    File.Exists(resolved))
                {
                    return resolved;
                }
            }
            catch (Exception ex)
            {
                logger.Warn($"Vigil Overlay Bridge ignored icon for game {game.Id:D}: {ex.Message}");
            }

            return null;
        }

        private static string GetOptionalAbsolutePath(string value)
        {
            if (string.IsNullOrWhiteSpace(value))
            {
                return null;
            }

            try
            {
                return Path.IsPathRooted(value) ? value : null;
            }
            catch (ArgumentException)
            {
                return null;
            }
        }

        private static string ToUtcTimestamp(DateTime? value)
        {
            return value.HasValue
                ? value.Value.ToUniversalTime().ToString("o", CultureInfo.InvariantCulture)
                : null;
        }

        private static void ReplaceSnapshot(string temporaryPath, string targetPath)
        {
            if (File.Exists(targetPath))
            {
                File.Replace(temporaryPath, targetPath, null);
            }
            else
            {
                File.Move(temporaryPath, targetPath);
            }
        }

        private static void TryDelete(string path)
        {
            try
            {
                if (File.Exists(path))
                {
                    File.Delete(path);
                }
            }
            catch
            {
                // Best-effort cleanup only. A future refresh uses FileMode.Create.
            }
        }
    }

    [DataContract]
    internal sealed class VigilSnapshot
    {
        [DataMember(Name = "schema_version", Order = 1)]
        public int SchemaVersion { get; set; }

        [DataMember(Name = "generated_at_utc", Order = 2)]
        public string GeneratedAtUtc { get; set; }

        [DataMember(Name = "refresh_request_id", Order = 3, EmitDefaultValue = false)]
        public string RefreshRequestId { get; set; }

        [DataMember(Name = "games", Order = 4)]
        public List<VigilGameRecord> Games { get; set; }
    }

    [DataContract]
    internal sealed class VigilRefreshRequest
    {
        [DataMember(Name = "request_id", Order = 1)]
        public string RequestId { get; set; }

        [DataMember(Name = "requested_at_utc", Order = 2)]
        public string RequestedAtUtc { get; set; }
    }

    [DataContract]
    internal sealed class VigilGameRecord
    {
        [DataMember(Name = "id", Order = 1)]
        public string Id { get; set; }

        [DataMember(Name = "title", Order = 2)]
        public string Title { get; set; }

        [DataMember(Name = "is_installed", Order = 3)]
        public bool IsInstalled { get; set; }

        [DataMember(Name = "install_directory", Order = 4)]
        public string InstallDirectory { get; set; }

        [DataMember(Name = "icon", Order = 5)]
        public string Icon { get; set; }

        [DataMember(Name = "last_played_utc", Order = 6)]
        public string LastPlayedUtc { get; set; }
    }
}
