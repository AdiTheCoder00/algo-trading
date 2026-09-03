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
input double InpTakeProfitPct     = 0.0;     // Target, % of entry. 0 disables (no Python counterpart)
input bool   InpBracketAtEntry    = true;    // Attach SL/TP to the ENTRY order, not the next bar
//--- Point-based overrides. Non-zero WINS over the percentage above.
//---
//--- A percentage is the Python's unit and is symbol-independent, but it is a
//--- blunt instrument at these prices: 1% of FixedVol100 at 4,731 is 47 price
//--- units, which on M1 is an enormous target. Points let the distance be
//--- stated directly.
//---
//--- MIND THE SCALE. A point is SYMBOL_POINT, not a pip:
//---   XAUUSD      2 digits, point 0.01  -> 100 points = 1.00 price
//---   FixedVol100 3 digits, point 0.001 -> 100 points = 0.10 price
//---   BTCUSD      2 digits, point 0.01  -> 100 points = 1.00 price
//--- so the same number means a tenth as much on FixedVol100. The broker
//--- minimum there is 7,123 POINTS (7.123 price); anything under that is
//--- rejected, and init prints the resolved distance so it can be checked.
//---
//--- The stop is converted back into an equivalent percentage against the
//--- position's entry, because the bar-close management (ProtectiveExitsCheck /
//--- ProtectiveStopPrice) is percentage-based and would otherwise overwrite a
//--- point-based stop on the very next bar with a different level.
input int    InpStopLossPoints    = 0;       // Stop in POINTS. >0 overrides InpStopLossPct
input int    InpTakeProfitPoints  = 0;       // Target in POINTS. >0 overrides InpTakeProfitPct
//--- Target as a MONEY amount, in account currency. Highest precedence of all.
//---
//--- Precedence: money > points > percent. The first non-zero one wins, so the
//--- others can be left set as fallbacks without ambiguity.
//---
//--- The conversion depends on VOLUME, which the other two do not:
//---     money per 1.0 price move = (tick_value / tick_size) * lots
//---     distance                 = InpTakeProfitTargetMoney / that
//--- so changing InpLots silently changes the price distance this produces.
//--- That is inherent to asking for a money target, not a flaw - but it means
//--- the resolved distance has to be read from the init banner rather than
//--- assumed, and re-read after any lot change.
//---
//--- Worked example on FixedVol100 at 1.2 lots: tick_value 0.001 and tick_size
//--- 0.001 give 1.0 per price per lot, so 1.2 per price. A $20 target is a
//--- 16.67 price move. On XAUUSD at 0.05 lots it is (1.0/0.01)*0.05 = 5.0 per
//--- price, so the same $20 is a 4.00 move. Same input, very different trade.
input double InpStopLossMoney     = 0.0;     // Stop as LOSS in account currency. >0 overrides points and percent
input double InpTakeProfitMoney   = 0.0;     // Target as PROFIT in account currency. >0 overrides both above

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
//| The stop as a PERCENTAGE, whichever way it was configured.        |
//|                                                                   |
//| One source of truth. Everything downstream - the bar-close exit    |
//| check, the broker-side stop level, the log line - is percentage-   |
//| based, so a point setting is resolved against the reference price  |
//| once and then behaves identically. Without this the entry would    |
//| use points and the next bar would overwrite it with the percent.   |
//+------------------------------------------------------------------+
//+------------------------------------------------------------------+
//| Money moved per 1.0 of PRICE, at the configured volume.           |
//|                                                                   |
//| Via tick_value/tick_size rather than contract size, because tick   |
//| value is already denominated in the ACCOUNT currency - the         |
//| contract-size route is right only while quote and account currency |
//| coincide. Returns 0 when the symbol reports nothing usable, and    |
//| every caller treats that as "fall through to the next unit"        |
//| rather than inventing a distance.                                  |
//+------------------------------------------------------------------+
double MoneyPerPrice()
  {
   const double tickValue = SymbolInfoDouble(_Symbol,SYMBOL_TRADE_TICK_VALUE);
   const double tickSize  = SymbolInfoDouble(_Symbol,SYMBOL_TRADE_TICK_SIZE);
   const double lots      = g_trader.Lots();
   if(tickValue<=0.0 || tickSize<=0.0 || lots<=0.0)
      return 0.0;
   return (tickValue/tickSize)*lots;
  }

double EffectiveStopPct(const double refPrice)
  {
   if(InpStopLossMoney > 0.0 && refPrice > 0.0)
     {
      const double perPrice = MoneyPerPrice();
      if(perPrice > 0.0)
         return (InpStopLossMoney/perPrice)/refPrice*100.0;
      Print("WARNING: InpStopLossMoney set but the symbol reports no usable tick "
            "value - falling back to points/percent");
     }
   if(InpStopLossPoints > 0 && refPrice > 0.0)
      return InpStopLossPoints*_Point/refPrice*100.0;
   return InpStopLossPct;
  }

