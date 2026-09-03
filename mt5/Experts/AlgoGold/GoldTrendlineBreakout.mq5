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
#include <AlgoGold\Dashboard.mqh>

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
input int    InpStopLossPoints    = 0;       // Stop in POINTS. >0 overrides InpStopLossPct. 0 = use money below
input int    InpTakeProfitPoints  = 0;       // Target in POINTS. >0 overrides InpTakeProfitPct. 0 = use money below
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
input double InpStopLossMoney     = 0.0;     // Stop as LOSS in account currency. 0 = fall back to points/percent
input double InpTakeProfitMoney   = 0.0;     // Target as PROFIT in account currency. 0 = fall back to points/percent
//--- MONEY-BASED TRAILING STOP.
//---
//--- Arm the trail once open profit reaches InpTrailActivationMoney, then keep
//--- the stop InpTrailMoney behind the best price seen. Both are in account
//--- currency and both override the percentage equivalents above.
//---
//--- These convert to percentages against different references, which is not
//--- arbitrary: ProtectiveExits measures activation from the ENTRY (how far
//--- the position has travelled) and the give-back from the PEAK (how much of
//--- the best price is surrendered). Converting both against entry would make
//--- the give-back drift as the peak advanced.
//---
//--- The cost-to-cost clamp in TrailLevel still applies: an armed trail can
//--- never sit worse than entry, so its floor is a scratch, not a loss.
//---
//--- NO PYTHON COUNTERPART. trailing_profit_stop.py is percentage-based; a
//--- money trail is a live-only divergence the backtest has never scored.
input double InpTrailActivationMoney = 5.0;  // Arm the trail at this PROFIT in account currency. 0 = use percent
input double InpTrailMoney           = 1.0;  // Trail this many account-currency units behind the peak. 0 = use percent

//+------------------------------------------------------------------+
//| SALVAGE EXIT - take the first profit on a trade that started badly|
//|                                                                   |
//| If the position goes adverse within the first InpSalvageWindowMin |
//| minutes, it is MARKED. A marked position is then closed at market |
//| the moment its floating profit reaches InpSalvageExitProfit,      |
//| instead of waiting for the target.                                |
//|                                                                   |
//| ====================================================================
//| "IN LOSS" IS MEASURED AGAINST A THRESHOLD, NOT AGAINST ENTRY
//| ====================================================================
//| Every position is in loss the instant it opens: a buy fills at the|
//| ask and is marked against the bid, so it starts down by the spread|
//| before the market has done anything. Marking on "price went below |
//| entry" would therefore mark EVERY trade and the rule would just   |
//| mean "always exit at the first profit".                           |
//|                                                                   |
//| So the trigger is a real adverse excursion, measured in money and |
//| required to exceed InpSalvageLossMoney. Left at 0 it defaults to  |
//| one spread, which is the smallest move that is not just the cost  |
//| of having opened.                                                 |
//|                                                                   |
//| ====================================================================
//| MARKED IS REPLAYED, NOT REMEMBERED
//| ====================================================================
//| The adverse excursion is recomputed from the bars since entry, the|
//| same way RebuildTrail works, so a recompile or restart cannot lose|
//| the mark. It is cached per ticket so the replay runs once a bar    |
//| rather than once a tick.                                          |
//|                                                                   |
//| The PROFIT test runs every tick, because "close at the first      |
//| profit" is worth little if it only looks once a minute.           |
//|                                                                   |
//| NO PYTHON COUNTERPART - trendline_breakout.py has no such rule, so|
//| enabling this makes live behaviour something no backtest scored.  |
//+------------------------------------------------------------------+
input group "--- Salvage exit (no Python counterpart) ---"
input bool   InpSalvageEnabled    = true;    // Close a recovered trade at the first profit
input int    InpSalvageWindowMin  = 5;       // Minutes after entry in which the adverse move must happen
input double InpSalvageLossMoney  = 0.0;     // Must have been down at least this much. 0 = one spread
input double InpSalvageExitProfit = 0.0;     // Close once profit reaches this. 0 = any profit above zero

