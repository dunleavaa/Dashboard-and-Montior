#region Using declarations
using System;
using System.Collections.Generic;
using System.ComponentModel.DataAnnotations;
using System.IO;
using System.Linq;
using System.Text;
using System.Timers;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Indicators;
using NinjaTrader.NinjaScript.Strategies;
#endregion

// MonitorExporter
// -----------------------------------------------------------------------------
// A non-trading strategy whose only job is to write NT8 status to local JSON
// files on a wall-clock timer. A separate Python service reads these files
// and pushes the data to Google Sheets.
//
// Writes four files:
//   heartbeat.json  - rewritten every N seconds with status snapshot
//   executions.log  - append-only, one JSON line per fill, written immediately
//   strategies.json - rewritten every N seconds, list of enabled strategies
//   atr_ranges.json - rewritten every N seconds; latest ATR(period) per
//                     configured instrument across 5m / 15m / daily timeframes.
//                     Intended for the AI-signal webhook bridge to use as a
//                     "sanity envelope" — reject signals whose SL or TP
//                     distance is implausible relative to current volatility.
//                     Tick size and point value are also exported so the
//                     consumer can convert ATR points to dollars per contract.
//
// All file I/O is local disk (sub-millisecond). Network calls happen in the
// Python service, fully decoupled from NT8.
//
// Threading note: ATR values are computed on NT's data thread inside
// OnBarUpdate (one bar-close per series fires it), cached in a dictionary,
// and read by the timer thread when writing atr_ranges.json. A dedicated
// lock (atrLock) protects the cache; file writes use writeLock as before.
// -----------------------------------------------------------------------------

namespace NinjaTrader.NinjaScript.Strategies
{
    public class MonitorExporter : Strategy
    {
        private System.Timers.Timer pollTimer;
        private readonly object writeLock = new object();
        private DateTime lastTimerRun = DateTime.MinValue;
        private readonly List<Account> subscribedAccounts = new List<Account>();

        // ATR export -----------------------------------------------------------
        // One entry per (instrument, timeframe), keyed by the BarsInProgress
        // index that NT will assign when we call AddDataSeries during
        // State.Configure. The cache is written by OnBarUpdate on NT's data
        // thread and read by the wall-clock timer on a worker thread, so all
        // access goes through atrLock.
        private readonly object atrLock = new object();
        private readonly Dictionary<int, AtrSeriesInfo> atrSeries =
            new Dictionary<int, AtrSeriesInfo>();
        private readonly List<string> atrInstrumentOrder = new List<string>();

        private class AtrSeriesInfo
        {
            public string  Instrument;     // e.g. "MNQ 06-26"
            public string  Timeframe;      // "1m" | "5m" | "15m" | "daily"
            public double  Atr        = double.NaN;
            public double  TickSize   = double.NaN;
            public double  PointValue = double.NaN;
            public DateTime LastUpdated = DateTime.MinValue;
            // v2.31.x diagnostic: bar count loaded for this series. Tracked
            // independently of Atr so we can distinguish "OnBarUpdate has
            // never fired" (BarCount == 0 — AddDataSeries silently failed
            // or data not available) from "OnBarUpdate fired but not
            // enough bars yet for ATR(period)" (0 < BarCount < AtrPeriod).
            public int     BarCount   = 0;
        }

        // Trade tracking -------------------------------------------------------
        // Per (account, instrument) pair: tracks an in-flight position from
        // first entry fill (position transitions from 0 to non-zero) to flat
        // (back to 0). Multiple entry fills are allowed (scale-in) and
        // multiple exit fills are allowed (half-close + final exit pattern
        // SphinxFib uses, or partial fills on stop hits). When the position
        // returns to 0, the accumulated trade record is emitted to
        // trades.json and broadcast to subscribers via the file write.
        //
        // All state mutations happen on NT's account-event thread, so we
        // protect with tradesLock for cross-thread reads (the timer thread
        // might also write trades.json on a snapshot tick).
        private readonly object tradesLock = new object();
        private readonly Dictionary<string, TradeState> tradeStates =
            new Dictionary<string, TradeState>();
        private readonly List<CompletedTrade> completedTrades =
            new List<CompletedTrade>();
        private const int MAX_COMPLETED_TRADES = 200;