//+------------------------------------------------------------------+
//| Target distance in PRICE. Points win over percent when set.       |
//| The target is attached once at entry and never re-derived, so it   |
//| needs no percentage round trip.                                    |
//+------------------------------------------------------------------+
double TakeDistancePrice(const double refPrice)
  {
   if(InpTakeProfitMoney > 0.0)
     {
      const double perPrice = MoneyPerPrice();
      if(perPrice > 0.0)
         return InpTakeProfitMoney/perPrice;
      Print("WARNING: InpTakeProfitMoney set but the symbol reports no usable tick "
            "value - falling back to points/percent");
     }
   if(InpTakeProfitPoints > 0)
      return InpTakeProfitPoints*_Point;
   if(InpTakeProfitPct > 0.0 && refPrice > 0.0)
      return refPrice*InpTakeProfitPct/100.0;
   return 0.0;
  }

//+------------------------------------------------------------------+
//| Which unit is actually in force, for the init banner.             |
//+------------------------------------------------------------------+
string StopUnitName()
  {
   if(InpStopLossMoney  > 0.0) return StringFormat("%.2f money",InpStopLossMoney);
   if(InpStopLossPoints > 0)   return StringFormat("%d points",InpStopLossPoints);
   return StringFormat("%.2f%%",InpStopLossPct);
  }
string TakeUnitName()
  {
   if(InpTakeProfitMoney  > 0.0) return StringFormat("%.2f money",InpTakeProfitMoney);
   if(InpTakeProfitPoints > 0)   return StringFormat("%d points",InpTakeProfitPoints);
   return StringFormat("%.2f%%",InpTakeProfitPct);
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
   if(InpTakeProfitPct<0.0)
     {
      Print("FATAL: InpTakeProfitPct cannot be negative");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpStopLossMoney<0.0 || InpTakeProfitMoney<0.0)
     {
      Print("FATAL: money stop/target cannot be negative");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpStopLossPoints<0 || InpTakeProfitPoints<0)
     {
      Print("FATAL: point stop/target cannot be negative");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(!GoldPreflight(InpMagic,InpStopLossPct,InpTrailActivationPct,InpTrailPct))
      return INIT_PARAMETERS_INCORRECT;

   if(!g_trader.Init(_Symbol,InpMagic,InpLots,InpSlippagePoints,InpComment))
      return INIT_FAILED;

//--- A percentage stop resolves to a different PRICE distance on every symbol,
//--- and the broker refuses anything inside SYMBOL_TRADE_STOPS_LEVEL. Measured
//--- on this account: XAUUSD 0.20, BTCUSD 0.00, FixedVol100 7.123. A 0.5% stop
//--- is comfortable on all three, but a 0.1% one is illegal on FixedVol100 -
//--- and an expert configured that way sends orders that are ALL rejected while
//--- looking perfectly healthy (D-141). Warned rather than refused, because the
//--- distance moves with price and a level that is legal now may not be later.
     {
      const int    digits = (int)SymbolInfoInteger(_Symbol,SYMBOL_DIGITS);
      const double point  = SymbolInfoDouble(_Symbol,SYMBOL_POINT);
      const double stops  = (double)SymbolInfoInteger(_Symbol,SYMBOL_TRADE_STOPS_LEVEL)*point;
      const double price  = SymbolInfoDouble(_Symbol,SYMBOL_BID);
      const double slPct  = EffectiveStopPct(price);
      const double slNow  = (slPct>0.0) ? price*slPct/100.0 : 0.0;
      const double tpNow  = TakeDistancePrice(price);
      PrintFormat("bracket at %.2f: stop %.*f (%s), target %.*f (%s), broker minimum %.*f (%d points)",
                  price,
                  digits,slNow,StopUnitName(),
                  digits,tpNow,TakeUnitName(),
                  digits,stops,(int)SymbolInfoInteger(_Symbol,SYMBOL_TRADE_STOPS_LEVEL));
      PrintFormat("  1 point = %.*f price here, so the broker minimum is %d points",
                  digits,point,(int)SymbolInfoInteger(_Symbol,SYMBOL_TRADE_STOPS_LEVEL));
//--- With a money target the price distance depends on VOLUME, so state what
//--- the configured lots actually buy - and what the levels are worth in money
//--- whichever unit was used, since that is the number being reasoned about.
      const double perPrice = MoneyPerPrice();
      if(perPrice > 0.0)
        {
         PrintFormat("  at %.2f lots, 1.0 of price = %.2f in account currency",
                     g_trader.Lots(),perPrice);
         PrintFormat("  so this bracket risks %.2f to make %.2f (%.2f:1)",
                     slNow*perPrice,tpNow*perPrice,
                     (slNow>0.0 ? tpNow/slNow : 0.0));
        }
      else
         Print("  WARNING: symbol reports no usable tick value - money-based "
               "stop/target cannot be converted and will fall back to points/percent");
      const int minPoints = (int)SymbolInfoInteger(_Symbol,SYMBOL_TRADE_STOPS_LEVEL);
      if(stops>0.0 && slNow>0.0 && slNow<stops)
         PrintFormat("WARNING: the stop resolves to %.*f here, INSIDE the broker's %.*f "
                     "minimum. Entries will be REJECTED. Raise it to at least %d points "
                     "(or %.3f%%).",digits,slNow,digits,stops,minPoints,stops/price*100.0);
      if(stops>0.0 && tpNow>0.0 && tpNow<stops)
         PrintFormat("WARNING: the target resolves to %.*f here, INSIDE the broker's %.*f "
                     "minimum. Entries will be REJECTED. Raise it to at least %d points "
                     "(or %.3f%%).",digits,tpNow,digits,stops,minPoints,stops/price*100.0);
      if(!InpBracketAtEntry)
         Print("WARNING: InpBracketAtEntry is false - positions are opened with no stop and "
               "stay naked until the next closed bar applies one.");
     }

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
                                            EffectiveStopPct(pos.entry),InpTrailActivationPct,InpTrailPct);
      g_trader.ApplyStop(sl);
      PrintFormat("adopted an existing %s position of %.2f lots at %.2f (magic %d)",
                  (pos.side==POSITION_TYPE_BUY?"BUY":"SELL"),pos.volume,pos.entry,(int)InpMagic);
     }

   g_lastBarTime = iTime(_Symbol,g_tf,0);
   PrintFormat("Donchian(%d) on %s %s | stop %.2f%% | trail %.2f%% from %.2f%% | magic %d",
               InpLookback,_Symbol,EnumToString(g_tf),
               EffectiveStopPct(SymbolInfoDouble(_Symbol,SYMBOL_BID)),
               InpTrailPct,InpTrailActivationPct,(int)InpMagic);
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
                                               high,low,EffectiveStopPct(pos.entry),
                                               InpTrailActivationPct,InpTrailPct);
   if(fired!=EXIT_NONE)
     {
      //--- Normally the broker-side SL placed last bar has already fired
      //--- intrabar and this branch finds nothing to do. It covers the bar
      //--- where the stop could not be placed.
      const string reason = StringFormat("%s: %.2f%% level against a %s position, entry %.2f",
                                         ExitKindName(fired),
                                         (fired==EXIT_STOP?EffectiveStopPct(pos.entry):InpTrailPct),
                                         (pos.side==POSITION_TYPE_BUY?"BUY":"SELL"),pos.entry);
      g_trader.CloseAll(reason);
      TrailClear(g_trail);
      return;
     }