//+------------------------------------------------------------------+
//| SCALE-IN - add to a losing position, same side, up to a limit.    |
//|                                                                   |
//| When the netted position is InpScaleInLossMoney down, open another|
//| position on the SAME side. When it is twice that down, open a     |
//| third, and so on to InpScaleInMaxTrades in total.                 |
//|                                                                   |
//| ====================================================================
//| WHAT THIS IS, STATED PLAINLY
//| ====================================================================
//| This is averaging into a loser. It is the same family as the grid |
//| and martingale EAs D-141 disqualified - with one important        |
//| difference: the size does NOT escalate. Every add is InpLots, so  |
//| total exposure is bounded at InpScaleInMaxTrades * InpLots and the|
//| worst case is a known number, not an open-ended one.              |
//|                                                                   |
//| At 1.1 lots on FixedVol100 with a 9.26 stop, five positions risk  |
//| about $51 against a ~$1,050 account - roughly 4.8%. That is the   |
//| number to decide about.                                           |
//|                                                                   |
//| ====================================================================
//| THE ADDS MOVE THE SHARED STOP
//| ====================================================================
//| Snapshot() nets every ticket into one volume-weighted position and|
//| ApplyStop puts ONE level on all of them. So each add pulls the    |
//| average entry toward price and the shared stop moves with it -    |
//| the first ticket's stop WIDENS as you add. That is coherent for an|
//| averaging strategy (you are betting on the mean), but it means the|
//| risk is not simply "five independent trades": they live and die   |
//| together on one level.                                            |
//|                                                                   |
//| ====================================================================
//| THE LADDER IS CUMULATIVE, WHICH IS WHAT STOPS IT SPAMMING
//| ====================================================================
//| Add number k fires at k * InpScaleInLossMoney of total floating   |
//| loss. So -5 opens the 2nd, -10 the 3rd, -15 the 4th, -20 the 5th. |
//| Because the count rises with each add, the next threshold is      |
//| immediately further away and one tick cannot trigger two adds.    |
//|                                                                   |
//| NO PYTHON COUNTERPART, and nothing here has ever backtested it.   |
//+------------------------------------------------------------------+
input group "--- Scale-in on loss (no Python counterpart) ---"
input bool   InpScaleInEnabled    = true;    // Add to a losing position, same side
input double InpScaleInLossMoney  = 5.0;     // Each add fires at another this-much of loss
input int    InpScaleInMaxTrades  = 5;       // Total positions including the first
//--- Size multiplier per add. 1.0 = flat averaging; anything above 1.0 is a
//--- MARTINGALE, and the distinction is not cosmetic.
//---
//--- With 1.25 and five positions the ladder is base x 1, 1.25, 1.5625, 1.9531,
//--- 2.4414 - a total of 8.21x the base lot rather than 5x. On FixedVol100 at
//--- 1.1 base that rounds to 1.1 / 1.4 / 1.7 / 2.1 / 2.7 = 9.0 lots, and a full
//--- stop-out costs about $83 instead of $51. Roughly 8% of a $1,050 account
//--- against 4.8%.
//---
//--- It is still BOUNDED - the ladder ends at InpScaleInMaxTrades and every
//--- position keeps its own bracket - so this is a martingale with a floor, not
//--- the open-ended kind that ends accounts. The floor is the max-trades cap;
//--- raising that is what would make it dangerous.
//---
//--- Each size is normalised to the symbol's volume step, so the actual ladder
//--- is printed on init rather than assumed from the multiplier.
input double InpScaleInLotMult    = 1.25;    // Multiply the lot by this on each add. 1.0 = flat
//--- BASKET TAKE PROFIT - close everything once the combined position is up.
//---
//--- This is the exit that makes scale-in coherent: the adds pull the average
//--- entry toward price, so the basket needs a smaller recovery to reach profit
//--- than the first ticket alone would. Without it the adds have no plan.
//---
//--- Measured on the COMBINED floating P&L of every ticket under our magic, not
//--- per trade. With five positions open, $2 means the basket as a whole is $2
//--- up - roughly $0.40 a trade - not $2 each. Set it to 10.00 if you want the
//--- latter.
//---
//--- ORDER MATTERS AGAINST SALVAGE. A marked position closes at the first
//--- profit above zero, which is a LOWER bar than this, so on a marked basket
//--- salvage fires first and this never gets the chance. That is deliberate -
//--- the tighter exit wins - but it means the two rules together behave like
//--- "exit at breakeven if it started badly, otherwise at InpBasketTakeMoney".
input double InpBasketTakeMoney   = 2.0;     // Close ALL positions at this combined profit. 0 = off

