//+------------------------------------------------------------------+
//| GoldMacdCrossover.mq5                                            |
//|                                                                  |
//| A port of algo/strategy/macd_crossover.py (strategy_id           |
//| "xauusd_macd_crossover_v1") to a MetaTrader 5 expert advisor.    |
//|                                                                  |
//| Long on a bullish MACD cross, short on a bearish one, flat in    |
//| between, with a flat percentage stop and an optional trailing    |
//| profit stop layered on top.                                      |
//|                                                                  |
//| ====================================================================
//| WHAT THIS DELIBERATELY DOES NOT DO
//| ====================================================================
//| It does not use iMACD(). The built-in seeds its EMAs with an SMA |
//| of the first `period` values; algo/pricing/indicators.py seeds   |
//| with the FIRST VALUE (pandas adjust=False, which is what the     |
//| tools/macd_telegram_alert alert tool and TradingView use). Those |
//| two differ, and a signal here disagreeing with an alert there    |
//| about what a crossover is would defeat the point of the port.    |
//| So the three EMAs are computed here, recursively, in the same    |
//| order and in the same 64-bit floating point the Python uses.     |
//|                                                                  |
//| It does not reverse in one step. Closing a position consumes the |
//| crossing event that triggered it, and re-entry waits for the     |
//| NEXT cross rather than immediately flipping into the opposite    |
//| side. The Python calls this out as a real design choice that     |
//| costs roughly half of every reversal timeliness - not an         |
//| oversight - so it is preserved rather than quietly improved.     |
//|                                                                  |
//| ====================================================================
//| INDICATOR STATE: SEEDED FROM HISTORY, NOT PERSISTED
//| ====================================================================
//| The Python carries the three EMAs as running state and persists  |
//| them across a restart, because reseeding from zero would spend   |
//| warmup_bars() bars re-converging with no signal - worst exactly  |
//| during a restart with a position open.                           |
//|                                                                  |
//| An expert is reloaded far more often than a daemon (recompile,   |
//| chart change, terminal restart), so it takes the other route: on |
//| every init it replays InpSeedBars closed bars forward from the   |
//| oldest, which is the alert tool own --backtest mode, computed    |
//| once. It is deterministic, needs no state file, and the seeding  |
//| error decays geometrically: for the 26-period EMA, alpha is      |
//| 0.074, so after 1,000 bars the residue of the seed is of order   |
//| e^-77 - many orders of magnitude below a $0.01 tick.             |
//|                                                                  |
//| ====================================================================
//| ONE KNOWN DIVERGENCE FROM THE PYTHON, STATED RATHER THAN HIDDEN
//| ====================================================================
//| In macd_crossover.py, when a protective exit fires the method    |
//| returns before _prev_histogram is assigned, so the next bar      |
//| compares against the histogram from TWO bars ago. This port      |
//| reproduces that exactly, because matching the measured backtest  |
//| matters more than tidying it here. It is flagged in mt5/README.md|
//| as something to decide about in the Python first; if that        |
//| changes, change ALGOGOLD_MATCH_PY_STOP_PREV_HISTOGRAM below.     |
//+------------------------------------------------------------------+
#property copyright "algo trading - GOLDM/XAUUSD engine"
#property link      ""
#property version   "1.00"
#property description "MACD(12,26,9) crossover on XAUUSD, ported from algo/strategy/macd_crossover.py"
#property strict

#include <AlgoGold\ProtectiveExits.mqh>
#include <AlgoGold\Trader.mqh>

//--- See the divergence note above. true = bit-match the Python.
#define ALGOGOLD_MATCH_PY_STOP_PREV_HISTOGRAM true

//+------------------------------------------------------------------+
//| Inputs. Every default is the Python default for the same name.   |
//+------------------------------------------------------------------+
input group "--- Signal (algo/strategy/macd_crossover.py) ---"
input int    InpFast              = 12;      // Fast EMA period
input int    InpSlow              = 26;      // Slow EMA period
input int    InpSignal            = 9;       // Signal EMA period
input int    InpSeedBars          = 1000;    // Closed bars replayed to seed the EMAs

input group "--- Protective exits (percent of price, NOT points) ---"
input double InpStopLossPct       = 0.5;     // Flat stop, % of entry. 0 disables
input double InpTrailActivationPct= 2.0;     // Profit % at which the trail arms
input double InpTrailPct          = 0.0;     // Trail distance, % behind peak. 0 disables

