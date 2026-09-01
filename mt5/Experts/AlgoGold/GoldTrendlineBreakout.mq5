//+------------------------------------------------------------------+
//| GoldTrendlineBreakout.mq5                                        |
//|                                                                  |
//| A port of algo/strategy/trendline_breakout.py (strategy_id       |
//| "xauusd_trendline_breakout_v1") to a MetaTrader 5 expert advisor.|
//|                                                                  |
//| Long on a fresh `lookback`-bar high, short on a fresh            |
//| `lookback`-bar low, flat in between, with the same flat          |
//| percentage stop and optional trailing profit stop the MACD       |
//| expert uses - the identical shared code, not a second copy.      |
//|                                                                  |
//| ====================================================================
//| WHY A DONCHIAN CHANNEL IS "TREND LINE BREAKOUT"
//| ====================================================================
//| A hand-drawn trend line is not implementable without a human      |
//| judgement call about which two swing points to connect, and this  |
//| project does not invent indicators without a stated, checkable    |
//| definition. The Donchian channel - the highest high and lowest    |
//| low of the last `lookback` bars - IS that definition, formalised: |
//| it is what "price broke its recent trend line" means once you     |
//| have to write it down. It is also the literal core of the Turtle  |
//| system, so the shape has a long, checkable precedent.             |
//|                                                                  |
//| ====================================================================
//| THE CHANNEL EXCLUDES THE BAR BEING TESTED
//| ====================================================================
//| The channel is built from the `lookback` bars STRICTLY BEFORE the |
//| one being tested - here, chart shifts 2..lookback+1, never shift  |
//| 1. Including the current bar own high would compare today price   |
//| against a range that already contains it, which cannot be broken  |
//| by definition.                                                    |
//|                                                                  |
//| ====================================================================
//| NO INDICATOR STATE TO SEED
//| ====================================================================
//| An EMA is path-dependent, so the MACD expert has to replay        |
//| history on every init. A rolling max/min over the last `lookback` |
//| bars depends on ONLY those bars: a freshly loaded expert facing   |
//| the same recent history makes the same decision as one that has   |
//| been running for a month. The only path-dependent piece here is   |
//| the optional trailing stop, and that is replayed from the         |
//| position own open time rather than persisted (see RebuildTrail).  |
//|                                                                  |
//| ====================================================================
//| IT DOES NOT REVERSE IN ONE STEP
//| ====================================================================
//| A long taken on an upside breakout is closed the moment price     |
//| makes a fresh `lookback`-bar low - the same event that would open |
//| a short if the expert were flat. It closes on that bar and waits  |
//| for the next break to enter, mirroring the Python, which emits at |
//| most one signal per bar.                                          |
//+------------------------------------------------------------------+
#property copyright "algo trading - GOLDM/XAUUSD engine"
#property link      ""
#property version   "1.00"
#property description "Donchian breakout on XAUUSD, ported from algo/strategy/trendline_breakout.py"
#property strict

#include <AlgoGold\ProtectiveExits.mqh>
#include <AlgoGold\Trader.mqh>

//+------------------------------------------------------------------+
//| Inputs. Every default is the Python default for the same name.   |
//+------------------------------------------------------------------+
input group "--- Signal (algo/strategy/trendline_breakout.py) ---"
input int    InpLookback          = 20;      // Channel length, bars. Minimum 2

input group "--- Protective exits (percent of price, NOT points) ---"
input double InpStopLossPct       = 0.5;     // Flat stop, % of entry. 0 disables
input double InpTrailActivationPct= 2.0;     // Profit % at which the trail arms
input double InpTrailPct          = 0.0;     // Trail distance, % behind peak. 0 disables

input group "--- Execution ---"
input double InpLots              = 1.00;    // Volume in MT5 LOTS (1.00 = 100 oz = Python default)
input long   InpMagic             = 20260902;// Must differ from the Python adapter 20260828
input ulong  InpSlippagePoints    = 30;      // Max deviation, points
input int    InpMaxSpreadPoints   = 0;       // Block NEW entries above this spread. 0 = off
input bool   InpAllowNewEntries   = true;    // false = manage open positions only
input string InpComment           = "AlgoGold Breakout"; // Cosmetic only - MT5 overwrites it

//+------------------------------------------------------------------+
//| State                                                            |
//+------------------------------------------------------------------+
CGoldTrader      g_trader;
TrailState       g_trail;
ENUM_TIMEFRAMES  g_tf          = PERIOD_CURRENT;
datetime         g_lastBarTime = 0;

//+------------------------------------------------------------------+
//| trendline_breakout.warmup_bars(): lookback + 1. The channel needs |
//| `lookback` prior bars plus the one being tested against them.     |
//+------------------------------------------------------------------+
int WarmupBars()
  {
   return InpLookback + 1;
  }