input group "--- Dashboard ---"
input bool   InpShowDashboard    = true;    // On-chart status panel
input int    InpDashX            = 12;      // Panel X offset, pixels
input int    InpDashY            = 18;      // Panel Y offset, pixels

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
//--- Salvage state, cached per position so the bar replay runs once a bar
//--- rather than once a tick. Keyed on entry+side, which is enough: this
//--- expert never holds two positions and never reverses without a flat bar.
double           g_salvageEntry  = 0.0;
int              g_salvageSide   = -1;
bool             g_salvageMarked = false;
CGoldDashboard   g_dash;
datetime         g_lastDashPaint = 0;
double           g_chanHigh      = 0.0;
double           g_chanLow       = 0.0;

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
//| SALVAGE: was this position adverse enough, early enough?          |
//|                                                                   |
//| Replayed from the bars covering the first InpSalvageWindowMin      |
//| minutes after entry. Returns the worst adverse excursion in MONEY,|
//| 0.0 when there was none.                                          |
//+------------------------------------------------------------------+
double EarlyAdverseMoney(const GoldPosition &pos)
  {
   if(!pos.exists || pos.entry<=0.0)
      return 0.0;
   const int firstBar = iBarShift(_Symbol,g_tf,pos.openTime,false);
   if(firstBar<0)
      return 0.0;
//--- how many bars cover the window; at least the entry bar itself
   const int perBar = (int)PeriodSeconds(g_tf);
   int windowBars = (perBar>0) ? (int)MathCeil(InpSalvageWindowMin*60.0/perBar) : 1;
   if(windowBars<1)
      windowBars = 1;
//--- shifts firstBar (entry bar) down to firstBar-windowBars+1, never past 1
   const int lastShift = MathMax(1, firstBar-windowBars+1);
   double worst = 0.0;
   for(int shift=firstBar; shift>=lastShift; shift--)
     {
      const double adverse = (pos.side==POSITION_TYPE_BUY)
                             ? (pos.entry - iLow(_Symbol,g_tf,shift))
                             : (iHigh(_Symbol,g_tf,shift) - pos.entry);
      if(adverse>worst)
         worst = adverse;
     }
   if(worst<=0.0)
      return 0.0;
   const double perPrice = MoneyPerPrice();
   return (perPrice>0.0) ? worst*perPrice : 0.0;
  }

//+------------------------------------------------------------------+
//| The adverse move that counts as "started badly".                  |
//| 0 means one spread - the smallest move that is not just the cost  |
//| of having opened the position.                                    |
//+------------------------------------------------------------------+
double SalvageLossTrigger()
  {
   if(InpSalvageLossMoney>0.0)
      return InpSalvageLossMoney;
   const double spread   = (double)SymbolInfoInteger(_Symbol,SYMBOL_SPREAD)*SymbolInfoDouble(_Symbol,SYMBOL_POINT);
   const double perPrice = MoneyPerPrice();
   return (spread>0.0 && perPrice>0.0) ? spread*perPrice : 0.0;
  }

//+------------------------------------------------------------------+
//| Trail activation as a PERCENTAGE, measured from ENTRY.            |
//|                                                                   |
//| TrailIsArmed compares against TrailFavourableMovePct, which is    |
//| (peak - entry) / entry, so the money amount is divided by entry.  |
//+------------------------------------------------------------------+
double EffectiveTrailActivationPct(const double entry)
  {
   if(InpTrailActivationMoney > 0.0 && entry > 0.0)
     {
      const double perPrice = MoneyPerPrice();
      if(perPrice > 0.0)
         return (InpTrailActivationMoney/perPrice)/entry*100.0;
     }
   return InpTrailActivationPct;
  }

//+------------------------------------------------------------------+
//| Trail distance as a PERCENTAGE, measured from the PEAK.           |
//|                                                                   |
//| TrailLevel computes give-back as peak * trailPct / 100, so to     |
//| surrender a fixed money amount the percentage must be taken       |
//| against the peak - not the entry. Using entry here would make the |
//| give-back grow as the peak advanced, which is the opposite of a   |
//| fixed-money trail.                                                |
//|                                                                   |
//| Falls back to entry when there is no peak yet (trail not started).|
//+------------------------------------------------------------------+
double EffectiveTrailPct(const double peakOrEntry)
  {
   if(InpTrailMoney > 0.0 && peakOrEntry > 0.0)
     {
      const double perPrice = MoneyPerPrice();
      if(perPrice > 0.0)
         return (InpTrailMoney/perPrice)/peakOrEntry*100.0;
     }
   return InpTrailPct;
  }