input group "--- Execution ---"
input double InpLots              = 1.00;    // Volume in MT5 LOTS (1.00 = 100 oz = Python default)
input long   InpMagic             = 20260901;// Must differ from the Python adapter 20260828
input ulong  InpSlippagePoints    = 30;      // Max deviation, points
input int    InpMaxSpreadPoints   = 0;       // Block NEW entries above this spread. 0 = off
input bool   InpAllowNewEntries   = true;    // false = manage open positions only
input string InpComment           = "AlgoGold MACD"; // Cosmetic only - MT5 overwrites it

//+------------------------------------------------------------------+
//| State                                                            |
//+------------------------------------------------------------------+
CGoldTrader      g_trader;
TrailState       g_trail;
ENUM_TIMEFRAMES  g_tf              = PERIOD_CURRENT;
datetime         g_lastBarTime     = 0;

double           g_fastEma         = 0.0;
double           g_slowEma         = 0.0;
double           g_signalEma       = 0.0;
bool             g_hasEma          = false;
double           g_prevHistogram   = 0.0;
bool             g_hasPrevHistogram= false;
int              g_barsSeen        = 0;

//+------------------------------------------------------------------+
//| Bars needed before a crossover means anything.                   |
//| algo/pricing/indicators.py warmup_bars(): slow + signal + 2.     |
//| Not a point of exactness - a recursive EMA is never exactly      |
//| settled - but the point past which the residue is below a tick.  |
//+------------------------------------------------------------------+
int WarmupBars()
  {
   return InpSlow + InpSignal + 2;
  }

//+------------------------------------------------------------------+
//| One recursive EMA step. Seeded with the raw price on the first   |
//| call, exactly as _ema_step does.                                 |
//+------------------------------------------------------------------+
double EmaStep(const double value,const double previous,const bool hasPrevious,const double alpha)
  {
   if(!hasPrevious)
      return value;
   return alpha*value + (1.0-alpha)*previous;
  }

//+------------------------------------------------------------------+
//| Feed one closed bar close into the running EMAs. Returns the new |
//| histogram. alpha = 2/(period+1), the standard EMA weight.        |
//|                                                                  |
//| The EMAs must see EVERY bar regardless of what follows - skipping |
//| the update during warmup or while a stop is being checked would  |
//| leave them permanently behind by however many bars were skipped. |
//+------------------------------------------------------------------+
double UpdateMacd(const double close)
  {
   const double fastAlpha   = 2.0/(InpFast+1.0);
   const double slowAlpha   = 2.0/(InpSlow+1.0);
   const double signalAlpha = 2.0/(InpSignal+1.0);

   const bool hadEma = g_hasEma;

   g_fastEma = EmaStep(close,g_fastEma,hadEma,fastAlpha);
   g_slowEma = EmaStep(close,g_slowEma,hadEma,slowAlpha);
   g_barsSeen++;

   const double macdValue = g_fastEma - g_slowEma;
   g_signalEma = EmaStep(macdValue,g_signalEma,hadEma,signalAlpha);
   g_hasEma = true;

   return macdValue - g_signalEma;
  }

//+------------------------------------------------------------------+
//| Replay InpSeedBars closed bars, oldest first.                     |
//+------------------------------------------------------------------+
bool SeedIndicators()
  {
   double closes[];
   const int copied = CopyClose(_Symbol,g_tf,1,InpSeedBars,closes);
   if(copied<=0)
     {
      PrintFormat("FATAL: no closed-bar history for %s %s - the terminal may still be "
                  "downloading it, or the market may be shut",
                  _Symbol,EnumToString(g_tf));
      return false;
     }

//--- CopyClose returns oldest-first, which is the order the recursion needs.
   for(int i=0; i<copied; i++)
     {
      g_prevHistogram    = UpdateMacd(closes[i]);
      g_hasPrevHistogram = true;
     }

   PrintFormat("seeded from %d closed bar(s): MACD %.5f, signal %.5f, histogram %.5f "
               "(warmup needs %d)",
               copied,g_fastEma-g_slowEma,g_signalEma,g_prevHistogram,WarmupBars());

   if(copied < WarmupBars())
      PrintFormat("WARNING: only %d bar(s) of history, below the %d-bar warmup - "
                  "no entry will be taken until enough bars have closed",
                  copied,WarmupBars());
   return true;
  }

