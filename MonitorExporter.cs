#region Using declarations
using System;
using System.Collections.Generic;
using System.ComponentModel.DataAnnotations;
using System.IO;
using System.Linq;
using System.Text;
using System.Timers;
using NinjaTrader.Cbi;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Strategies;
#endregion

// MonitorExporter
// -----------------------------------------------------------------------------
// A non-trading strategy whose only job is to write NT8 status to local JSON
// files on a wall-clock timer. A separate Python service reads these files
// and pushes the data to Google Sheets.
//
// Writes three files:
//   heartbeat.json  - rewritten every N seconds with status snapshot
//   executions.log  - append-only, one JSON line per fill, written immediately
//   strategies.json - rewritten every N seconds, list of enabled strategies
//
// All file I/O is local disk (sub-millisecond). Network calls happen in the
// Python service, fully decoupled from NT8.
// -----------------------------------------------------------------------------

namespace NinjaTrader.NinjaScript.Strategies
{
    public class MonitorExporter : Strategy
    {
        private System.Timers.Timer pollTimer;
        private readonly object writeLock = new object();
        private DateTime lastTimerRun = DateTime.MinValue;
        private readonly List<Account> subscribedAccounts = new List<Account>();

        #region Parameters

        [Display(Name = "Output Folder", Order = 1, GroupName = "Monitor")]
        public string OutputFolder { get; set; }

        [Range(5, 600)]
        [Display(Name = "Poll Interval (seconds)", Order = 2, GroupName = "Monitor")]
        public int PollIntervalSeconds { get; set; }

        [Display(Name = "Accounts", Order = 3, GroupName = "Monitor",
            Description = "Comma-separated list of accounts to monitor, or 'ALL' for every account NT knows about. Examples: 'ALL' or 'Sim101,MyLive'")]
        public string AccountNames { get; set; }

        [Display(Name = "Log Verbose Errors", Order = 4, GroupName = "Monitor")]
        public bool VerboseErrors { get; set; }

        #endregion

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "Writes NT8 status to local JSON files for external pickup.";
                Name = "MonitorExporter";
                Calculate = Calculate.OnBarClose;
                IsExitOnSessionCloseStrategy = false;
                IsFillLimitOnTouch = false;
                IncludeCommission = false;
                IsInstantiatedOnEachOptimizationIteration = false;