//+------------------------------------------------------------------+
//| The peak to measure the trail give-back against.                  |
//+------------------------------------------------------------------+
double TrailReference(const double entry)
  {
   return (g_trail.active && g_trail.peak > 0.0) ? g_trail.peak : entry;
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

//--- The trail, in whatever unit it is configured in, with the resolved
//--- distances so the money case can be checked rather than assumed.
      if(InpTrailMoney > 0.0 || InpTrailPct > 0.0)
        {
         if(InpTrailMoney > 0.0 && perPrice > 0.0)
            PrintFormat("  trail: arms at %.2f profit, then keeps the stop %.2f behind the "
                        "peak (= %.*f and %.*f in price at %.2f lots)",
                        InpTrailActivationMoney,InpTrailMoney,
                        digits,InpTrailActivationMoney/perPrice,
                        digits,InpTrailMoney/perPrice,g_trader.Lots());
         else
            PrintFormat("  trail: arms at %.2f%% profit, then %.2f%% behind the peak",
                        InpTrailActivationPct,InpTrailPct);
         Print("  the trail can never sit worse than entry (cost-to-cost clamp), so its "
               "floor is a scratch rather than a loss");
        }
      else
         Print("  trail: OFF (both InpTrailMoney and InpTrailPct are 0)");

//--- Salvage, with the resolved trigger so "0 = one spread" is a number
//--- rather than a promise.
      if(InpSalvageEnabled)
        {
         const double trig = SalvageLossTrigger();
         PrintFormat("  salvage: ON - if the trade is %.2f down within its first %d minute(s), "
                     "close it at the first %s",
                     trig,InpSalvageWindowMin,
                     (InpSalvageExitProfit>0.0
                      ? StringFormat("%.2f of profit",InpSalvageExitProfit)
                      : "profit above zero"));
         if(trig<=0.0)
            Print("  WARNING: salvage trigger resolved to 0 - no position can ever be marked. "
                  "Set InpSalvageLossMoney explicitly.");
        }
      else
         Print("  salvage: OFF");

//--- Scale-in and the basket exit, with the worst case spelled out. The
//--- exposure question is the one worth answering before it runs, not after.
      if(InpScaleInEnabled && InpScaleInMaxTrades>1)
        {
         PrintFormat("  scale-in: ON - same side, at every %.2f of loss, up to %d position(s), "
                     "lot x%.2f each time",
                     InpScaleInLossMoney,InpScaleInMaxTrades,InpScaleInLotMult);
//--- Walk the actual ladder rather than describing it: each rung is normalised
//--- to the volume step, so the multiplier alone does not tell you the sizes.
         string ladder = "";
         double totalLots = 0.0;
         for(int k=0; k<InpScaleInMaxTrades; k++)
           {
            const double rung = g_trader.NormaliseVolume(
                                   g_trader.Lots()*MathPow(MathMax(InpScaleInLotMult,1.0),k));
            totalLots += rung;
            ladder += StringFormat("%s%.2f",(k>0?" + ":""),rung);
           }
         PrintFormat("  ladder: %s = %.2f lots total",ladder,totalLots);
         if(perPrice>0.0 && slNow>0.0 && g_trader.Lots()>0.0)
           {
            //--- perPrice is money-per-price at the BASE lot, so scale it by the
            //--- ladder's total volume to get the whole book's exposure.
            const double perPriceAll = perPrice*(totalLots/g_trader.Lots());
            PrintFormat("  WORST CASE: all %d stopped at %.*f = about %.2f, before slippage",
                        InpScaleInMaxTrades,digits,slNow,slNow*perPriceAll);
           }
        }
      else
         Print("  scale-in: OFF");

      if(InpBasketTakeMoney>0.0)
         PrintFormat("  basket exit: close ALL tickets at %.2f COMBINED profit "
                     "(with %d positions that is %.2f each)",
                     InpBasketTakeMoney,InpScaleInMaxTrades,
                     InpBasketTakeMoney/MathMax(InpScaleInMaxTrades,1));
      else
         Print("  basket exit: OFF");
      if(InpBasketTakeMoney>0.0 && InpSalvageEnabled && InpSalvageExitProfit<InpBasketTakeMoney)
         Print("  NOTE: salvage exits at a LOWER profit than the basket target, so on a "
               "marked position salvage fires first and the basket target is never reached");
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
                                            EffectiveStopPct(pos.entry),
                                            EffectiveTrailActivationPct(pos.entry),
                                            EffectiveTrailPct(TrailReference(pos.entry)));
      g_trader.ApplyStop(sl);
      PrintFormat("adopted an existing %s position of %.2f lots at %.2f (magic %d)",
                  (pos.side==POSITION_TYPE_BUY?"BUY":"SELL"),pos.volume,pos.entry,(int)InpMagic);
     }

   if(InpShowDashboard)
      g_dash.Create("AGBrk_",StringFormat("ALGOGOLD BREAKOUT  (%d)",(int)InpMagic),
                    InpDashX,InpDashY);

   g_lastBarTime = iTime(_Symbol,g_tf,0);
   PrintFormat("Donchian(%d) on %s %s | stop %.2f%% | trail %.2f%% from %.2f%% | magic %d",
               InpLookback,_Symbol,EnumToString(g_tf),
               EffectiveStopPct(SymbolInfoDouble(_Symbol,SYMBOL_BID)),
               EffectiveTrailPct(SymbolInfoDouble(_Symbol,SYMBOL_BID)),
               EffectiveTrailActivationPct(SymbolInfoDouble(_Symbol,SYMBOL_BID)),
               (int)InpMagic);
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   g_dash.Destroy();
   PrintFormat("stopped (reason %d). Open positions are LEFT AS THEY ARE - removing an "
               "expert is not a flatten instruction.",reason);
  }