//--- 2. Move the broker-side stop to wherever this bar left it.
   if(pos.exists)
     {
      const double sl = ProtectiveStopPrice(g_trail,pos.side,pos.entry,
                                            EffectiveStopPct(pos.entry),InpTrailActivationPct,InpTrailPct);
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
   const ENUM_POSITION_TYPE side = brokeUp ? POSITION_TYPE_BUY : POSITION_TYPE_SELL;

//--- 6. Send it, bracketed.
//---
//---    Open() sends sl=0 tp=0 and the stop is only applied on the NEXT closed
//---    bar by ApplyStop. That leaves every position naked for a full bar -
//---    an hour on H1 - and it is not theoretical: a live FixedVol100 position
//---    under this magic was observed sitting with sl=0.0.
//---
//---    Attaching the stop to the entry order is also MORE faithful to the
//---    Python, not less. price_stop.py checks the bar's low/high rather than
//---    its close precisely because it is standing in for a broker-side stop
//---    firing intrabar; a stop that does not exist until the next bar cannot
//---    do that. The bar-close check in step 1 stays as the backstop for the
//---    bar where the broker refuses the level.
   if(!InpBracketAtEntry)
     {
      g_trader.Open(side,reason);
      return;
     }

   const double ref = (side==POSITION_TYPE_BUY)
                      ? SymbolInfoDouble(_Symbol,SYMBOL_ASK)
                      : SymbolInfoDouble(_Symbol,SYMBOL_BID);
   if(ref<=0.0)
     {
      Print("entry suppressed: no two-sided quote to price the bracket against");
      return;
     }
   const double slPct      = EffectiveStopPct(ref);
   const double slDistance = (slPct>0.0) ? ref*slPct/100.0 : 0.0;
   const double tpDistance = TakeDistancePrice(ref);
   g_trader.OpenBracket(side,g_trader.Lots(),slDistance,tpDistance,reason);
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