//+------------------------------------------------------------------+
//| Init                                                             |
//+------------------------------------------------------------------+
int OnInit()
  {
   g_tf = (ENUM_TIMEFRAMES)_Period;
   TrailClear(g_trail);

   if(InpFast>=InpSlow)
     {
      PrintFormat("FATAL: the fast period must be shorter than the slow one, got %d and %d",
                  InpFast,InpSlow);
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpFast<1 || InpSignal<1)
     {
      Print("FATAL: EMA periods must be at least 1");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpSeedBars < WarmupBars())
     {
      PrintFormat("FATAL: InpSeedBars (%d) is below the %d-bar warmup; the EMAs would "
                  "never be trusted",InpSeedBars,WarmupBars());
      return INIT_PARAMETERS_INCORRECT;
     }
   if(!GoldPreflight(InpMagic,InpStopLossPct,InpTrailActivationPct,InpTrailPct))
      return INIT_PARAMETERS_INCORRECT;

   if(!g_trader.Init(_Symbol,InpMagic,InpLots,InpSlippagePoints,InpComment))
      return INIT_FAILED;

   if(!SeedIndicators())
      return INIT_FAILED;

   GoldTimeframeNote(g_tf);

//--- A reload mid-trade must not leave the position naked for a whole bar:
//--- rebuild the trail from history and put the stop back on immediately.
   const GoldPosition pos = g_trader.Snapshot();
   if(pos.exists)
     {
      RebuildTrail(g_trail,_Symbol,g_tf,pos);
      const double sl = ProtectiveStopPrice(g_trail,pos.side,pos.entry,
                                            InpStopLossPct,InpTrailActivationPct,InpTrailPct);
      g_trader.ApplyStop(sl);
      PrintFormat("adopted an existing %s position of %.2f lots at %.2f (magic %d)",
                  (pos.side==POSITION_TYPE_BUY?"BUY":"SELL"),pos.volume,pos.entry,(int)InpMagic);
     }

   g_lastBarTime = iTime(_Symbol,g_tf,0);
   PrintFormat("MACD(%d,%d,%d) on %s %s | stop %.2f%% | trail %.2f%% from %.2f%% | magic %d",
               InpFast,InpSlow,InpSignal,_Symbol,EnumToString(g_tf),
               InpStopLossPct,InpTrailPct,InpTrailActivationPct,(int)InpMagic);
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   PrintFormat("stopped (reason %d). Open positions are LEFT AS THEY ARE - removing an "
               "expert is not a flatten instruction.",reason);
  }

//+------------------------------------------------------------------+
//| Tick: nothing happens except on a bar close.                     |
//|                                                                  |
//| The Python decides on the closed bar and fills at the next price. |
//| So does this: the decision is taken the instant bar 1 becomes     |
//| final, and the market order that follows fills at the open of the |
//| bar now forming.                                                  |
//+------------------------------------------------------------------+
void OnTick()
  {
   const datetime current = iTime(_Symbol,g_tf,0);
   if(current==g_lastBarTime || current==0)
      return;
   g_lastBarTime = current;
   OnClosedBar();
  }