//+------------------------------------------------------------------+
//| Tick: nothing happens except on a bar close.                     |
//+------------------------------------------------------------------+
void OnTick()
  {
//--- Salvage runs on the TICK, not the bar. "Close at the first profit" is
//--- worth little if it only looks once a minute; the whole point is to take
//--- the recovery when it appears. The expensive part (replaying the bars to
//--- decide whether the position was marked) is cached and refreshed on the
//--- bar boundary below, so this path is only a P&L read.
//--- Basket take profit FIRST: it closes everything, so there is no point
//--- adding to a position on the same tick that the basket is done.
   if(InpBasketTakeMoney>0.0 && CheckBasketTakeProfit())
      return;
   if(InpSalvageEnabled)
      CheckSalvageExit();
//--- Scale-in also runs on the tick: the trigger is a floating-loss level, and
//--- waiting for a bar close would add at a worse price than the level asked for.
   if(InpScaleInEnabled)
      CheckScaleIn();

   PaintDashboard();

   const datetime current = iTime(_Symbol,g_tf,0);
   if(current==g_lastBarTime || current==0)
      return;
   g_lastBarTime = current;
   OnClosedBar();
  }

//+------------------------------------------------------------------+
//| Realised P&L for our magic since a given time.                    |
//+------------------------------------------------------------------+
double RealisedSince(const datetime from)
  {
   if(!HistorySelect(from,TimeCurrent()+86400))
      return 0.0;
   double sum = 0.0;
   const int total = HistoryDealsTotal();
   for(int i=0; i<total; i++)
     {
      const ulong t = HistoryDealGetTicket(i);
      if(t==0)
         continue;
      if(HistoryDealGetString(t,DEAL_SYMBOL)!=_Symbol)
         continue;
      if(HistoryDealGetInteger(t,DEAL_MAGIC)!=InpMagic)
         continue;
      sum += HistoryDealGetDouble(t,DEAL_PROFIT)
             + HistoryDealGetDouble(t,DEAL_SWAP)
             + HistoryDealGetDouble(t,DEAL_COMMISSION);
     }
   return sum;
  }