        private class OpenLot
        {
            public DateTime Time;
            public double   Price;
            public int      Qty;       // positive (direction held in TradeState)
        }

        private class TradeState
        {
            public string AccountName;
            public string Instrument;
            public int    Direction;          // +1 long, -1 short
            public List<OpenLot> OpenLots = new List<OpenLot>();
            public DateTime FirstEntryTime;
            public double   EntryNotional;    // sum of qty * price across entries
            public int      EntryQtyCum;
            public DateTime LastExitTime;
            public double   ExitNotional;
            public int      ExitQtyCum;
            public double   RealizedPnl;      // running $ realized as exits fill
            public string   FirstEntryName;
            public string   LastExitName;
            public double   PointValue;
            public bool     AnyEntrySphinx;   // true if any entry fill was a SphinxFib order
        }

        private class CompletedTrade
        {
            public string   Id;
            public string   AccountName;
            public string   Instrument;
            public string   Side;            // "L" or "S"
            public int      Qty;
            public DateTime EntryTime;
            public DateTime ExitTime;
            public double   DurationMinutes;
            public double   EntryAvgPrice;
            public double   ExitAvgPrice;
            public double   PnlDollars;
            public double   PnlPoints;
            public string   Source;          // "sphinxfib" | "ghost"
            public string   ExitReason;      // "tp_hit" | "sl_hit" | "manual_close" | "auto"
        }

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

        // ATR export -----------------------------------------------------------
        // Comma-separated list of NT8 instrument identifiers including contract
        // month. Each instrument gets three data series added at State.Configure
        // (5-minute, 15-minute, daily). Leave blank to disable ATR export.
        //
        // Contract months are NOT auto-resolved — update this string at each
        // futures rollover (typically the 2nd week of the contract month). The
        // strategy will need to be disabled and re-enabled for the change to
        // take effect, because AddDataSeries is only called during Configure.
        [Display(Name = "ATR Instruments", Order = 5, GroupName = "ATR",
            Description = "Comma-separated NT8 contract codes. " +
                          "Update at rollover. Examples: 'MNQ 06-26,MES 06-26,MGC 06-26'. " +
                          "Leave blank to disable ATR export.")]
        public string AtrInstruments { get; set; }

        [Range(2, 200)]
        [Display(Name = "ATR Period", Order = 6, GroupName = "ATR",
            Description = "Lookback period for ATR calculation. 14 is the standard default.")]
        public int AtrPeriod { get; set; }

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

                // ATR export defaults — update contract months at rollover
                AtrInstruments = "MNQ 06-26,MES 06-26,MGC 06-26";
                AtrPeriod      = 14;
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

