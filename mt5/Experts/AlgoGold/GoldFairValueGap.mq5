//+------------------------------------------------------------------+
//| GoldFairValueGap.mq5                                             |
//|                                                                  |
//| H4 displacement break -> fair value gap -> M15 confirmation.     |
//|                                                                  |
//| ====================================================================
//| WHERE THIS CAME FROM, AND WHAT THAT IS WORTH
//| ====================================================================
//| This is NOT a port. Nothing in algo/strategy/ corresponds to it,  |
//| so there is no backtest it has to agree with and - far more       |
//| importantly - none standing behind it.                            |
//|                                                                  |
//| The rules were transcribed from the published description of      |
//| "Best Prop Firm GOLD Strategy 2026 (High Win Rate XAUUSD Setup)", |
//| RBI FOREX, 28 Jun 2026, youtube.com/watch?v=WokhegaZ5WM. Verbatim,|
//| the six rules given there are:                                    |
//|                                                                  |
//|   1. Start by identifying a completed 4-hour candle.              |
//|   2. Wait for the next 4-hour candle to close above the previous  |
//|      candle's high (buy) or below its low (sell).                 |
//|   3. A valid setup requires the creation of a Fair Value Gap      |
//|      during the breakout.                                         |
//|   4. Once the FVG is formed, wait for price to retrace and tap    |
//|      into the imbalance zone.                                     |
//|   5. Use 15-minute displacement as confirmation before entering.  |
//|   6. Target the previous 4-hour buyside/sellside liquidity, and   |
//|      move the stop to break even at 1:1.                          |
//|                                                                  |
//| That source is a 479-subscriber channel with 502 views on the     |
//| video, monetised through broker affiliate links and a Telegram    |
//| channel. Its "high win rate" claim is unverified marketing and is |
//| treated here as a claim, not a finding. What the source is        |
//| actually good for is that the rules are MECHANICAL - they can be  |
//| written down, coded, and measured, which is the only property     |
//| that matters at this stage. The measuring has not been done.      |
//|                                                                  |
//| Read mt5/README.md on GoldIntradayScalper before trusting this    |
//| one on the strength of it compiling. That expert also looked      |
//| reasonable and returned profit factor 0.73-0.91 across ~5,900     |
//| trades. **Backtest this before it sees a funded account.**        |
//|                                                                  |
//| ====================================================================
//| WHAT THE SOURCE LEFT UNDEFINED, AND WHAT WAS CHOSEN INSTEAD
//| ====================================================================
//| Three things the rules above need in order to be code at all. Each|
//| is an input so it can be moved, and each is flagged because a     |
//| choice made here is NOT a choice the source endorsed.             |
//|                                                                  |
//| "DISPLACEMENT" (rule 5) has no definition in the source. Here it  |
//| is a closed entry-timeframe bar that (a) is in the trade          |
//| direction, (b) has a body of at least InpDisplaceAtrMult * ATR,   |
//| and (c) closes back OUT of the imbalance zone in that direction.  |
//| Clause (c) is what makes it displacement rather than merely a big |
//| bar: price entered the gap and was pushed out of it.              |
//|                                                                  |
//| THE STOP LOSS is not specified anywhere in the source - it gives  |
//| a break-even rule but never says where the stop starts. It is     |
//| placed beyond the far edge of the imbalance zone, plus            |
//| InpStopBufferAtrMult * ATR. The far edge is the level that, if    |
//| traded through, means the gap has been filled and the premise is  |
//| gone; putting the stop there makes "stopped out" and "setup was   |
//| wrong" the same event, which is the only stop placement that does |
//| not need a second justification.                                  |
//|                                                                  |
//| "PREVIOUS BUYSIDE LIQUIDITY" (rule 6) is formalised as the        |
//| highest high of the InpLiquidityLookback structure bars STRICTLY  |
//| BEFORE the breakout bar - the same "exclude the bar being tested" |
//| discipline GoldTrendlineBreakout applies to its Donchian channel, |
//| and for the same reason. A target inside the range that produced  |
//| it is not a target.                                               |
//|                                                                  |
//| ====================================================================
//| THE FVG IS THE THREE-CANDLE DEFINITION, AND IT MUST STRADDLE THE  |
//| BREAKOUT                                                          |
//| ====================================================================
//| A bullish fair value gap exists across shifts 3,2,1 when          |
//| low(1) > high(3): the middle candle moved far enough that no      |
//| trade occurred between those two prices. The zone is              |
//| [high(3), low(1)]. Bearish is the mirror: high(1) < low(3), zone  |
//| [high(1), low(3)].                                                |
//|                                                                  |
//| Rule 3 says the gap must be created DURING the breakout, so the   |
//| gap is tested on exactly the three bars ending at the breakout    |
//| bar. A gap found anywhere else is a different setup and is not    |
//| taken. This is the rule most likely to be quietly dropped in an   |
//| implementation - it is also the one doing most of the filtering.  |
//|                                                                  |
//| ====================================================================
//| DECIDE ON THE CLOSED BAR, FILL AT THE NEXT PRICE                  |
//| ====================================================================
//| As in both ports: nothing happens intrabar except a broker-side   |
//| stop or target firing. The break-even move of rule 6 is therefore |
//| evaluated on entry-timeframe bar closes, not tick by tick. A tick |
//| that touches 1R and retraces inside the same bar does not arm it. |
//| That is a real difference from a human doing this by hand, and it |
//| is deliberate - the alternative is a rule whose result depends on |
//| tick density and cannot be reproduced by a backtest.              |
//+------------------------------------------------------------------+
#property copyright "algo trading - GOLDM/XAUUSD engine"
#property link      ""
#property version   "1.00"
#property description "H4 FVG breakout with M15 displacement confirmation on XAUUSD. NOT a port - no backtest behind it."
#property strict