//+------------------------------------------------------------------+
//| The whole strategy, in the order macd_crossover.on_bar runs it.  |
//+------------------------------------------------------------------+
void OnClosedBar()
  {
   const double close = iClose(_Symbol,g_tf,1);
   const double high  = iHigh(_Symbol,g_tf,1);
   const double low   = iLow(_Symbol,g_tf,1);
   if(close<=0.0)
      return;

//--- 1. The EMAs see this bar first, unconditionally.
   const double histogram = UpdateMacd(close);

   const GoldPosition pos = g_trader.Snapshot();
   if(pos.opposing)
      Print("WARNING: opposing tickets on this symbol under our magic - both legs pay "
            "swap and spread while netting to a smaller exposure.");

//--- Keep the replayed trail in step with reality. Cheap, and it is what makes
//--- a reload, a manual partial close or a broker-side fill impossible to miss.
   if(pos.exists && (!g_trail.active || g_trail.side!=pos.side || g_trail.entry!=pos.entry))
      RebuildTrail(g_trail,_Symbol,g_tf,pos);

//--- 2. Protective exits, BEFORE the warmup gate and before any signal logic.
//---    A held position must never go unprotected because the indicator that
//---    would eventually close it opposingly has not converged yet.
   const ExitKind fired = ProtectiveExitsCheck(g_trail,pos.exists,pos.side,pos.entry,
                                               high,low,InpStopLossPct,
                                               InpTrailActivationPct,InpTrailPct);
   if(fired!=EXIT_NONE)
     {
      //--- Normally the broker-side SL placed last bar has already fired intrabar
      //--- and this branch finds nothing to do. It exists for the bar where the
      //--- stop could not be placed (freeze band, rejected modify, a position
      //--- adopted at init) - the position is closed at market instead.
      const string reason = StringFormat("%s: %.2f%% level against a %s position, entry %.2f",
                                         ExitKindName(fired),
                                         (fired==EXIT_STOP?InpStopLossPct:InpTrailPct),
                                         (pos.side==POSITION_TYPE_BUY?"BUY":"SELL"),pos.entry);
      g_trader.CloseAll(reason);
      TrailClear(g_trail);

      if(!ALGOGOLD_MATCH_PY_STOP_PREV_HISTOGRAM)
        {
         g_prevHistogram    = histogram;
         g_hasPrevHistogram = true;
        }
      return;
     }

//--- 3. Move the broker-side stop to wherever this bar left it. This is the
//---    order the Python only MODELS: it checks the bar low/high precisely
//---    because it is standing in for a real stop order firing intrabar.
   if(pos.exists)
     {
      const double sl = ProtectiveStopPrice(g_trail,pos.side,pos.entry,
                                            InpStopLossPct,InpTrailActivationPct,InpTrailPct);
      g_trader.ApplyStop(sl);
     }

//--- 4. Warmup. The histogram is still recorded so the first post-warmup bar
//---    has a real predecessor to compare against.
   if(g_barsSeen < WarmupBars())
     {
      g_prevHistogram    = histogram;
      g_hasPrevHistogram = true;
      return;
     }

   const double previous    = g_prevHistogram;
   const bool   hadPrevious = g_hasPrevHistogram;
   g_prevHistogram    = histogram;
   g_hasPrevHistogram = true;
   if(!hadPrevious)
      return;

//--- 5. The crossover rule, `<=` then `>` and `>=` then `<`, exactly as the
//---    alert tool applies it. Using `<=` rather than `<` means a histogram
//---    sitting exactly at zero and then rising counts as a crossing.
   const bool crossedUp   = (previous<=0.0 && histogram>0.0);
   const bool crossedDown = (previous>=0.0 && histogram<0.0);

//--- 6. Held: the entry signal IS the exit signal for the opposite side.
//---    Holding a long after MACD has turned bearish means holding a position
//---    the strategy own logic no longer believes in.
   if(pos.exists)
     {
      const bool wantsClose = (pos.side==POSITION_TYPE_BUY  && crossedDown) ||
                              (pos.side==POSITION_TYPE_SELL && crossedUp);
      if(!wantsClose)
         return;

      const string reason = StringFormat("macd crossover: %s cross, flattening a %s position "
                                         "of %.2f lots (hist %.4f -> %.4f)",
                                         (crossedDown?"bearish":"bullish"),
                                         (pos.side==POSITION_TYPE_BUY?"BUY":"SELL"),
                                         pos.volume,previous,histogram);
      g_trader.CloseAll(reason);
      TrailClear(g_trail);
      return;   // no reversal on the same bar - see the header note
     }

//--- 7. Flat: open on a fresh cross.
   if(!crossedUp && !crossedDown)
      return;
   if(!InpAllowNewEntries)
     {
      Print("entry suppressed: InpAllowNewEntries is false");
      return;
     }
   if(!SpreadIsAcceptable())
      return;

   const string reason = StringFormat("macd crossover: %s (hist %.4f -> %.4f)",
                                      (crossedUp?"bullish":"bearish"),previous,histogram);
   g_trader.Open(crossedUp?POSITION_TYPE_BUY:POSITION_TYPE_SELL,reason);
  }

//+------------------------------------------------------------------+
//| A live-only guard with no counterpart in the backtest.           |
//|                                                                  |
//| The backtest charges a modelled $0.29 round trip (D-121) at every |
//| instant alike. Live, the spread blows out around the 21:00        |
//| rollover and on releases, and paying it is the single largest     |
//| term in every measurement here. This blocks NEW entries when the  |
//| book is that wide - it never blocks an exit, because refusing to  |
//| leave a position because leaving is expensive is how a small loss |
//| becomes a large one.                                              |
//|                                                                  |
//| Off by default (0), because enabling it makes live diverge from   |
//| the backtest in a way the backtest cannot score.                  |
//+------------------------------------------------------------------+
bool SpreadIsAcceptable()
  {
   if(InpMaxSpreadPoints<=0)
      return true;
   const long spread = SymbolInfoInteger(_Symbol,SYMBOL_SPREAD);
   if(spread<=InpMaxSpreadPoints)
      return true;
   PrintFormat("entry suppressed: spread %d points is above the %d-point limit",
               (int)spread,InpMaxSpreadPoints);
   return false;
  }