                OutputFolder = @"C:\NT_Logger";
                PollIntervalSeconds = 30;
                AccountNames = "ALL";
                VerboseErrors = false;
            }
            else if (State == State.Configure)
            {
                // Make sure the output folder exists before we try to write to it
                try
                {
                    if (!Directory.Exists(OutputFolder))
                        Directory.CreateDirectory(OutputFolder);
                }
                catch (Exception ex)
                {
                    Print("MonitorExporter: could not create output folder: " + ex.Message);
                }
            }
            else if (State == State.DataLoaded)
            {
                // Hook execution events on every targeted account so fills are written immediately
                List<Account> targets = FindTargetAccounts();
                if (targets.Count == 0)
                {
                    Print("MonitorExporter: no matching accounts found at startup. " +
                          "Status snapshots will still run; will retry account discovery on each poll.");
                }
                foreach (Account a in targets)
                {
                    a.ExecutionUpdate += OnAccountExecutionUpdate;
                    subscribedAccounts.Add(a);
                    Print(string.Format("MonitorExporter: subscribed to executions on '{0}'.", a.Name));
                }

                // Wall-clock timer fires whether the market is open or not
                pollTimer = new System.Timers.Timer(PollIntervalSeconds * 1000);
                pollTimer.Elapsed += OnPollTimerElapsed;
                pollTimer.AutoReset = true;
                pollTimer.Start();

                // Write one snapshot immediately so files exist for the Python side to find
                WriteHeartbeat();
                WriteStrategies();
            }
            else if (State == State.Terminated)
            {
                if (pollTimer != null)
                {
                    pollTimer.Stop();
                    pollTimer.Elapsed -= OnPollTimerElapsed;
                    pollTimer.Dispose();
                    pollTimer = null;
                }

                foreach (Account a in subscribedAccounts)
                {
                    try { a.ExecutionUpdate -= OnAccountExecutionUpdate; } catch { }
                }
                subscribedAccounts.Clear();

                // Mark the heartbeat as stopped so Python can tell the difference
                // between "NT crashed" and "monitor was disabled cleanly"
                TryWriteFile("heartbeat.json", BuildStoppedJson());
            }
        }

        // OnBarUpdate is required to exist but does no work for this strategy
        protected override void OnBarUpdate() { }

        // ---------------------------------------------------------------------
        // Timer-driven snapshot writes
        // ---------------------------------------------------------------------

        private void OnPollTimerElapsed(object sender, ElapsedEventArgs e)
        {
            // Timer fires on a worker thread, not NT's main thread.
            // File I/O here does not block tick processing.
            try
            {
                lastTimerRun = DateTime.UtcNow;
                WriteHeartbeat();
                WriteStrategies();
            }
            catch (Exception ex)
            {
                if (VerboseErrors)
                    Print("MonitorExporter timer error: " + ex.Message);
            }
        }

        private void WriteHeartbeat()
        {
            List<Account> accts = FindTargetAccounts();
            StringBuilder sb = new StringBuilder();
            sb.Append("{");
            sb.AppendFormat("\"timestamp_utc\":\"{0}\",", DateTime.UtcNow.ToString("o"));
            sb.AppendFormat("\"poll_interval_sec\":{0},", PollIntervalSeconds);
            sb.AppendFormat("\"accounts_filter\":\"{0}\",", JsonEscape(AccountNames));
            sb.Append("\"accounts\":[");

            bool firstAcct = true;
            foreach (Account acct in accts)
            {
                // Wrap each account so a single bad one doesn't kill the whole
                // snapshot. Common failure mode: account is mid-(dis)connect
                // and accessing properties throws.
                try
                {
                    string acctJson = BuildAccountJson(acct);
                    if (!firstAcct) sb.Append(",");
                    sb.Append(acctJson);
                    firstAcct = false;
                }
                catch (Exception ex)
                {
                    if (VerboseErrors)
                        Print("MonitorExporter: skipping account '" +
                              acct.Name + "' due to error: " + ex.Message);
                }
            }

            sb.Append("]}");
            TryWriteFile("heartbeat.json", sb.ToString());
        }

        private string BuildAccountJson(Account acct)
        {
            double cash = acct.Get(AccountItem.CashValue, Currency.UsDollar);
            double realized = acct.Get(AccountItem.RealizedProfitLoss, Currency.UsDollar);
            double buyingPower = acct.Get(AccountItem.BuyingPower, Currency.UsDollar);
            bool connected = acct.Connection != null &&
                acct.Connection.Status == ConnectionStatus.Connected;

            StringBuilder sb = new StringBuilder();
            sb.Append("{");
            sb.AppendFormat("\"name\":\"{0}\",", JsonEscape(acct.Name));
            sb.AppendFormat("\"connection\":\"{0}\",",
                JsonEscape(acct.Connection != null ? acct.Connection.Options.Name : ""));
            sb.AppendFormat("\"connected\":{0},", connected ? "true" : "false");
            sb.AppendFormat("\"cash_value\":{0},", FormatNum(cash));
            sb.AppendFormat("\"realized_pnl\":{0},", FormatNum(realized));
            sb.AppendFormat("\"buying_power\":{0},", FormatNum(buyingPower));
            sb.Append("\"positions\":[");

            // Snapshot positions so we don't iterate a live-mutating collection
            List<Position> positions;
            try
            {
                positions = new List<Position>(acct.Positions);
            }
            catch
            {
                positions = new List<Position>();
            }

            bool firstPos = true;
            foreach (Position p in positions)
            {
                try
                {
                    if (p.MarketPosition == MarketPosition.Flat) continue;
                    if (!firstPos) sb.Append(",");
                    sb.Append("{");
                    sb.AppendFormat("\"instrument\":\"{0}\",", JsonEscape(p.Instrument.FullName));
                    sb.AppendFormat("\"side\":\"{0}\",", p.MarketPosition);
                    sb.AppendFormat("\"qty\":{0},", p.Quantity);
                    sb.AppendFormat("\"avg_price\":{0}", FormatNum(p.AveragePrice));
                    sb.Append("}");
                    firstPos = false;
                }
                catch
                {
                    // Skip individual broken position
                }
            }
            sb.Append("]}");
            return sb.ToString();
        }

        private void WriteStrategies()
        {
            // Enumerate strategies across every targeted account so Python can
            // verify that all expected strategies are still enabled.
            //
            // IMPORTANT: Account.Strategies returns StrategyBase, not Strategy.
            // Iterating as Strategy throws an InvalidCastException on certain
            // strategy implementations (subclasses that don't cleanly downcast).
            // StrategyBase exposes everything we need (Name, State, Instrument).
            StringBuilder sb = new StringBuilder();
            sb.Append("{");
            sb.AppendFormat("\"timestamp_utc\":\"{0}\",", DateTime.UtcNow.ToString("o"));
            sb.Append("\"strategies\":[");

            bool first = true;
            foreach (Account acct in FindTargetAccounts())
            {
                // Snapshot the strategy collection inside a try block — if one
                // account's strategies list is being mutated mid-poll we don't
                // want to lose every other account's data.
                List<StrategyBase> snapshot;
                try
                {
                    snapshot = new List<StrategyBase>(acct.Strategies);
                }
                catch (Exception ex)
                {
                    if (VerboseErrors)
                        Print("MonitorExporter: failed to read strategies for '" +
                              acct.Name + "': " + ex.Message);
                    continue;
                }

                foreach (StrategyBase s in snapshot)
                {
                    try
                    {
                        if (!first) sb.Append(",");
                        string instrument = (s.Instrument != null) ? s.Instrument.FullName : "";
                        sb.Append("{");
                        sb.AppendFormat("\"account\":\"{0}\",", JsonEscape(acct.Name));
                        sb.AppendFormat("\"name\":\"{0}\",", JsonEscape(s.Name));
                        sb.AppendFormat("\"instrument\":\"{0}\",", JsonEscape(instrument));
                        sb.AppendFormat("\"state\":\"{0}\",", s.State);
                        sb.AppendFormat("\"enabled\":{0}", s.State == State.Realtime ||
                            s.State == State.Historical ? "true" : "false");
                        sb.Append("}");
                        first = false;
                    }
                    catch (Exception ex)
                    {
                        // Trailing comma cleanup if we wrote one above
                        if (sb.Length > 0 && sb[sb.Length - 1] == ',')
                            sb.Length -= 1;
                        if (VerboseErrors)
                            Print("MonitorExporter: skipped strategy due to error: " + ex.Message);
                    }
                }
            }

            sb.Append("]}");
            TryWriteFile("strategies.json", sb.ToString());
        }

        private string BuildStoppedJson()
        {
            return string.Format(
                "{{\"timestamp_utc\":\"{0}\",\"status\":\"stopped\",\"reason\":\"strategy_terminated\"}}",
                DateTime.UtcNow.ToString("o"));
        }

        // ---------------------------------------------------------------------
        // Execution event handler — fires immediately on every fill
        // ---------------------------------------------------------------------

        private void OnAccountExecutionUpdate(object sender, ExecutionEventArgs e)
        {
            try
            {
                Execution ex = e.Execution;
                if (ex == null) return;

                StringBuilder sb = new StringBuilder();
                sb.Append("{");
                sb.AppendFormat("\"timestamp_utc\":\"{0}\",", DateTime.UtcNow.ToString("o"));
                sb.AppendFormat("\"exec_time\":\"{0}\",", ex.Time.ToUniversalTime().ToString("o"));
                sb.AppendFormat("\"account\":\"{0}\",", JsonEscape(ex.Account != null ? ex.Account.Name : ""));
                sb.AppendFormat("\"instrument\":\"{0}\",", JsonEscape(ex.Instrument.FullName));
                sb.AppendFormat("\"action\":\"{0}\",", ex.MarketPosition);
                sb.AppendFormat("\"qty\":{0},", ex.Quantity);
                sb.AppendFormat("\"price\":{0},", FormatNum(ex.Price));
                sb.AppendFormat("\"order_id\":\"{0}\",", JsonEscape(ex.OrderId ?? ""));
                sb.AppendFormat("\"exec_id\":\"{0}\",", JsonEscape(ex.ExecutionId ?? ""));
                sb.AppendFormat("\"strategy\":\"{0}\",",
                    JsonEscape(ex.Order != null && ex.Order.FromEntrySignal != null
                        ? ex.Order.FromEntrySignal : ""));
                sb.AppendFormat("\"commission\":{0}", FormatNum(ex.Commission));
                sb.Append("}");

                TryAppendFile("executions.log", sb.ToString() + Environment.NewLine);
            }
            catch (Exception ex2)
            {
                if (VerboseErrors)
                    Print("MonitorExporter execution log error: " + ex2.Message);
            }
        }

        // ---------------------------------------------------------------------
        // Helpers
        // ---------------------------------------------------------------------

        private List<Account> FindTargetAccounts()
        {
            // Account.All is the canonical list of accounts NT knows about.
            // If AccountNames is "ALL" (or blank) we return everything; otherwise
            // we match by name (case-insensitive, comma-separated).
            List<Account> result = new List<Account>();
            string filter = (AccountNames ?? "").Trim();
            bool allMode = filter.Length == 0 ||
                           string.Equals(filter, "ALL", StringComparison.OrdinalIgnoreCase) ||
                           filter == "*";

            HashSet<string> wanted = null;
            if (!allMode)
            {
                wanted = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                foreach (string name in filter.Split(','))
                {
                    string trimmed = name.Trim();
                    if (trimmed.Length > 0) wanted.Add(trimmed);
                }
            }

            lock (Account.All)
            {
                foreach (Account a in Account.All)
                {
                    if (allMode || wanted.Contains(a.Name))
                        result.Add(a);
                }
            }
            return result;
        }

        private void TryWriteFile(string fileName, string content)
        {
            string path = Path.Combine(OutputFolder, fileName);
            // Lock guards against the unlikely case of the timer thread and the
            // termination handler trying to write the same file simultaneously
            lock (writeLock)
            {
                try
                {
                    // Write to a temp file then move into place so Python never
                    // sees a half-written file
                    string tmp = path + ".tmp";
                    File.WriteAllText(tmp, content);
                    if (File.Exists(path)) File.Delete(path);
                    File.Move(tmp, path);
                }
                catch (Exception ex)
                {
                    if (VerboseErrors)
                        Print("MonitorExporter write error (" + fileName + "): " + ex.Message);
                }
            }
        }

        private void TryAppendFile(string fileName, string line)
        {
            string path = Path.Combine(OutputFolder, fileName);
            lock (writeLock)
            {
                try
                {
                    File.AppendAllText(path, line);
                }
                catch (Exception ex)
                {
                    if (VerboseErrors)
                        Print("MonitorExporter append error (" + fileName + "): " + ex.Message);
                }
            }
        }

        private static string JsonEscape(string s)
        {
            if (string.IsNullOrEmpty(s)) return "";
            return s.Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\r", "").Replace("\n", " ");
        }

        private static string FormatNum(double d)
        {
            if (double.IsNaN(d) || double.IsInfinity(d)) return "null";
            return d.ToString("0.########", System.Globalization.CultureInfo.InvariantCulture);
        }
    }
}