                // ATR export: parse the instrument list and add three data series
                // per instrument (5m / 15m / daily). BarsInProgress for added
                // series starts at 1, in the exact order of AddDataSeries calls.
                // We pre-populate atrSeries with that mapping so OnBarUpdate
                // only has to do dictionary writes (no parsing) on the data
                // thread.
                atrSeries.Clear();
                atrInstrumentOrder.Clear();
                if (!string.IsNullOrWhiteSpace(AtrInstruments))
                {
                    int nextBarsIdx = 1; // 0 is the primary/chart instrument
                    foreach (string raw in AtrInstruments.Split(','))
                    {
                        string sym = raw.Trim();
                        if (sym.Length == 0) continue;
                        try
                        {
                            AddDataSeries(sym, BarsPeriodType.Minute, 1);
                            atrSeries[nextBarsIdx++] = new AtrSeriesInfo
                                { Instrument = sym, Timeframe = "1m" };

                            AddDataSeries(sym, BarsPeriodType.Minute, 5);
                            atrSeries[nextBarsIdx++] = new AtrSeriesInfo
                                { Instrument = sym, Timeframe = "5m" };

                            AddDataSeries(sym, BarsPeriodType.Minute, 15);
                            atrSeries[nextBarsIdx++] = new AtrSeriesInfo
                                { Instrument = sym, Timeframe = "15m" };

                            // Daily ATR via 1440-minute bars instead of
                            // BarsPeriodType.Day. Same calculation, but goes
                            // through the same intraday code path that we
                            // know works for 1m/5m/15m — sidesteps a NT8
                            // quirk where BarsPeriodType.Day can silently
                            // fail to deliver bars depending on the data
                            // provider / session template on the host chart.
                            // Observed in the field on 2026-05-14: 1m/5m/15m
                            // populated normally but atr_daily came back null
                            // for all three instruments. (See conversation
                            // history. If this ever needs to revert, change
                            // back to BarsPeriodType.Day, 1.)
                            AddDataSeries(sym, BarsPeriodType.Minute, 1440);
                            atrSeries[nextBarsIdx++] = new AtrSeriesInfo
                                { Instrument = sym, Timeframe = "daily" };

                            atrInstrumentOrder.Add(sym);
                            Print(string.Format(
                                "MonitorExporter: ATR series added for '{0}' (1m/5m/15m/daily).",
                                sym));
                        }
                        catch (Exception ex)
                        {
                            // Most common failure here is a bad contract code
                            // (wrong month, missing space, etc.). Log loudly so
                            // it's obvious why ATR for one symbol is missing.
                            Print(string.Format(
                                "MonitorExporter: AddDataSeries failed for '{0}': {1}. " +
                                "Check the contract code and rollover month.",
                                sym, ex.Message));
                        }
                    }
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
                WriteAtrRanges();
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

        // OnBarUpdate fires once per bar close on whichever data series just
        // closed (BarsInProgress identifies which). Series 0 is the chart's
        // primary instrument and is ignored here — this strategy doesn't trade
        // and doesn't care about it. Series 1+ are the ATR-tracked instruments
        // we added in State.Configure; each one gets its ATR cached for later
        // pickup by the timer-driven WriteAtrRanges.
        protected override void OnBarUpdate()
        {
            int bip = BarsInProgress;
            if (bip <= 0) return;

            AtrSeriesInfo info;
            if (!atrSeries.TryGetValue(bip, out info)) return;

            // Track bar count BEFORE the AtrPeriod guard so the JSON exposes
            // "OnBarUpdate fired, but not enough history yet" separately from
            // "OnBarUpdate has never fired for this series" (most likely a
            // silent AddDataSeries failure or a missing data subscription).
            int currentBars = CurrentBars[bip];
            lock (atrLock)
            {
                info.BarCount = currentBars;
            }

            // Need at least AtrPeriod bars of history for a meaningful ATR
            if (currentBars < AtrPeriod) return;

            try
            {
                double atrValue   = ATR(BarsArray[bip], AtrPeriod)[0];
                Instrument inst   = BarsArray[bip].Instrument;
                double tickSize   = (inst != null && inst.MasterInstrument != null)
                                    ? inst.MasterInstrument.TickSize   : double.NaN;
                double pointValue = (inst != null && inst.MasterInstrument != null)
                                    ? inst.MasterInstrument.PointValue : double.NaN;

                lock (atrLock)
                {
                    info.Atr         = atrValue;
                    info.TickSize    = tickSize;
                    info.PointValue  = pointValue;
                    info.LastUpdated = DateTime.UtcNow;
                }
            }
            catch (Exception ex)
            {
                // Don't let one bad series kill the whole bar-update path.
                if (VerboseErrors)
                    Print(string.Format(
                        "MonitorExporter ATR calc error (series={0} {1} {2}): {3}",
                        bip, info.Instrument, info.Timeframe, ex.Message));
            }
        }

        // ---------------------------------------------------------------------
        // ATR snapshot writer — called from the wall-clock timer
        // ---------------------------------------------------------------------
        //
        // Output JSON shape (consumed by the AI-signal webhook bridge as a
        // sanity envelope for SL/TP distances):
        //
        // {
        //   "timestamp_utc": "...",
        //   "atr_period": 14,
        //   "instruments": {
        //     "MNQ 06-26": {
        //       "atr_1m":     3.2,    // ATR(14) on 1-minute bars, in price points
        //       "atr_5m":    11.8,    // ATR(14) on 5-minute bars
        //       "atr_15m":   22.3,
        //       "atr_daily": 287.5,
        //       "tick_size":  0.25,
        //       "point_value": 2.0,
        //       "updated_1m":    "2026-05-14T14:36:00.000Z",
        //       "updated_5m":    "2026-05-14T14:35:00.000Z",
        //       "updated_15m":   "2026-05-14T14:30:00.000Z",
        //       "updated_daily": "2026-05-13T21:00:00.000Z"
        //     },
        //     ...
        //   }
        // }
        //
        // Any series that hasn't yet accumulated AtrPeriod bars will report null
        // for that timeframe (rather than 0 or a stale value). At session start
        // the daily series will typically already be populated from history; the
        // intraday series populate within their first AtrPeriod bar closes
        // (~14 minutes for 1m, ~70 minutes for 5m, etc.).
        private void WriteAtrRanges()
        {
            if (atrInstrumentOrder.Count == 0) return;

            // Snapshot the cache under the lock so we don't read a half-updated
            // entry. AtrSeriesInfo is shallow-copied; the underlying doubles
            // are value types so the snapshot is genuinely independent.
            Dictionary<int, AtrSeriesInfo> snapshot;
            lock (atrLock)
            {
                snapshot = new Dictionary<int, AtrSeriesInfo>(atrSeries.Count);
                foreach (var kv in atrSeries)
                {
                    snapshot[kv.Key] = new AtrSeriesInfo
                    {
                        Instrument  = kv.Value.Instrument,
                        Timeframe   = kv.Value.Timeframe,
                        Atr         = kv.Value.Atr,
                        TickSize    = kv.Value.TickSize,
                        PointValue  = kv.Value.PointValue,
                        LastUpdated = kv.Value.LastUpdated,
                        BarCount    = kv.Value.BarCount
                    };
                }
            }

            StringBuilder sb = new StringBuilder();
            sb.Append("{");
            sb.AppendFormat("\"timestamp_utc\":\"{0}\",", DateTime.UtcNow.ToString("o"));
            sb.AppendFormat("\"atr_period\":{0},", AtrPeriod);
            sb.Append("\"instruments\":{");

            bool firstInst = true;
            foreach (string sym in atrInstrumentOrder)
            {
                AtrSeriesInfo s1m   = FindInSnapshot(snapshot, sym, "1m");
                AtrSeriesInfo s5m   = FindInSnapshot(snapshot, sym, "5m");
                AtrSeriesInfo s15m  = FindInSnapshot(snapshot, sym, "15m");
                AtrSeriesInfo sDay  = FindInSnapshot(snapshot, sym, "daily");

                // Pick whichever entry has tick_size populated to report
                // instrument metadata. They should all match, but the
                // intraday ones populate first so prefer the shortest TF.
                AtrSeriesInfo meta = (s1m  != null && !double.IsNaN(s1m.TickSize))  ? s1m  :
                                     (s5m  != null && !double.IsNaN(s5m.TickSize))  ? s5m  :
                                     (s15m != null && !double.IsNaN(s15m.TickSize)) ? s15m :
                                     (sDay != null && !double.IsNaN(sDay.TickSize)) ? sDay : null;

                if (!firstInst) sb.Append(",");
                sb.AppendFormat("\"{0}\":{{", JsonEscape(sym));
                sb.AppendFormat("\"atr_1m\":{0},",       FormatAtr(s1m));
                sb.AppendFormat("\"atr_5m\":{0},",       FormatAtr(s5m));
                sb.AppendFormat("\"atr_15m\":{0},",      FormatAtr(s15m));
                sb.AppendFormat("\"atr_daily\":{0},",    FormatAtr(sDay));
                sb.AppendFormat("\"tick_size\":{0},",    meta != null ? FormatNum(meta.TickSize)   : "null");
                sb.AppendFormat("\"point_value\":{0},",  meta != null ? FormatNum(meta.PointValue) : "null");
                // v2.31.x: bar-count diagnostics. Lets the consumer (and you)
                // distinguish "OnBarUpdate hasn't fired for this series yet"
                // (0) from "fired but still warming up" (<AtrPeriod) from
                // "healthy" (>=AtrPeriod). A null atr_* combined with a 0
                // bar count usually means AddDataSeries silently failed or
                // the data subscription doesn't cover that bar type.
                sb.AppendFormat("\"bars_loaded_1m\":{0},",    s1m  != null ? s1m.BarCount.ToString()  : "0");
                sb.AppendFormat("\"bars_loaded_5m\":{0},",    s5m  != null ? s5m.BarCount.ToString()  : "0");
                sb.AppendFormat("\"bars_loaded_15m\":{0},",   s15m != null ? s15m.BarCount.ToString() : "0");
                sb.AppendFormat("\"bars_loaded_daily\":{0},", sDay != null ? sDay.BarCount.ToString() : "0");
                sb.AppendFormat("\"updated_1m\":{0},",     FormatTime(s1m));
                sb.AppendFormat("\"updated_5m\":{0},",     FormatTime(s5m));
                sb.AppendFormat("\"updated_15m\":{0},",    FormatTime(s15m));
                sb.AppendFormat("\"updated_daily\":{0}",   FormatTime(sDay));
                sb.Append("}");
                firstInst = false;
            }

            sb.Append("}}");
            TryWriteFile("atr_ranges.json", sb.ToString());
        }

        private static AtrSeriesInfo FindInSnapshot(
            Dictionary<int, AtrSeriesInfo> snap, string sym, string tf)
        {
            foreach (var info in snap.Values)
            {
                if (info.Instrument == sym && info.Timeframe == tf)
                    return info;
            }
            return null;
        }

        private static string FormatAtr(AtrSeriesInfo s)
        {
            if (s == null || double.IsNaN(s.Atr)) return "null";
            return FormatNum(s.Atr);
        }

        private static string FormatTime(AtrSeriesInfo s)
        {
            if (s == null || s.LastUpdated == DateTime.MinValue) return "null";
            return "\"" + s.LastUpdated.ToString("o") + "\"";
        }

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
                WriteAtrRanges();
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

                // Trade tracking: update position state and emit trades.json on
                // each completed round-trip (position back to 0).
                ProcessExecutionForTrades(ex);
            }
            catch (Exception ex2)
            {
                if (VerboseErrors)
                    Print("MonitorExporter execution log error: " + ex2.Message);
            }
        }