//--- ProtectiveExits FIRST, and not because this expert uses it.
//--- Trader.mqh's RebuildTrail takes a TrailState, which is declared in
//--- ProtectiveExits.mqh, so Trader.mqh does not compile on its own.
//--- Reordering these two produces 35 errors inside Trader.mqh and none
//--- in this file, which is a confusing place to start debugging.
#include <AlgoGold\ProtectiveExits.mqh>
#include <AlgoGold\Trader.mqh>
#include <AlgoGold\ScalpFilters.mqh>
#include <AlgoGold\Dashboard.mqh>

//+------------------------------------------------------------------+
//| Inputs                                                           |
//+------------------------------------------------------------------+
input group "--- Structure and confirmation timeframes ---"
input ENUM_TIMEFRAMES InpStructureTf   = PERIOD_H4;   // Breakout + FVG timeframe (source says H4)
input ENUM_TIMEFRAMES InpEntryTf       = PERIOD_M15;  // Confirmation timeframe (source says M15)

input group "--- Setup detection ---"
input int    InpLiquidityLookback      = 12;     // Structure bars searched for the target. Minimum 2
input int    InpSetupExpiryBars        = 6;      // Structure bars an un-triggered setup stays armed. 0 = never expires
input double InpMinZoneAtrMult         = 0.15;   // Reject gaps thinner than this * ATR. 0 = accept any gap

input group "--- Confirmation (rule 5 - the source does not define it) ---"
input int    InpAtrPeriod              = 14;     // ATR period, entry timeframe
input double InpDisplaceAtrMult        = 0.60;   // Confirming bar body, as a multiple of ATR
input int    InpConfirmWindowBars      = 8;      // Entry bars after the tap to wait for it. 0 = unlimited

input group "--- Risk (rule 6 gives break-even but never the stop) ---"
input double InpStopBufferAtrMult      = 0.25;   // Padding beyond the far edge of the zone, * ATR
input double InpMinRewardRisk          = 1.5;    // Skip the setup if the liquidity target is closer than this
input bool   InpBreakEvenAt1R          = true;   // Rule 6: stop to entry once 1R is reached
input double InpBreakEvenOffsetAtr     = 0.05;   // Nudge past entry so break even is not a scratch minus costs

input group "--- Sizing ---"
input double InpRiskMoney              = 0.0;    // Risk per trade in account currency. 0 = use InpLots
input double InpLots                   = 0.05;   // Volume in MT5 LOTS, used when InpRiskMoney is 0

input group "--- Session and daily governors ---"
input int    InpSessionStartHour       = 7;      // Server hour new entries open. start==end means all day
input int    InpSessionEndHour         = 20;     // Server hour new entries stop
input int    InpFridayEndHour          = 20;     // No new entries from this Friday hour. -1 disables
input double InpDailyLossLimit         = 0.0;    // Halt for the day at this realised loss. 0 = off
input double InpDailyProfitTarget      = 0.0;    // Halt for the day at this realised profit. 0 = off
input int    InpMaxTradesPerDay        = 3;      // Entry cap per day. 0 = off

input group "--- Execution ---"
input long   InpMagic                  = 20260904;// Must differ from 20260828/01/02/03
input ulong  InpSlippagePoints         = 30;     // Max deviation, points
input int    InpMaxSpreadPoints        = 0;      // Block NEW entries above this spread. 0 = off
input bool   InpAllowNewEntries        = true;   // false = manage open positions only
input bool   InpShowDashboard          = true;   // Draw the on-chart panel
input string InpComment                = "AlgoGold FVG"; // Cosmetic only - MT5 overwrites it

//+------------------------------------------------------------------+
//| An armed setup.                                                  |
//|                                                                  |
//| Held in memory rather than persisted, and rebuilt on init by      |
//| rescanning the structure bars - the same reasoning Trader.mqh     |
//| gives for replaying the trail. A setup is a function of the last  |
//| InpSetupExpiryBars bars and nothing else, so a freshly loaded     |
//| expert facing the same history arms the same setup.               |
//+------------------------------------------------------------------+
struct FvgSetup
  {
   bool               active;
   ENUM_POSITION_TYPE side;
   double             zoneTop;       // upper edge of the imbalance
   double             zoneBottom;    // lower edge of the imbalance
   double             target;        // the liquidity level of rule 6
   datetime           armedAt;       // open time of the structure bar that broke
   bool               tapped;        // price has traded into the zone
   datetime           tappedAt;      // entry-timeframe bar of the tap
  };