//+------------------------------------------------------------------+
//| Repaint the panel. Throttled to once a second - chart objects are |
//| not free and nothing here changes faster than a human can read.   |
//+------------------------------------------------------------------+
void PaintDashboard(void)
  {
   if(!g_dash.Active())
      return;
   const datetime now = TimeCurrent();
   if(now==g_lastDashPaint)
      return;
   g_lastDashPaint = now;

//--- Rebuild anything deleted by hand. Deleting a panel object is easy to do by
//--- accident (Object List, or Ctrl+A on the chart), and a panel that stays half
//--- gone until the expert is re-attached reads as a bug.
   g_dash.Refresh(StringFormat("ALGOGOLD BREAKOUT  (%d)",(int)InpMagic));

   const int    digits = (int)SymbolInfoInteger(_Symbol,SYMBOL_DIGITS);
   const double bid    = SymbolInfoDouble(_Symbol,SYMBOL_BID);
   const GoldPosition pos = g_trader.Snapshot();

   const color cOk = C'120,220,140', cBad = C'240,110,110', cDim = C'150,160,180', cHot = C'255,200,90';

   int r = 0;
   g_dash.Set(r++,"SYMBOL / TF",
              StringFormat("%s  %s",_Symbol,StringSubstr(EnumToString(g_tf),7)),clrWhite);

   const bool canTrade = (bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)
                         && (bool)MQLInfoInteger(MQL_TRADE_ALLOWED);
   g_dash.Set(r++,"STATUS",
              (canTrade ? (InpAllowNewEntries ? "TRADING" : "MANAGE ONLY") : "ALGO OFF"),
              (canTrade && InpAllowNewEntries) ? cOk : cHot);

//--- The channel is the signal. Nothing else in MT5 shows it.
   if(g_chanHigh>0.0)
     {
      g_dash.Set(r++,"CHANNEL HI",StringFormat("%.*f  (%+.*f)",digits,g_chanHigh,
                                               digits,g_chanHigh-bid),cDim);
      g_dash.Set(r++,"CHANNEL LO",StringFormat("%.*f  (%+.*f)",digits,g_chanLow,
                                               digits,g_chanLow-bid),cDim);
     }
   else
      g_dash.Set(r++,"CHANNEL","warming up",cDim);

   g_dash.Set(r++,"SPREAD",StringFormat("%d pts",(int)SymbolInfoInteger(_Symbol,SYMBOL_SPREAD)),cDim);

   if(pos.exists)
     {
      double floating = 0.0;
      for(int i=PositionsTotal()-1; i>=0; i--)
        {
         if(PositionGetSymbol(i)!=_Symbol) continue;
         if(PositionGetInteger(POSITION_MAGIC)!=InpMagic) continue;
         floating += PositionGetDouble(POSITION_PROFIT)+PositionGetDouble(POSITION_SWAP);
        }
      g_dash.Set(r++,"POSITION",
                 StringFormat("%s %.2f @ %.*f",(pos.side==POSITION_TYPE_BUY?"BUY":"SELL"),
                              pos.volume,digits,pos.entry),
                 (pos.side==POSITION_TYPE_BUY?cOk:cBad));
      g_dash.Set(r++,"TICKETS",StringFormat("%d",pos.tickets),cDim);
      g_dash.Set(r++,"FLOATING",StringFormat("%+.2f",floating),(floating>=0?cOk:cBad));
      //--- Trail and salvage state exist nowhere else in the terminal.
      const bool armed = TrailIsArmed(g_trail,EffectiveTrailActivationPct(pos.entry));
      g_dash.Set(r++,"TRAIL",(armed?"ARMED":"not armed"),(armed?cOk:cDim));
      g_dash.Set(r++,"SALVAGE",(g_salvageMarked?"MARKED":"clean"),(g_salvageMarked?cHot:cDim));
     }
   else
     {
      g_dash.Set(r++,"POSITION","flat",cDim);
      g_dash.Set(r++,"TICKETS","0",cDim);
      g_dash.Set(r++,"FLOATING","0.00",cDim);
      g_dash.Set(r++,"TRAIL","-",cDim);
      g_dash.Set(r++,"SALVAGE","-",cDim);
     }

   MqlDateTime t; TimeToStruct(TimeCurrent(),t);
   t.hour=0; t.min=0; t.sec=0;
   const datetime dayStart = StructToTime(t);
   const double today = RealisedSince(dayStart);
   const double week  = RealisedSince(dayStart-6*86400);
   g_dash.Set(r++,"TODAY",StringFormat("%+.2f",today),(today>=0?cOk:cBad));
   g_dash.Set(r++,"7 DAYS",StringFormat("%+.2f",week),(week>=0?cOk:cBad));
   g_dash.Set(r++,"EQUITY",StringFormat("%.2f",AccountInfoDouble(ACCOUNT_EQUITY)),clrWhite);

   ChartRedraw(0);
  }