        // ---------------------------------------------------------------------
        // Trade tracking — FIFO match per (account, instrument)
        // ---------------------------------------------------------------------

        // Per-fill state machine:
        //   - position is flat -> this fill opens a new TradeState
        //   - position is non-zero, same direction as fill -> scale-in (push lot)
        //   - position is non-zero, opposite direction -> close lots FIFO,
        //     accumulate realized P&L. When all lots consumed, emit a
        //     CompletedTrade record and clear the state.
        //
        // Reversals (an exit fill larger than current open position) are split:
        // the part that closes the existing position completes the trade; the
        // remainder opens a new trade in the opposite direction. Most users
        // don't reverse, but it costs nothing to handle.
        private void ProcessExecutionForTrades(Execution ex)
        {
            if (ex == null || ex.Instrument == null) return;
            string accountName = ex.Account != null ? ex.Account.Name : "";
            string instName    = ex.Instrument.FullName;
            string key         = accountName + "|" + instName;
            int    fillDir     = ex.MarketPosition == MarketPosition.Long ? +1 : -1;
            int    fillQty     = ex.Quantity;
            double fillPrice   = ex.Price;
            DateTime fillTime  = ex.Time;
            string  orderName  = (ex.Order != null && ex.Order.Name != null) ? ex.Order.Name : "";

            double pv = 0, ts = 0;
            if (ex.Instrument.MasterInstrument != null)
            {
                pv = ex.Instrument.MasterInstrument.PointValue;
                ts = ex.Instrument.MasterInstrument.TickSize;
            }

            bool tradeCompletedThisCall = false;

            lock (tradesLock)
            {
                int remaining = fillQty;
                while (remaining > 0)
                {
                    TradeState st;
                    bool hasState = tradeStates.TryGetValue(key, out st);

                    if (!hasState || st.Direction == 0)
                    {
                        // Flat position — this fill opens a new trade.
                        st = new TradeState
                        {
                            AccountName     = accountName,
                            Instrument      = instName,
                            Direction       = fillDir,
                            FirstEntryTime  = fillTime,
                            FirstEntryName  = orderName,
                            PointValue      = pv,
                            AnyEntrySphinx  = orderName.StartsWith("Sphinx"),
                        };
                        st.OpenLots.Add(new OpenLot { Time = fillTime, Price = fillPrice, Qty = remaining });
                        st.EntryNotional = fillPrice * remaining;
                        st.EntryQtyCum   = remaining;
                        tradeStates[key] = st;
                        remaining = 0;
                    }
                    else if (st.Direction == fillDir)
                    {
                        // Scale-in: same side as current open position.
                        st.OpenLots.Add(new OpenLot { Time = fillTime, Price = fillPrice, Qty = remaining });
                        st.EntryNotional += fillPrice * remaining;
                        st.EntryQtyCum   += remaining;
                        if (orderName.StartsWith("Sphinx")) st.AnyEntrySphinx = true;
                        remaining = 0;
                    }
                    else
                    {
                        // Opposite side — close lots FIFO.
                        int closedHere = 0;
                        while (remaining > 0 && st.OpenLots.Count > 0)
                        {
                            OpenLot lot = st.OpenLots[0];
                            int closeQty = Math.Min(remaining, lot.Qty);
                            double lotPnl = (st.Direction == +1)
                                ? (fillPrice - lot.Price) * pv * closeQty
                                : (lot.Price - fillPrice) * pv * closeQty;
                            st.RealizedPnl += lotPnl;
                            lot.Qty   -= closeQty;
                            remaining -= closeQty;
                            closedHere += closeQty;
                            if (lot.Qty == 0) st.OpenLots.RemoveAt(0);
                        }
                        st.ExitNotional += fillPrice * closedHere;
                        st.ExitQtyCum   += closedHere;
                        st.LastExitTime  = fillTime;
                        st.LastExitName  = orderName;

                        if (st.OpenLots.Count == 0)
                        {
                            // Trade complete.
                            EmitCompletedTrade(st);
                            tradeStates.Remove(key);
                            tradeCompletedThisCall = true;
                            // If `remaining > 0`, the fill was larger than the
                            // open position — loop continues and opens a new
                            // trade in the opposite direction (reversal).
                        }
                    }
                }
            }

            if (tradeCompletedThisCall) WriteTradesJson();
        }