//+------------------------------------------------------------------+
//| State                                                            |
//+------------------------------------------------------------------+
CGoldTrader      g_trader;
CGoldDashboard   g_dash;
DayGuard         g_day;
GateStats        g_gates;
FvgSetup         g_setup;

datetime         g_lastEntryBar    = 0;
datetime         g_lastStructBar   = 0;
int              g_atrHandle       = INVALID_HANDLE;

//--- Risk of the open position, in price. Re-derived on init from the
//--- live stop rather than persisted, so a recompile mid-trade cannot
//--- lose the level the break-even rule is measured against.
double           g_openRisk        = 0.0;
bool             g_beDone          = false;

//+------------------------------------------------------------------+
//| ATR on the entry timeframe, at `shift`. 0 when unavailable.      |
//+------------------------------------------------------------------+
double EntryAtr(const int shift)
  {
   if(g_atrHandle==INVALID_HANDLE)
      return 0.0;
   double buf[];
   if(CopyBuffer(g_atrHandle,0,shift,1,buf) != 1)
      return 0.0;
   return buf[0];
  }

//+------------------------------------------------------------------+
//| Clear the armed setup.                                           |
//+------------------------------------------------------------------+
void SetupClear(FvgSetup &s)
  {
   s.active     = false;
   s.side       = POSITION_TYPE_BUY;
   s.zoneTop    = 0.0;
   s.zoneBottom = 0.0;
   s.target     = 0.0;
   s.armedAt    = 0;
   s.tapped     = false;
   s.tappedAt   = 0;
  }

//+------------------------------------------------------------------+
//| Try to arm a setup from the structure bar at `shift`.            |
//|                                                                  |
//| `shift` is the breakout bar. Rules 2 and 3 are tested together    |
//| because rule 3 says the gap must be created DURING the breakout:  |
//| the three candles of the FVG are shift+2, shift+1, shift, and the |
//| broken level is the high/low of shift+1.                          |
//+------------------------------------------------------------------+
bool TryArmSetup(const int shift,FvgSetup &out,string &why)
  {
   const string sym = _Symbol;
   const int    need = shift + MathMax(2,InpLiquidityLookback) + 1;
   if(Bars(sym,InpStructureTf) < need)
     {
      why = "not enough structure history";
      return false;
     }

   const double c0 = iClose(sym,InpStructureTf,shift);      // the breakout bar
   const double h1 = iHigh (sym,InpStructureTf,shift+1);    // the bar it must break
   const double l1 = iLow  (sym,InpStructureTf,shift+1);
   const double l0 = iLow  (sym,InpStructureTf,shift);
   const double h0 = iHigh (sym,InpStructureTf,shift);
   const double h2 = iHigh (sym,InpStructureTf,shift+2);    // the far side of the gap
   const double l2 = iLow  (sym,InpStructureTf,shift+2);

   const bool brokeUp   = (c0 > h1);
   const bool brokeDown = (c0 < l1);
   if(!brokeUp && !brokeDown)
     {
      why = "no close beyond the previous bar's range";
      return false;
     }
//--- An outside bar closing beyond BOTH extremes is not a directional
//--- break, it is a range expansion. Refusing it costs nothing and
//--- avoids arming two contradictory setups from one bar.
   if(brokeUp && brokeDown)
     {
      why = "closed beyond both extremes - not directional";
      return false;
     }

   const double atr = EntryAtr(1);
   double top = 0.0, bottom = 0.0;

   if(brokeUp)
     {
      //--- Bullish FVG across shift+2, shift+1, shift.
      if(!(l0 > h2))
        {
         why = "break was not accompanied by a bullish gap";
         return false;
        }
      bottom = h2;
      top    = l0;
     }
   else
     {
      if(!(h0 < l2))
        {
         why = "break was not accompanied by a bearish gap";
         return false;
        }
      bottom = h0;
      top    = l2;
     }

   const double width = top - bottom;
   if(width <= 0.0)
     {
      why = "degenerate gap";
      return false;
     }
//--- NOTE THE UNIT MISMATCH, because it is deliberate rather than
//--- overlooked: this compares a STRUCTURE-timeframe gap against an
//--- ENTRY-timeframe ATR. It is not a claim that the two are
//--- commensurate. It is a floor that throws out one-tick gaps left by
//--- a thin quote, and it is expressed in ATR only so that the floor
//--- scales with volatility instead of being a hardcoded price. Do not
//--- read the default as "15% of a normal move".
   if(InpMinZoneAtrMult > 0.0 && atr > 0.0 && width < InpMinZoneAtrMult*atr)
     {
      why = StringFormat("gap %.2f is thinner than %.2f (%.2f x ATR)",
                         width,InpMinZoneAtrMult*atr,InpMinZoneAtrMult);
      return false;
     }

//--- Rule 6's target: the extreme of the bars STRICTLY BEFORE the
//--- breakout bar. Starting at shift+1 rather than shift is the same
//--- exclusion the Donchian channel makes.
   double liquidity = brokeUp ? -DBL_MAX : DBL_MAX;
   for(int i=shift+1; i<=shift+InpLiquidityLookback; i++)
     {
      if(brokeUp)
         liquidity = MathMax(liquidity,iHigh(sym,InpStructureTf,i));
      else
         liquidity = MathMin(liquidity,iLow(sym,InpStructureTf,i));
     }

   out.active     = true;
   out.side       = brokeUp ? POSITION_TYPE_BUY : POSITION_TYPE_SELL;
   out.zoneTop    = top;
   out.zoneBottom = bottom;
   out.target     = liquidity;
   out.armedAt    = iTime(sym,InpStructureTf,shift);
   out.tapped     = false;
   out.tappedAt   = 0;
   why            = "";
   return true;
  }