//+------------------------------------------------------------------+
//| Close every ticket once the COMBINED floating profit reaches the  |
//| basket target. Returns true if it closed.                         |
//+------------------------------------------------------------------+
bool CheckBasketTakeProfit()
  {
   double profit  = 0.0;
   int    tickets = 0;
   for(int i=PositionsTotal()-1; i>=0; i--)
     {
      if(PositionGetSymbol(i)!=_Symbol)
         continue;
      if(PositionGetInteger(POSITION_MAGIC)!=InpMagic)
         continue;
      profit += PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
      tickets++;
     }
   if(tickets==0 || profit < InpBasketTakeMoney)
      return false;

   PrintFormat("basket take profit: %d ticket(s) combined at %.2f, target %.2f - closing all",
               tickets,profit,InpBasketTakeMoney);
   g_trader.CloseAll(StringFormat("basket take profit at %.2f across %d ticket(s)",
                                  profit,tickets));
   TrailClear(g_trail);
   g_salvageEntry  = 0.0;
   g_salvageSide   = -1;
   g_salvageMarked = false;
   return true;
  }

//+------------------------------------------------------------------+
//| Add to a losing position, same side, on a cumulative loss ladder. |
//+------------------------------------------------------------------+
void CheckScaleIn()
  {
   if(!InpAllowNewEntries)
      return;
   if(InpScaleInLossMoney<=0.0 || InpScaleInMaxTrades<2)
      return;

   const GoldPosition pos = g_trader.Snapshot();
   if(!pos.exists)
      return;
//--- Opposing tickets mean the netted view is not what is actually held, and
//--- adding to a book we are misreading is exactly how a small mess becomes a
//--- large one. Refuse rather than guess.
   if(pos.opposing)
     {
      Print("scale-in suppressed: opposing tickets under our magic - the netted view "
            "does not describe what is actually held");
      return;
     }
   if(pos.tickets >= InpScaleInMaxTrades)
      return;

//--- Live floating P&L across our tickets, the number the account would realise.
   double profit = 0.0;
   for(int i=PositionsTotal()-1; i>=0; i--)
     {
      if(PositionGetSymbol(i)!=_Symbol)
         continue;
      if(PositionGetInteger(POSITION_MAGIC)!=InpMagic)
         continue;
      profit += PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
     }
   if(profit >= 0.0)
      return;

//--- Add number `tickets` fires at `tickets * InpScaleInLossMoney` of loss, so
//--- the threshold moves away the instant an add lands and one tick cannot
//--- trigger two.
   const double needed = pos.tickets * InpScaleInLossMoney;
   if(-profit < needed)
      return;

   if(!SpreadIsAcceptable())
      return;

//--- The add carries its own bracket, priced from the CURRENT market rather
//--- than the original entry - it is a new position, not an extension.
   const double ref = (pos.side==POSITION_TYPE_BUY)
                      ? SymbolInfoDouble(_Symbol,SYMBOL_ASK)
                      : SymbolInfoDouble(_Symbol,SYMBOL_BID);
   if(ref<=0.0)
      return;
   const double slPct      = EffectiveStopPct(ref);
   const double slDistance = (slPct>0.0) ? ref*slPct/100.0 : 0.0;
   const double tpDistance = TakeDistancePrice(ref);

//--- Ladder size: base * mult^(adds so far). Normalised to the symbol's volume
//--- step, so "1.25x" becomes whatever the broker will actually accept - on a
//--- 0.1-step symbol 1.375 is dealt as 1.4, and the log states the real number.
   const double wanted = g_trader.Lots()*MathPow(MathMax(InpScaleInLotMult,1.0),pos.tickets);
   const double volume = g_trader.NormaliseVolume(wanted);
   if(volume<=0.0)
     {
      PrintFormat("scale-in suppressed: %.4f lots does not normalise to a tradable size",wanted);
      return;
     }

   const string reason = StringFormat("scale-in %d of %d: position is %.2f down, threshold %.2f",
                                      pos.tickets+1,InpScaleInMaxTrades,-profit,needed);
   PrintFormat("%s - adding %.2f lots %s at %s (ladder wanted %.4f, %.2f total on the book)",
               reason,volume,
               (pos.side==POSITION_TYPE_BUY?"BUY":"SELL"),
               DoubleToString(ref,g_trader.Digits()),
               wanted,pos.volume+volume);
   g_trader.OpenBracket(pos.side,volume,slDistance,tpDistance,reason);
  }