        // Build a CompletedTrade from the now-flat TradeState and append to the
        // rolling completedTrades list (capped at MAX_COMPLETED_TRADES).
        // Caller must hold tradesLock.
        private void EmitCompletedTrade(TradeState st)
        {
            double entryAvg = st.EntryQtyCum > 0 ? (st.EntryNotional / st.EntryQtyCum) : 0;
            double exitAvg  = st.ExitQtyCum  > 0 ? (st.ExitNotional  / st.ExitQtyCum)  : 0;
            double pnlPts   = (st.Direction == +1) ? (exitAvg - entryAvg) : (entryAvg - exitAvg);
            double durMin   = (st.LastExitTime - st.FirstEntryTime).TotalMinutes;

            string source = st.AnyEntrySphinx ? "sphinxfib" : "ghost";
            string exitReason = InferExitReason(st);

            CompletedTrade t = new CompletedTrade
            {
                Id              = string.Format("{0}|{1}|{2:yyyyMMddTHHmmss}|{3}",
                    st.AccountName, st.Instrument, st.FirstEntryTime,
                    st.Direction == +1 ? "L" : "S"),
                AccountName     = st.AccountName,
                Instrument      = st.Instrument,
                Side            = st.Direction == +1 ? "L" : "S",
                Qty             = st.EntryQtyCum,
                EntryTime       = st.FirstEntryTime,
                ExitTime        = st.LastExitTime,
                DurationMinutes = durMin,
                EntryAvgPrice   = entryAvg,
                ExitAvgPrice    = exitAvg,
                PnlDollars      = st.RealizedPnl,
                PnlPoints       = pnlPts,
                Source          = source,
                ExitReason      = exitReason,
            };
            completedTrades.Add(t);
            while (completedTrades.Count > MAX_COMPLETED_TRADES)
                completedTrades.RemoveAt(0);
        }