//+------------------------------------------------------------------+
//| Rescan recent structure bars for a still-valid setup.            |
//|                                                                  |
//| Runs on init and after any setup is consumed or dropped. Walks    |
//| from the oldest candidate forward so the MOST RECENT valid setup  |
//| wins - an older gap that newer price action has already worked    |
//| through should not outrank the one just formed.                   |
//+------------------------------------------------------------------+
void RescanSetups(void)
  {
   SetupClear(g_setup);
   const int span = (InpSetupExpiryBars>0 ? InpSetupExpiryBars : 6);

   for(int shift=span; shift>=1; shift--)
     {
      FvgSetup cand;
      SetupClear(cand);
      string why = "";
      if(!TryArmSetup(shift,cand,why))
         continue;
      if(ZoneInvalidated(cand,shift))
         continue;
      g_setup = cand;
     }

   if(g_setup.active)
      PrintFormat("setup rearmed from history: %s zone %.2f-%.2f target %.2f (armed %s)",
                  (g_setup.side==POSITION_TYPE_BUY?"BUY":"SELL"),
                  g_setup.zoneBottom,g_setup.zoneTop,g_setup.target,
                  TimeToString(g_setup.armedAt,TIME_DATE|TIME_MINUTES));
  }

//+------------------------------------------------------------------+
//| Has price closed clean through the zone against the setup?       |
//|                                                                  |
//| A gap that has been fully traded through and closed beyond is no  |
//| longer an imbalance - it has been rebalanced, which is the whole  |
//| premise. Checked on structure closes from `sinceShift`-1 forward. |
//+------------------------------------------------------------------+
bool ZoneInvalidated(const FvgSetup &s,const int sinceShift)
  {
   for(int i=sinceShift-1; i>=1; i--)
     {
      const double c = iClose(_Symbol,InpStructureTf,i);
      if(s.side==POSITION_TYPE_BUY  && c < s.zoneBottom)
         return true;
      if(s.side==POSITION_TYPE_SELL && c > s.zoneTop)
         return true;
     }
   return false;
  }