//+------------------------------------------------------------------+
//| Close a marked position the moment it turns profitable enough.    |
//+------------------------------------------------------------------+
void CheckSalvageExit()
  {
   const GoldPosition pos = g_trader.Snapshot();
   if(!pos.exists)
     {
      g_salvageEntry  = 0.0;
      g_salvageSide   = -1;
      g_salvageMarked = false;
      return;
     }

//--- Re-decide only when the position changed, not every tick.
   if(pos.entry!=g_salvageEntry || (int)pos.side!=g_salvageSide)
     {
      g_salvageEntry  = pos.entry;
      g_salvageSide   = (int)pos.side;
      const double adverse = EarlyAdverseMoney(pos);
      const double trigger = SalvageLossTrigger();
      g_salvageMarked = (trigger>0.0 && adverse>=trigger);
      if(g_salvageMarked)
         PrintFormat("salvage: MARKED - went %.2f against entry within the first %d minute(s), "
                     "trigger %.2f. Will close at the first %.2f of profit.",
                     adverse,InpSalvageWindowMin,trigger,InpSalvageExitProfit);
     }

   if(!g_salvageMarked)
      return;

//--- Live floating P&L, including swap and commission - the number the
//--- account actually realises, not a price-derived approximation.
   double profit = 0.0;
   bool   found  = false;
   for(int i=PositionsTotal()-1; i>=0; i--)
     {
      if(PositionGetSymbol(i)!=_Symbol)
         continue;
      if(PositionGetInteger(POSITION_MAGIC)!=InpMagic)
         continue;
      profit += PositionGetDouble(POSITION_PROFIT)
                + PositionGetDouble(POSITION_SWAP);
      found = true;
     }
   if(!found)
      return;

//--- 0 means "any profit above zero", so use a strict test there.
   const bool hit = (InpSalvageExitProfit>0.0) ? (profit>=InpSalvageExitProfit) : (profit>0.0);
   if(!hit)
      return;

   PrintFormat("salvage: closing at %.2f profit - this trade was adverse inside its first "
               "%d minute(s), so it is taken on recovery rather than held for the target",
               profit,InpSalvageWindowMin);
   g_trader.CloseAll(StringFormat("salvage exit at %.2f profit",profit));
   TrailClear(g_trail);
   g_salvageEntry  = 0.0;
   g_salvageSide   = -1;
   g_salvageMarked = false;
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
                                               EffectiveTrailActivationPct(pos.entry),
                                               EffectiveTrailPct(TrailReference(pos.entry)));
   if(fired!=EXIT_NONE)
     {
      //--- Normally the broker-side SL placed last bar has already fired
      //--- intrabar and this branch finds nothing to do. It covers the bar
      //--- where the stop could not be placed.
      const string reason = StringFormat("%s: %.2f%% level against a %s position, entry %.2f",
                                         ExitKindName(fired),
                                         (fired==EXIT_STOP?EffectiveStopPct(pos.entry)
                                                          :EffectiveTrailPct(TrailReference(pos.entry))),
                                         (pos.side==POSITION_TYPE_BUY?"BUY":"SELL"),pos.entry);
      g_trader.CloseAll(reason);
      TrailClear(g_trail);
      return;
     }

//--- 2. Move the broker-side stop to wherever this bar left it.
   if(pos.exists)
     {
      const double sl = ProtectiveStopPrice(g_trail,pos.side,pos.entry,
                                            EffectiveStopPct(pos.entry),
                                            EffectiveTrailActivationPct(pos.entry),
                                            EffectiveTrailPct(TrailReference(pos.entry)));
      g_trader.ApplyStop(sl);
     }

//--- 3. Warmup.
   double channelHigh = 0.0, channelLow = 0.0;
   if(!ChannelFromPriorBars(channelHigh,channelLow))
     {
      PrintFormat("no entry: not enough closed bars for a %d-bar channel",InpLookback);
      return;
     }

   g_chanHigh = channelHigh;
   g_chanLow  = channelLow;

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