        private static string InferExitReason(TradeState st)
        {
            // Priority: explicit name tag from last exit fill wins.
            string name = st.LastExitName ?? "";
            if (name.IndexOf("SL", StringComparison.OrdinalIgnoreCase) >= 0) return "sl_hit";
            if (name.IndexOf("TP", StringComparison.OrdinalIgnoreCase) >= 0) return "tp_hit";
            if (name.IndexOf("Flat", StringComparison.OrdinalIgnoreCase) >= 0) return "manual_close";
            if (name.IndexOf("Close", StringComparison.OrdinalIgnoreCase) >= 0) return "manual_close";
            // No tag — Ghost-style bracket fill. Infer from P&L sign which
            // bracket leg fired. Imperfect (a manual close in profit reads as
            // tp_hit) but reasonable default.
            if (st.RealizedPnl > 0)  return "tp_hit";
            if (st.RealizedPnl < 0)  return "sl_hit";
            return "auto";
        }

        // Serialize the rolling completedTrades list to trades.json under the
        // share folder. Atomic write (temp file + rename) so consumers never
        // read a half-written file. Called from the account-event thread; uses
        // tradesLock to snapshot before writing.
        private void WriteTradesJson()
        {
            List<CompletedTrade> snapshot;
            lock (tradesLock)
            {
                snapshot = new List<CompletedTrade>(completedTrades);
            }

            StringBuilder sb = new StringBuilder(snapshot.Count * 256);
            sb.Append("{");
            sb.AppendFormat("\"timestamp_utc\":\"{0}\",", DateTime.UtcNow.ToString("o"));
            sb.AppendFormat("\"count\":{0},", snapshot.Count);
            sb.Append("\"trades\":[");
            bool first = true;
            foreach (CompletedTrade t in snapshot)
            {
                if (!first) sb.Append(",");
                sb.Append("{");
                sb.AppendFormat("\"id\":\"{0}\",",             JsonEscape(t.Id));
                sb.AppendFormat("\"account\":\"{0}\",",        JsonEscape(t.AccountName));
                sb.AppendFormat("\"instrument\":\"{0}\",",     JsonEscape(t.Instrument));
                sb.AppendFormat("\"side\":\"{0}\",",           t.Side);
                sb.AppendFormat("\"qty\":{0},",                t.Qty);
                sb.AppendFormat("\"entry_time\":\"{0}\",",     t.EntryTime.ToString("o"));
                sb.AppendFormat("\"exit_time\":\"{0}\",",      t.ExitTime.ToString("o"));
                sb.AppendFormat("\"duration_min\":{0},",       FormatNum(t.DurationMinutes));
                sb.AppendFormat("\"entry_avg_price\":{0},",    FormatNum(t.EntryAvgPrice));
                sb.AppendFormat("\"exit_avg_price\":{0},",     FormatNum(t.ExitAvgPrice));
                sb.AppendFormat("\"pnl_dollars\":{0},",        FormatNum(t.PnlDollars));
                sb.AppendFormat("\"pnl_points\":{0},",         FormatNum(t.PnlPoints));
                sb.AppendFormat("\"source\":\"{0}\",",         t.Source);
                sb.AppendFormat("\"exit_reason\":\"{0}\"",     t.ExitReason);
                sb.Append("}");
                first = false;
            }
            sb.Append("]}");

            TryWriteFile("trades.json", sb.ToString());
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