//+------------------------------------------------------------------+
//| Init                                                             |
//+------------------------------------------------------------------+
int OnInit()
  {
   g_tf = (ENUM_TIMEFRAMES)_Period;
   TrailClear(g_trail);

   if(InpLookback<2)
     {
      PrintFormat("FATAL: lookback must be at least 2, got %d",InpLookback);
      return INIT_PARAMETERS_INCORRECT;
     }
   if(!GoldPreflight(InpMagic,InpStopLossPct,InpTrailActivationPct,InpTrailPct))
      return INIT_PARAMETERS_INCORRECT;

   if(!g_trader.Init(_Symbol,InpMagic,InpLots,InpSlippagePoints,InpComment))
      return INIT_FAILED;

   const int available = Bars(_Symbol,g_tf);
   if(available < WarmupBars()+1)
      PrintFormat("WARNING: %d bar(s) of history, below the %d needed for a %d-bar channel - "
                  "no entry will be taken until enough bars have closed",
                  available,WarmupBars()+1,InpLookback);

   GoldTimeframeNote(g_tf);

//--- A reload mid-trade must not leave the position naked for a whole bar.
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
   PrintFormat("Donchian(%d) on %s %s | stop %.2f%% | trail %.2f%% from %.2f%% | magic %d",
               InpLookback,_Symbol,EnumToString(g_tf),
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
//| The channel over the `lookback` bars STRICTLY BEFORE bar 1.      |
//+------------------------------------------------------------------+
bool ChannelFromPriorBars(double &channelHigh,double &channelLow)
  {
   double highs[], lows[];
   const int gotHighs = CopyHigh(_Symbol,g_tf,2,InpLookback,highs);
   const int gotLows  = CopyLow(_Symbol,g_tf,2,InpLookback,lows);
   if(gotHighs<InpLookback || gotLows<InpLookback)
      return false;

   channelHigh = highs[ArrayMaximum(highs)];
   channelLow  = lows[ArrayMinimum(lows)];
   return true;
  }

//+------------------------------------------------------------------+
//| The whole strategy, in the order trendline_breakout.on_bar runs.  |
//+------------------------------------------------------------------+
void OnClosedBar()
  {
   const double close = iClose(_Symbol,g_tf,1);
   const double high  = iHigh(_Symbol,g_tf,1);
   const double low   = iLow(_Symbol,g_tf,1);
   if(close<=0.0)
      return;

   const GoldPosition pos = g_trader.Snapshot();
   if(pos.opposing)
      Print("WARNING: opposing tickets on this symbol under our magic - both legs pay "
            "swap and spread while netting to a smaller exposure.");

   if(pos.exists && (!g_trail.active || g_trail.side!=pos.side || g_trail.entry!=pos.entry))
      RebuildTrail(g_trail,_Symbol,g_tf,pos);

//--- 1. Protective exits, BEFORE the warmup gate - a held position must never
//---    go unprotected because the channel has not been recomputed yet.
   const ExitKind fired = ProtectiveExitsCheck(g_trail,pos.exists,pos.side,pos.entry,
                                               high,low,InpStopLossPct,
                                               InpTrailActivationPct,InpTrailPct);
   if(fired!=EXIT_NONE)
     {
      //--- Normally the broker-side SL placed last bar has already fired
      //--- intrabar and this branch finds nothing to do. It covers the bar
      //--- where the stop could not be placed.
      const string reason = StringFormat("%s: %.2f%% level against a %s position, entry %.2f",
                                         ExitKindName(fired),
                                         (fired==EXIT_STOP?InpStopLossPct:InpTrailPct),
                                         (pos.side==POSITION_TYPE_BUY?"BUY":"SELL"),pos.entry);
      g_trader.CloseAll(reason);
      TrailClear(g_trail);
      return;
     }

//--- 2. Move the broker-side stop to wherever this bar left it.
   if(pos.exists)
     {
      const double sl = ProtectiveStopPrice(g_trail,pos.side,pos.entry,
                                            InpStopLossPct,InpTrailActivationPct,InpTrailPct);
      g_trader.ApplyStop(sl);
     }

//--- 3. Warmup.
   double channelHigh = 0.0, channelLow = 0.0;
   if(!ChannelFromPriorBars(channelHigh,channelLow))
     {
      PrintFormat("no entry: not enough closed bars for a %d-bar channel",InpLookback);
      return;
     }

   const bool brokeUp   = (close>channelHigh);
   const bool brokeDown = (close<channelLow);

//--- 4. Held: a breakout is its own exit signal for the opposite side.
   if(pos.exists)
     {
      const bool wantsClose = (pos.side==POSITION_TYPE_BUY  && brokeDown) ||
                              (pos.side==POSITION_TYPE_SELL && brokeUp);
      if(!wantsClose)
         return;

      //--- Named from the break that actually fired, not derived from the
      //--- closing side: a short is closed by a fresh HIGH, and keying the
      //--- label off the side inverted it on both directions in the Python
      //--- until that was fixed.
      const string reason = StringFormat("trendline breakout: fresh %d-bar %s, flattening a %s "
                                         "position of %.2f lots (close %.2f vs channel [%.2f, %.2f])",
                                         InpLookback,(brokeUp?"high":"low"),
                                         (pos.side==POSITION_TYPE_BUY?"BUY":"SELL"),
                                         pos.volume,close,channelLow,channelHigh);
      g_trader.CloseAll(reason);
      TrailClear(g_trail);
      return;   // no reversal on the same bar
     }

//--- 5. Flat: open on a fresh break.
   if(!brokeUp && !brokeDown)
      return;
   if(!InpAllowNewEntries)
     {
      Print("entry suppressed: InpAllowNewEntries is false");
      return;
     }
   if(!SpreadIsAcceptable())
      return;

   const string reason = brokeUp
                         ? StringFormat("trendline breakout: close %.2f above the %d-bar high %.2f",
                                        close,InpLookback,channelHigh)
                         : StringFormat("trendline breakout: close %.2f below the %d-bar low %.2f",
                                        close,InpLookback,channelLow);
   g_trader.Open(brokeUp?POSITION_TYPE_BUY:POSITION_TYPE_SELL,reason);
  }

//+------------------------------------------------------------------+
//| A live-only guard with no counterpart in the backtest.           |
//| Blocks NEW entries when the book is abnormally wide; never blocks |
//| an exit, because refusing to leave a position because leaving is  |
//| expensive is how a small loss becomes a large one.                |
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