//+------------------------------------------------------------------+
//| Init                                                             |
//+------------------------------------------------------------------+
int OnInit()
  {
   SetupClear(g_setup);
   DayGuardReset(g_day);
   GateStatsReset(g_gates);

   if(InpLiquidityLookback < 2)
     {
      Print("FATAL: InpLiquidityLookback must be at least 2");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpAtrPeriod < 1)
     {
      Print("FATAL: InpAtrPeriod must be at least 1");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpMinRewardRisk <= 0.0)
     {
      Print("FATAL: InpMinRewardRisk must be positive - a setup with no minimum "
            "reward:risk cannot reject anything");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(PeriodSeconds(InpStructureTf) <= PeriodSeconds(InpEntryTf))
     {
      PrintFormat("FATAL: the structure timeframe (%s) must be SLOWER than the entry "
                  "timeframe (%s). The strategy is a higher-timeframe setup confirmed "
                  "on a lower one; equal or inverted makes rule 5 meaningless.",
                  EnumToString(InpStructureTf),EnumToString(InpEntryTf));
      return INIT_PARAMETERS_INCORRECT;
     }
//--- Percent-based protective exits are not used here, so the three
//--- percentage arguments are passed as zero. The magic check is the
//--- part of the preflight that matters for this expert.
   if(!GoldPreflight(InpMagic,0.0,0.0,0.0))
      return INIT_PARAMETERS_INCORRECT;

   if(!g_trader.Init(_Symbol,InpMagic,InpLots,InpSlippagePoints,InpComment))
      return INIT_FAILED;

   g_atrHandle = iATR(_Symbol,InpEntryTf,InpAtrPeriod);
   if(g_atrHandle==INVALID_HANDLE)
     {
      Print("FATAL: could not create the ATR handle");
      return INIT_FAILED;
     }

   if(_Period != InpEntryTf)
      PrintFormat("WARNING: the chart is %s but the entry timeframe is %s. This works, "
                  "but in the strategy tester the modelling granularity follows the "
                  "CHART period - run it on %s to test what it will actually do.",
                  EnumToString((ENUM_TIMEFRAMES)_Period),EnumToString(InpEntryTf),
                  EnumToString(InpEntryTf));

   AdoptOpenPosition();
   RescanSetups();

   g_lastEntryBar  = iTime(_Symbol,InpEntryTf,0);
   g_lastStructBar = iTime(_Symbol,InpStructureTf,0);

   if(InpShowDashboard)
      g_dash.Create("AlgoGoldFvg_","FVG "+_Symbol);

   PrintFormat("FVG %s setup / %s confirm on %s | displacement %.2f x ATR(%d) | "
               "stop buffer %.2f x ATR | min RR %.2f | magic %d",
               EnumToString(InpStructureTf),EnumToString(InpEntryTf),_Symbol,
               InpDisplaceAtrMult,InpAtrPeriod,InpStopBufferAtrMult,
               InpMinRewardRisk,(int)InpMagic);
   Print("NOT a port and NOT backtested. See the header, and mt5/README.md on "
         "GoldIntradayScalper for what an untested expert is worth.");
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   if(g_atrHandle!=INVALID_HANDLE)
      IndicatorRelease(g_atrHandle);
   g_dash.Destroy();
//--- GateStats is the scalper's struct, reused rather than forked. Two
//--- of its rows do not apply here and will always read zero: there is
//--- no cooldown gate (one position at a time already enforces spacing)
//--- and no spread-multiple cost gate. Its "cost gate" row counts the
//--- reward:risk rejections instead - the nearest thing this expert has
//--- to a target declining to justify its risk.
   GateStatsReport(g_gates);
   PrintFormat("stopped (reason %d). Open positions are LEFT AS THEY ARE - removing an "
               "expert is not a flatten instruction.",reason);
  }

//+------------------------------------------------------------------+
//| Re-derive the break-even state of a position we already hold.    |
//|                                                                  |
//| The risk distance is not persisted. It is recovered from the live |
//| stop, which is the only record of it that survives a recompile.   |
//| If the stop is already at or beyond entry, break even has already |
//| been done and must not be redone at a worse level.                |
//+------------------------------------------------------------------+
void AdoptOpenPosition(void)
  {
   g_openRisk = 0.0;
   g_beDone   = false;

   const GoldPosition pos = g_trader.Snapshot();
   if(!pos.exists)
      return;

   double sl = 0.0;
   for(int i=PositionsTotal()-1; i>=0; i--)
     {
      const ulong ticket = PositionGetTicket(i);
      if(ticket==0 || !PositionSelectByTicket(ticket))
         continue;
      if(PositionGetString(POSITION_SYMBOL)!=_Symbol)
         continue;
      if(PositionGetInteger(POSITION_MAGIC)!=InpMagic)
         continue;
      sl = PositionGetDouble(POSITION_SL);
      break;
     }

   if(sl>0.0)
     {
      g_openRisk = MathAbs(pos.entry - sl);
      g_beDone   = (pos.side==POSITION_TYPE_BUY) ? (sl >= pos.entry) : (sl <= pos.entry);
     }

   PrintFormat("adopted an existing %s position of %.2f lots at %.2f | stop %.2f | "
               "risk %.2f | break even %s (magic %d)",
               (pos.side==POSITION_TYPE_BUY?"BUY":"SELL"),pos.volume,pos.entry,sl,
               g_openRisk,(g_beDone?"already done":"pending"),(int)InpMagic);
   if(sl<=0.0)
      Print("WARNING: the adopted position has NO stop loss. The break-even rule "
            "measures 1R from the stop, so it stays disarmed until one is set by hand.");
//--- On a hedging account the net position can be several tickets with
//--- several stops, and 1R is then not a single number. The first
//--- ticket's stop is used, which is a guess. Say so rather than let a
//--- break-even move fire off an arbitrary one of them.
   if(pos.tickets>1)
      PrintFormat("WARNING: %d tickets make up this position. Risk was derived from one "
                  "of their stops (%.2f); if they differ, the break-even trigger is "
                  "measured against the wrong level.",pos.tickets,sl);
   if(pos.opposing)
      Print("WARNING: opposing tickets are open on both sides. They still pay financing "
            "and still hold spread even when they net to zero.");
  }

//+------------------------------------------------------------------+
//| Tick: nothing happens except on a bar close.                     |
//+------------------------------------------------------------------+
void OnTick()
  {
   const datetime structNow = iTime(_Symbol,InpStructureTf,0);
   if(structNow!=g_lastStructBar && structNow!=0)
     {
      g_lastStructBar = structNow;
      OnClosedStructureBar();
     }

   const datetime entryNow = iTime(_Symbol,InpEntryTf,0);
   if(entryNow==g_lastEntryBar || entryNow==0)
      return;
   g_lastEntryBar = entryNow;
   OnClosedEntryBar();
  }

//+------------------------------------------------------------------+
//| A structure bar closed: rules 1-3, and setup ageing.             |
//+------------------------------------------------------------------+
void OnClosedStructureBar(void)
  {
//--- An armed setup is dropped before a new one is looked for, so a
//--- fresh break always supersedes a stale gap rather than queueing
//--- behind it. Two live setups would mean two contradictory biases.
   if(g_setup.active)
     {
      //--- `age` is how many closed bars back the breakout is, so the bars
      //--- to re-examine are age-1..1 - which is exactly what
      //--- ZoneInvalidated walks. Passing a literal 1 here instead would
      //--- make its loop run zero times and silently disable invalidation
      //--- altogether, leaving expiry as the only way a setup could ever
      //--- be dropped.
      const int age = iBarShift(_Symbol,InpStructureTf,g_setup.armedAt,false);
      if(ZoneInvalidated(g_setup,age))
        {
         PrintFormat("setup dropped: price closed through the %s zone %.2f-%.2f",
                     (g_setup.side==POSITION_TYPE_BUY?"BUY":"SELL"),
                     g_setup.zoneBottom,g_setup.zoneTop);
         SetupClear(g_setup);
        }
      else if(InpSetupExpiryBars>0 && age > InpSetupExpiryBars)
        {
         PrintFormat("setup expired unfilled after %d structure bars",age);
         SetupClear(g_setup);
        }
     }

   FvgSetup cand;
   SetupClear(cand);
   string why = "";
   if(!TryArmSetup(1,cand,why))
      return;

   g_setup = cand;
   PrintFormat("ARMED %s | gap %.2f-%.2f (%.2f wide) | target %.2f | broke %s",
               (cand.side==POSITION_TYPE_BUY?"BUY":"SELL"),
               cand.zoneBottom,cand.zoneTop,cand.zoneTop-cand.zoneBottom,cand.target,
               TimeToString(cand.armedAt,TIME_DATE|TIME_MINUTES));
  }

//+------------------------------------------------------------------+
//| An entry bar closed: rules 4-6.                                  |
//|                                                                  |
//| Position management runs FIRST and unconditionally. A held        |
//| position must never wait on a setup gate - the same ordering the  |
//| ports use to keep protective exits ahead of the warmup check.     |
//+------------------------------------------------------------------+
void OnClosedEntryBar(void)
  {
   const GoldPosition pos = g_trader.Snapshot();
   if(pos.exists)
     {
      ManageOpenPosition(pos);
      RepaintDashboard(pos);
      return;                        // one position at a time, by design
     }

   if(g_openRisk!=0.0 || g_beDone)
     {
      g_openRisk = 0.0;
      g_beDone   = false;
     }

   DayGuardRefresh(g_day,_Symbol,InpMagic,TimeCurrent());

   if(g_setup.active)
      CheckForEntry();

   RepaintDashboard(pos);
  }

//+------------------------------------------------------------------+
//| Rule 6: stop to break even once 1R is reached.                   |
//+------------------------------------------------------------------+
void ManageOpenPosition(const GoldPosition &pos)
  {
   if(!InpBreakEvenAt1R || g_beDone || g_openRisk<=0.0)
      return;

   const double close = iClose(_Symbol,InpEntryTf,1);
   const bool   reached = (pos.side==POSITION_TYPE_BUY)
                          ? (close >= pos.entry + g_openRisk)
                          : (close <= pos.entry - g_openRisk);
   if(!reached)
      return;

//--- Entry exactly is a loser once the spread is paid, so the level is
//--- nudged a little way into profit. The offset is ATR-relative for
//--- the reason ScalpFilters gives: one input has to mean the same
//--- thing in a quiet session and a violent one.
   const double atr    = EntryAtr(1);
   const double offset = (atr>0.0) ? InpBreakEvenOffsetAtr*atr : 0.0;
   const double level  = (pos.side==POSITION_TYPE_BUY)
                         ? pos.entry + offset
                         : pos.entry - offset;

   if(!g_trader.ApplyStop(NormalizeDouble(level,g_trader.Digits())))
     {
      Print("break-even move REJECTED - the stop stays where it is, and this will be "
            "retried on the next bar");
      return;
     }
   g_beDone = true;
   PrintFormat("BREAK EVEN: 1R (%.2f) reached at %.2f, stop moved to %.2f",
               g_openRisk,close,level);
  }

//+------------------------------------------------------------------+
//| Rules 4 and 5, then the gates, then the order.                   |
//+------------------------------------------------------------------+
void CheckForEntry(void)
  {
   const string sym  = _Symbol;
   const double high = iHigh (sym,InpEntryTf,1);
   const double low  = iLow  (sym,InpEntryTf,1);
   const double open = iOpen (sym,InpEntryTf,1);
   const double close= iClose(sym,InpEntryTf,1);
   const bool   isBuy= (g_setup.side==POSITION_TYPE_BUY);

//--- Rule 4: has price retraced into the imbalance?
   if(!g_setup.tapped)
     {
      const bool tapped = isBuy ? (low <= g_setup.zoneTop) : (high >= g_setup.zoneBottom);
      if(!tapped)
         return;
      g_setup.tapped   = true;
      g_setup.tappedAt = iTime(sym,InpEntryTf,1);
      PrintFormat("TAP: price entered the %s zone %.2f-%.2f",
                  (isBuy?"BUY":"SELL"),g_setup.zoneBottom,g_setup.zoneTop);
     }

//--- A tap that never produces displacement should not wait forever;
//--- the longer price sits inside a gap, the less it is an imbalance.
   if(InpConfirmWindowBars>0)
     {
      const int since = iBarShift(sym,InpEntryTf,g_setup.tappedAt,false);
      if(since > InpConfirmWindowBars)
        {
         PrintFormat("setup dropped: %d entry bars since the tap with no displacement "
                     "(window %d)",since,InpConfirmWindowBars);
         SetupClear(g_setup);
         return;
        }
     }

//--- Rule 5: displacement out of the zone, in the trade direction.
   const double atr  = EntryAtr(1);
   if(atr<=0.0)
     {
      Print("no ATR yet - confirmation cannot be judged, waiting");
      return;
     }
   const double body = MathAbs(close-open);
   const bool directional = isBuy ? (close>open) : (close<open);
   const bool impulsive   = (body >= InpDisplaceAtrMult*atr);
   const bool escaped     = isBuy ? (close > g_setup.zoneTop) : (close < g_setup.zoneBottom);
   if(!directional || !impulsive || !escaped)
      return;

   PrintFormat("CONFIRMED: %s displacement, body %.2f vs %.2f required, close %.2f "
               "outside the zone",(isBuy?"BUY":"SELL"),body,InpDisplaceAtrMult*atr,close);
   g_gates.signals++;
   TryEnter(atr);
  }

//+------------------------------------------------------------------+
//| Gates, sizing, and the bracketed order.                          |
//|                                                                  |
//| WHICH REJECTIONS DROP THE SETUP AND WHICH DO NOT                  |
//|                                                                  |
//| A TRANSIENT gate - session window, daily governor, wide spread -  |
//| leaves the setup armed. The imbalance is still there; only the    |
//| clock or the book said no, and both change. A STRUCTURAL          |
//| rejection - the target is behind price, the reward:risk does not  |
//| clear the floor, price is already through the stop - clears it,   |
//| because none of those become true again by waiting. Getting this  |
//| backwards produces either a setup that retries forever against a  |
//| target it can never reach, or one thrown away because the spread  |
//| widened for a minute.                                             |
//+------------------------------------------------------------------+
void TryEnter(const double atr)
  {
   const bool isBuy = (g_setup.side==POSITION_TYPE_BUY);

   if(!InpAllowNewEntries)
     {
      Print("entry suppressed: InpAllowNewEntries is false");
      return;
     }

   string why = "";
   if(!SessionAllowsEntry(TimeCurrent(),InpSessionStartHour,InpSessionEndHour,
                          InpFridayEndHour,why))
     {
      g_gates.outOfSession++;
      Print("entry blocked - ",why);
      return;
     }
   if(!DayGuardAllowsEntry(g_day,InpDailyLossLimit,InpDailyProfitTarget,InpMaxTradesPerDay))
     {
      g_gates.dayHalted++;
      return;
     }

   if(InpMaxSpreadPoints>0)
     {
      const long spread = SymbolInfoInteger(_Symbol,SYMBOL_SPREAD);
      if(spread > InpMaxSpreadPoints)
        {
         g_gates.wideSpread++;
         PrintFormat("entry blocked: spread %d points is above the %d-point limit",
                     (int)spread,InpMaxSpreadPoints);
         return;
        }
     }

   const double bid = SymbolInfoDouble(_Symbol,SYMBOL_BID);
   const double ask = SymbolInfoDouble(_Symbol,SYMBOL_ASK);
   if(bid<=0.0 || ask<=0.0)
     {
      Print("entry blocked: no two-sided quote");
      return;
     }

//--- The stop sits beyond the FAR edge of the zone: through it, the gap
//--- is filled and the reason for the trade is gone.
   const double buffer   = InpStopBufferAtrMult*atr;
   const double stopLevel= isBuy ? (g_setup.zoneBottom - buffer)
                                 : (g_setup.zoneTop    + buffer);
//--- Distances are measured from the side the position will be valued
//--- on, matching OpenBracket's own anchoring.
   const double anchor       = isBuy ? bid : ask;
   const double stopDistance = MathAbs(anchor - stopLevel);
   const double takeDistance = MathAbs(g_setup.target - anchor);

   if(stopDistance<=0.0)
     {
      g_gates.sizing++;
      Print("entry blocked: price is already through the stop level");
      SetupClear(g_setup);
      return;
     }
//--- The target must be on the correct side of price. A liquidity level
//--- that price has already run through is not a target, and computing
//--- a reward:risk from its absolute distance would silently invert it.
   const bool targetAhead = isBuy ? (g_setup.target > anchor) : (g_setup.target < anchor);
   if(!targetAhead)
     {
      g_gates.costGate++;
      PrintFormat("setup dropped: the liquidity target %.2f is already behind price %.2f",
                  g_setup.target,anchor);
      SetupClear(g_setup);
      return;
     }

   const double rr = takeDistance/stopDistance;
   if(rr < InpMinRewardRisk)
     {
      g_gates.costGate++;
      PrintFormat("entry blocked: reward:risk %.2f is below the %.2f minimum "
                  "(stop %.2f, target %.2f)",rr,InpMinRewardRisk,stopDistance,takeDistance);
      SetupClear(g_setup);
      return;
     }

   double volume = InpLots;
   if(InpRiskMoney > 0.0)
     {
      volume = g_trader.LotsForRisk(InpRiskMoney,stopDistance);
      if(volume<=0.0)
        {
         g_gates.sizing++;
         PrintFormat("entry blocked: %.2f of risk over a %.2f stop is not a tradable "
                     "volume on this symbol",InpRiskMoney,stopDistance);
         return;
        }
     }
   volume = g_trader.NormaliseVolume(volume);

   const string reason = StringFormat("FVG %s, zone %.2f-%.2f, RR %.2f",
                                      (isBuy?"BUY":"SELL"),
                                      g_setup.zoneBottom,g_setup.zoneTop,rr);
   if(!g_trader.OpenBracket(g_setup.side,volume,stopDistance,takeDistance,reason))
      return;

//--- The setup is consumed by the entry whether or not it works out.
//--- Re-entering the same imbalance after it has already been traded
//--- would be a second trade on one signal.
   g_openRisk = stopDistance;
   g_beDone   = false;
   g_gates.taken++;
//--- DayGuardRefresh recomputes `trades` from the deal history, so this
//--- increment is overwritten on the next flat bar. It is here anyway
//--- because the entry deal is not necessarily in history yet, and
//--- without it the trade cap could be cleared twice in one bar.
   g_day.trades++;
//--- Cleared and NOT rescanned. RescanSetups would happily re-arm the
//--- very setup just traded, and the next retrace into the same
//--- imbalance would be a second entry on one signal. A new setup
//--- arrives on a later structure close, like any other.
//---
//--- The one case this does not cover is a reload: OnInit's rescan
//--- cannot know a setup was already traded, because nothing about that
//--- is persisted. A recompile in the window between an exit and the
//--- expiry of the setup that produced it can therefore re-arm it once.
   SetupClear(g_setup);
  }

//+------------------------------------------------------------------+
//| Dashboard                                                        |
//+------------------------------------------------------------------+
void RepaintDashboard(const GoldPosition &pos)
  {
   if(!g_dash.Active())
      return;
   g_dash.Refresh("FVG "+_Symbol);

   if(pos.exists)
      g_dash.Set(0,"position",StringFormat("%s %.2f @ %.2f",
                 (pos.side==POSITION_TYPE_BUY?"BUY":"SELL"),pos.volume,pos.entry),
                 (pos.side==POSITION_TYPE_BUY?clrLime:clrTomato));
   else
      g_dash.Set(0,"position","flat",clrSilver);

   if(g_setup.active)
     {
      g_dash.Set(1,"setup",StringFormat("%s %.2f-%.2f",
                 (g_setup.side==POSITION_TYPE_BUY?"BUY":"SELL"),
                 g_setup.zoneBottom,g_setup.zoneTop),clrAqua);
      g_dash.Set(2,"tapped",(g_setup.tapped?"yes - awaiting displacement":"no"),
                 (g_setup.tapped?clrGold:clrSilver));
      g_dash.Set(3,"target",DoubleToString(g_setup.target,g_trader.Digits()),clrAqua);
     }
   else
     {
      g_dash.Set(1,"setup","none armed",clrSilver);
      g_dash.Set(2,"tapped","-",clrSilver);
      g_dash.Set(3,"target","-",clrSilver);
     }

   g_dash.Set(4,"break even",
              (!InpBreakEvenAt1R ? "disabled" : (g_beDone?"done":"pending")),
              (g_beDone?clrLime:clrSilver));
   g_dash.Set(5,"today",StringFormat("%d trades  %.2f",g_day.trades,g_day.realised),
              (g_day.realised<0.0?clrTomato:clrLime));
   if(g_day.halted)
      g_dash.Set(6,"HALTED",g_day.haltReason,clrTomato);
   else
      g_dash.Set(6,"status","running",clrSilver);
  }
//+------------------------------------------------------------------+
