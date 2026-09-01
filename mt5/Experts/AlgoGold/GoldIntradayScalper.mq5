//+------------------------------------------------------------------+
//| GoldIntradayScalper.mq5                                          |
//|                                                                  |
//| An intraday scalper: trend-filtered pullback entries, an ATR      |
//| bracket placed with the order, and hard daily governors.          |
//|                                                                  |
//| Unlike the other two experts here, this one is NOT a port. There  |
//| is no counterpart in algo/strategy/, so there is no measured      |
//| backtest behind it and nothing for it to agree with. Read the     |
//| section below before sizing it.                                   |
//|                                                                  |
//| ====================================================================
//| WHAT THE MEASUREMENTS ALREADY SAY ABOUT SCALPING HERE
//| ====================================================================
//| mt5/README.md, from 2.11 years of real XAUUSD bars against real   |
//| Vantage costs:                                                    |
//|                                                                   |
//|              M15        M30        H1                             |
//|   MACD    -$230,052   $97,653   $190,186                          |
//|   Breakout -$14,779   $52,467   $136,477    (both with 0.5% stop) |
//|                                                                   |
//| The stated cause is not the signal. Trade count roughly halves    |
//| per step to a slower interval while the $0.29 round-trip spread   |
//| is charged PER ROUND TRIP, so cost is the dominant term. A        |
//| scalper trades MORE than the M15 column that lost money.          |
//|                                                                   |
//| That is not an argument that this cannot work. It is an argument  |
//| that the only thing worth engineering carefully is the cost side, |
//| so this expert is built around three commitments the swing        |
//| experts do not make:                                              |
//|                                                                   |
//|  1. THE COST GATE IS MANDATORY AND ON BY DEFAULT. A trade whose   |
//|     target does not clear the CURRENT spread by InpMinTpSpread    |
//|     is not taken. In GoldMacdCrossover the spread guard defaults  |
//|     off, on the reasoning that enabling it makes live diverge     |
//|     from the backtest. There is no backtest here to diverge from, |
//|     and the measured numbers say the guard is the whole game.     |
//|                                                                   |
//|  2. THE CHOP FILTER IS THE SIGNAL'S MAIN JOB. Scalpers do not     |
//|     die on one bad trade, they die on forty round trips through   |
//|     a flat market, each paying the spread. InpMinSepAtr refuses   |
//|     to trade when the two EMAs are tangled, which is the state    |
//|     that generates those forty trades.                            |
//|                                                                   |
//|  3. THE DAY IS BOUNDED, NOT JUST THE TRADE. A loss limit, a       |
//|     profit target and a trade cap, all recomputed from deal       |
//|     history rather than held in memory, so a recompile cannot     |
//|     hand back a budget that was already spent.                    |
//|                                                                   |
//| ====================================================================
//| THE SIGNAL, STATED PLAINLY
//| ====================================================================
//| Everything is read from CLOSED bars. Long:                        |
//|                                                                   |
//|   regime   emaFast > emaSlow, and their separation is at least    |
//|            InpMinSepAtr * ATR - a trend, not a tangle             |
//|   pullback RSI was at or below InpRsiPullback on the bar before   |
//|   resume   RSI is above it now                                    |
//|   confirm  the bar closed above emaFast                           |
//|                                                                   |
//| Short is the mirror, with the pullback band at 100-InpRsiPullback.|
//|                                                                   |
//| It buys a dip inside an established uptrend at the moment the dip |
//| stops - not a breakout, and not a reversal. The confirm clause is |
//| what stops it from catching a falling knife: RSI can turn up on a |
//| bar that still closed below the fast EMA, and that bar is a pause |
//| in a decline rather than the end of a pullback.                   |
//|                                                                   |
//| ====================================================================
//| WHY THE BRACKET GOES OUT WITH THE ORDER
//| ====================================================================
//| Open() then ApplyStop() leaves the position naked for a server    |
//| round trip. On a swing stop of $23 that is tolerable. On a scalp  |
//| stop of a few ATR-tenths it is not - the fast move that motivated |
//| the entry is still moving during exactly that window. So the      |
//| bracket is attached to the same request via OpenBracket(), and    |
//| the broker takes the whole thing or none of it.                   |
//|                                                                   |
//| The consequence is that SL/TP - not the bar-close logic - is the  |
//| primary exit. OnClosedBar mostly manages a position the broker    |
//| may already have closed intrabar.                                 |
//|                                                                   |
//| ====================================================================
//| NOTHING IS PERSISTED, EVERYTHING IS DERIVED
//| ====================================================================
//| Same discipline as the other two experts. The risk unit R is      |
//| recovered from the TP the bracket actually placed (R = TP         |
//| distance / InpRewardRisk) rather than remembered, and the extreme |
//| for the trail is replayed from the bars since entry. A recompile  |
//| mid-trade therefore changes nothing about how the position is     |
//| managed, and there is no state file to go stale.                  |
//+------------------------------------------------------------------+
#property copyright "algo trading - GOLDM/XAUUSD engine"
#property link      ""
#property version   "1.00"
#property description "Intraday pullback scalper with an ATR bracket, a mandatory spread"
#property description "cost gate, and daily loss/profit/trade governors. Not a port - no"
#property description "measured backtest stands behind it. Tester first."
#property strict

#include <AlgoGold\ProtectiveExits.mqh>
#include <AlgoGold\Trader.mqh>
#include <AlgoGold\ScalpFilters.mqh>

//+------------------------------------------------------------------+
//| Inputs                                                            |
//+------------------------------------------------------------------+
input group "--- Signal ---"
input int    InpEmaFast          = 21;      // Fast EMA period (trend side)
input int    InpEmaSlow          = 50;      // Slow EMA period (trend direction)
input int    InpRsiPeriod        = 14;      // RSI period
input double InpRsiPullback      = 40.0;    // Long pullback band: RSI <= this, then above
input double InpMinSepAtr        = 0.25;    // CHOP FILTER: min |emaFast-emaSlow| in ATR. 0 = off

input group "--- Bracket (ATR multiples, NOT percentages) ---"
input int    InpAtrPeriod        = 14;      // ATR period
input double InpAtrStopMult      = 1.20;    // Stop distance = this * ATR
input double InpRewardRisk       = 1.50;    // Target = this * stop distance
input int    InpMinStopPoints    = 150;     // Hard floor on the stop, points. 0 = broker level only

input group "--- Cost gate (the one the measurements argue for) ---"
input double InpMinTpSpread      = 4.0;     // Target must clear this many spreads. 0 = OFF (not advised)
input int    InpMaxSpreadPoints  = 60;      // Block entries above this spread, points. 0 = off

input group "--- Position management ---"
input double InpBreakEvenR       = 0.80;    // Move stop to entry at this many R. 0 = off
input double InpBreakEvenLockPts = 20;      // Points of profit locked when it moves. 0 = flat entry
input double InpTrailStartR      = 1.00;    // Arm the ATR trail at this many R. 0 = off
input double InpTrailAtrMult     = 1.00;    // Trail this many ATR behind the extreme
input int    InpMaxBarsInTrade   = 24;      // Time stop, bars. 0 = off

input group "--- Session (SERVER hours - the expert prints the current one on init) ---"
input int    InpSessionStart     = 8;       // Entries allowed from this server hour
input int    InpSessionEnd       = 20;      // ...until this one. Equal values = all day
input int    InpFridayEnd        = 19;      // No entries Friday from here. -1 = off
input bool   InpCloseAtSessionEnd= true;    // Flatten when the window shuts (no overnight swap)

input group "--- Daily governors ---"
input double InpDailyLossLimit   = 0.0;     // Halt for the day at this realised loss. 0 = off
input double InpDailyProfitTarget= 0.0;     // Halt for the day at this realised profit. 0 = off
input int    InpMaxTradesPerDay  = 10;      // Halt after this many entries. 0 = off

input group "--- Execution ---"
input bool   InpUseRiskSizing    = true;    // true: size from InpRiskPercent. false: fixed InpLots
input double InpRiskPercent      = 0.5;     // Percent of BALANCE risked per trade
input double InpLots             = 0.10;    // Fixed volume, MT5 LOTS (used when sizing is off)
input long   InpMagic            = 20260903;// Distinct from 20260901/02 and the Python 20260828
input ulong  InpSlippagePoints   = 20;      // Max deviation, points
input bool   InpAllowNewEntries  = true;    // false = manage open positions only
input string InpComment          = "AlgoGold Scalp"; // Cosmetic only - MT5 overwrites it

//+------------------------------------------------------------------+
//| State                                                             |
//+------------------------------------------------------------------+
CGoldTrader      g_trader;
DayGuard         g_day;
ENUM_TIMEFRAMES  g_tf          = PERIOD_CURRENT;
datetime         g_lastBarTime = 0;

int              g_emaFastH    = INVALID_HANDLE;
int              g_emaSlowH    = INVALID_HANDLE;
int              g_rsiH        = INVALID_HANDLE;
int              g_atrH        = INVALID_HANDLE;

//--- Bars of indicator history the handles need before their values mean
//--- anything. The EMAs dominate; the +5 covers the two-bar RSI comparison and
//--- the shift-1/shift-2 reads on top of it.
int WarmupBars()
  {
   return MathMax(MathMax(InpEmaSlow,InpEmaFast),MathMax(InpRsiPeriod,InpAtrPeriod))*3 + 5;
  }

//+------------------------------------------------------------------+
//| One indicator buffer at shifts 2 and 1.                           |
//|                                                                   |
//| CopyBuffer returns oldest-first, so out[0] is shift 2 and out[1]  |
//| is shift 1. Both are CLOSED bars; shift 0 is still forming and is |
//| never read anywhere in this expert.                               |
//|                                                                   |
//| A short read is a hard failure, not a zero. An indicator that has |
//| not filled yet returns 0.0, and 0.0 is a perfectly plausible RSI  |
//| or EMA separation - it would be read as a signal rather than as   |
//| missing data.                                                     |
//+------------------------------------------------------------------+
bool ReadPair(const int handle,const string name,double &prev,double &last)
  {
   double buf[];
   ArraySetAsSeries(buf,false);
   const int copied = CopyBuffer(handle,0,1,2,buf);
   if(copied<2)
     {
      PrintFormat("%s not ready: CopyBuffer returned %d of 2 (error %d)",
                  name,copied,GetLastError());
      return false;
     }
   prev = buf[0];   // shift 2
   last = buf[1];   // shift 1
   return true;
  }

//+------------------------------------------------------------------+
//| The SL and TP currently on our position.                          |
//|                                                                   |
//| Read back from the broker rather than remembered, so a recompile  |
//| mid-trade recovers the full management state. This expert holds   |
//| at most one position, so the first matching ticket is the answer. |
//+------------------------------------------------------------------+
bool OurLevels(double &sl,double &tp)
  {
   for(int i=PositionsTotal()-1; i>=0; i--)
     {
      if(PositionGetSymbol(i)!=_Symbol)
         continue;
      if(PositionGetInteger(POSITION_MAGIC)!=InpMagic)
         continue;
      sl = PositionGetDouble(POSITION_SL);
      tp = PositionGetDouble(POSITION_TP);
      return true;
     }
   return false;
  }

//+------------------------------------------------------------------+
//| The risk unit R, in price.                                        |
//|                                                                   |
//| Recovered from the target the bracket actually placed rather than |
//| stored: TP distance / InpRewardRisk is by construction the stop   |
//| distance that was used, and it survives a reload. Falls back to   |
//| the live ATR only when the position carries no TP - a position    |
//| adopted at init, or one whose TP a human removed.                 |
//+------------------------------------------------------------------+
double RiskUnit(const GoldPosition &pos,const double atr)
  {
   double sl=0.0, tp=0.0;
   if(OurLevels(sl,tp) && tp>0.0 && InpRewardRisk>0.0)
      return MathAbs(tp-pos.entry)/InpRewardRisk;
   return atr*InpAtrStopMult;
  }

//+------------------------------------------------------------------+
//| Best price seen since entry, replayed from closed bars.           |
//|                                                                   |
//| The same technique RebuildTrail uses, and for the same reason: an |
//| expert is reloaded far too often for a remembered extreme to be   |
//| trusted, and the replay is the identical arithmetic with no state |
//| file behind it.                                                   |
//+------------------------------------------------------------------+
double ExtremeSinceEntry(const GoldPosition &pos)
  {
   double best = pos.entry;
   const int firstBar = iBarShift(_Symbol,g_tf,pos.openTime,false);
   for(int shift=MathMax(firstBar,1); shift>=1; shift--)
     {
      if(pos.side==POSITION_TYPE_BUY)
         best = MathMax(best,iHigh(_Symbol,g_tf,shift));
      else
         best = MathMin(best,iLow(_Symbol,g_tf,shift));
     }
   return best;
  }

//+------------------------------------------------------------------+
//| Tighten the stop to `wanted`, never loosen it.                    |
//|                                                                   |
//| ApplyStop will happily move a stop further from price, which for  |
//| a trail is the one direction that must never happen: a bar whose  |
//| extreme retreated would otherwise widen the risk on a position    |
//| already in profit. The monotonicity is enforced here, before the  |
//| call, rather than inside the shared ApplyStop - the swing experts |
//| legitimately move their stop both ways as the trail arms.         |
//+------------------------------------------------------------------+
bool TightenStop(const GoldPosition &pos,const double requested,const string why)
  {
   double sl=0.0, tp=0.0;
   if(!OurLevels(sl,tp))
      return false;
   if(requested<=0.0)
      return false;

   const double point = g_trader.Point();
   const double bid   = SymbolInfoDouble(_Symbol,SYMBOL_BID);
   const double ask   = SymbolInfoDouble(_Symbol,SYMBOL_ASK);
   if(bid<=0.0 || ask<=0.0)
      return false;

//--- Clamp to the broker's stops band HERE, before the monotonicity test.
//--- ApplyStop performs the same clamp internally, but it does so after this
//--- function has decided the move is a tightening - and the clamp always
//--- pushes the level AWAY from price. A trail level inside the band would
//--- therefore be checked as tighter and then applied looser, which is the one
//--- direction a trail must never move. Clamping first makes the test and the
//--- order agree. The second clamp inside ApplyStop is then a no-op.
   const double band = (double)SymbolInfoInteger(_Symbol,SYMBOL_TRADE_STOPS_LEVEL)*point;
   double wanted = requested;
   if(pos.side==POSITION_TYPE_BUY)
      wanted = MathMin(wanted,bid-band);
   else
      wanted = MathMax(wanted,ask+band);
   wanted = NormalizeDouble(wanted,g_trader.Digits());

   if(wanted<=0.0)
      return false;

   if(sl>0.0)
     {
      if(pos.side==POSITION_TYPE_BUY  && wanted <= sl+point/2.0)
         return false;
      if(pos.side==POSITION_TYPE_SELL && wanted >= sl-point/2.0)
         return false;
     }

   if(MathAbs(wanted-requested) > point/2.0)
      PrintFormat("%s: level %s is inside the %s stops band, using %s",why,
                  DoubleToString(requested,g_trader.Digits()),
                  DoubleToString(band,g_trader.Digits()),
                  DoubleToString(wanted,g_trader.Digits()));

   PrintFormat("%s: stop %s -> %s",why,
               DoubleToString(sl,g_trader.Digits()),
               DoubleToString(wanted,g_trader.Digits()));
   return g_trader.ApplyStop(wanted);
  }

//+------------------------------------------------------------------+
//| Init                                                              |
//+------------------------------------------------------------------+
int OnInit()
  {
   g_tf = (ENUM_TIMEFRAMES)_Period;
   DayGuardReset(g_day);

   if(InpEmaFast>=InpEmaSlow)
     {
      PrintFormat("FATAL: the fast EMA must be shorter than the slow one, got %d and %d",
                  InpEmaFast,InpEmaSlow);
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpEmaFast<1 || InpRsiPeriod<2 || InpAtrPeriod<1)
     {
      Print("FATAL: EMA period must be >=1, RSI >=2, ATR >=1");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpRsiPullback<=0.0 || InpRsiPullback>=50.0)
     {
      PrintFormat("FATAL: InpRsiPullback must be between 0 and 50 exclusive, got %.1f. "
                  "The short band is derived as 100-%.1f, so a value at or above 50 "
                  "would make the two bands overlap and fire both sides on one bar.",
                  InpRsiPullback,InpRsiPullback);
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpRewardRisk<=0.0 || InpAtrStopMult<=0.0)
     {
      Print("FATAL: InpRewardRisk and InpAtrStopMult must both be positive");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpUseRiskSizing && InpRiskPercent<=0.0)
     {
      Print("FATAL: risk sizing is on but InpRiskPercent is not positive");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpSessionStart<0 || InpSessionStart>23 || InpSessionEnd<0 || InpSessionEnd>23)
     {
      Print("FATAL: session hours must be 0..23");
      return INIT_PARAMETERS_INCORRECT;
     }

//--- Reuses the shared preflight purely for the magic-collision check and the
//--- algo-trading-enabled warnings; the percentage arguments are not used by
//--- this expert and are passed as zero.
   if(!GoldPreflight(InpMagic,0.0,0.0,0.0))
      return INIT_PARAMETERS_INCORRECT;

   if(!g_trader.Init(_Symbol,InpMagic,InpLots,InpSlippagePoints,InpComment))
      return INIT_FAILED;

   g_emaFastH = iMA(_Symbol,g_tf,InpEmaFast,0,MODE_EMA,PRICE_CLOSE);
   g_emaSlowH = iMA(_Symbol,g_tf,InpEmaSlow,0,MODE_EMA,PRICE_CLOSE);
   g_rsiH     = iRSI(_Symbol,g_tf,InpRsiPeriod,PRICE_CLOSE);
   g_atrH     = iATR(_Symbol,g_tf,InpAtrPeriod);
   if(g_emaFastH==INVALID_HANDLE || g_emaSlowH==INVALID_HANDLE ||
      g_rsiH==INVALID_HANDLE     || g_atrH==INVALID_HANDLE)
     {
      Print("FATAL: could not create an indicator handle, error ",GetLastError());
      return INIT_FAILED;
     }

//--- Unlike GoldMacdCrossover this expert does NOT hand-roll its indicators.
//--- That expert avoids iMACD because it must agree bar-for-bar with the EMA
//--- seeding in algo/pricing/indicators.py. Nothing here has a Python
//--- counterpart to agree with, so the terminal's own indicators are the right
//--- choice: they are what the Strategy Tester and the chart will show.
   const int available = Bars(_Symbol,g_tf);
   if(available < WarmupBars())
      PrintFormat("WARNING: %d bars of history, below the %d-bar warmup. No entry will be "
                  "taken until the indicators have filled.",available,WarmupBars());

   GoldTimeframeNote(g_tf);
   if(InpMinTpSpread<=0.0)
      Print("WARNING: InpMinTpSpread is 0, so the cost gate is OFF. The measured M15 "
            "numbers in mt5/README.md are what trading through the spread looks like "
            "without it.");

   MqlDateTime now;
   TimeToStruct(TimeCurrent(),now);
   PrintFormat("EMA(%d/%d) + RSI(%d) pullback on %s %s | stop %.2f*ATR(%d), target %.2fR | "
               "cost gate %.1fx spread | session %02d:00-%02d:00 server (it is %02d:%02d "
               "there now) | magic %d",
               InpEmaFast,InpEmaSlow,InpRsiPeriod,_Symbol,EnumToString(g_tf),
               InpAtrStopMult,InpAtrPeriod,InpRewardRisk,InpMinTpSpread,
               InpSessionStart,InpSessionEnd,now.hour,now.min,(int)InpMagic);

   if(InpUseRiskSizing)
      PrintFormat("sizing: %.2f%% of balance %.2f = %.2f per trade, lots computed per "
                  "trade from the ATR stop",
                  InpRiskPercent,AccountInfoDouble(ACCOUNT_BALANCE),
                  AccountInfoDouble(ACCOUNT_BALANCE)*InpRiskPercent/100.0);

   const GoldPosition pos = g_trader.Snapshot();
   if(pos.exists)
      PrintFormat("adopted an existing %s position of %.2f lots at %.2f (magic %d). Its "
                  "own SL/TP stand; management resumes from the broker's levels.",
                  (pos.side==POSITION_TYPE_BUY?"BUY":"SELL"),pos.volume,pos.entry,
                  (int)InpMagic);

   g_lastBarTime = iTime(_Symbol,g_tf,0);
   return INIT_SUCCEEDED;
  }

//+------------------------------------------------------------------+
//| Deinit                                                            |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   if(g_emaFastH!=INVALID_HANDLE) IndicatorRelease(g_emaFastH);
   if(g_emaSlowH!=INVALID_HANDLE) IndicatorRelease(g_emaSlowH);
   if(g_rsiH!=INVALID_HANDLE)     IndicatorRelease(g_rsiH);
   if(g_atrH!=INVALID_HANDLE)     IndicatorRelease(g_atrH);

   PrintFormat("stopped (reason %d). Open positions are LEFT AS THEY ARE, with their "
               "broker-side SL and TP still in force - removing an expert is not a "
               "flatten instruction.",reason);
  }

//+------------------------------------------------------------------+
//| Tick: nothing happens except on a bar close.                      |
//|                                                                   |
//| The bracket is broker-side, so intrabar exits are handled by the  |
//| server and need no tick handling here. Everything this expert     |
//| decides for itself, it decides on a closed bar.                   |
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
//| The whole strategy, once per closed bar.                          |
//+------------------------------------------------------------------+
void OnClosedBar()
  {
   double emaFastPrev,emaFast, emaSlowPrev,emaSlow, rsiPrev,rsi, atrPrev,atr;
   if(!ReadPair(g_emaFastH,"fast EMA",emaFastPrev,emaFast)) return;
   if(!ReadPair(g_emaSlowH,"slow EMA",emaSlowPrev,emaSlow)) return;
   if(!ReadPair(g_rsiH,"RSI",rsiPrev,rsi))                  return;
   if(!ReadPair(g_atrH,"ATR",atrPrev,atr))                  return;

   const double close = iClose(_Symbol,g_tf,1);
   if(close<=0.0 || atr<=0.0)
      return;

   const datetime now = TimeCurrent();
   const GoldPosition pos = g_trader.Snapshot();
   if(pos.opposing)
      Print("WARNING: opposing tickets on this symbol under our magic - both legs pay "
            "swap and spread while netting to a smaller exposure.");

//--- 1. Manage what is held. This runs before any gate: a position must never
//---    be left unmanaged because the session shut or the day halted.
   if(pos.exists)
     {
      if(ManageOpenPosition(pos,atr,now))
         return;               // closed this bar; no re-entry on the same bar
     }

//--- 2. Governors, re-read from deal history every bar.
   DayGuardRefresh(g_day,_Symbol,InpMagic,now);

   if(pos.exists)
      return;                  // one position at a time

   if(!InpAllowNewEntries)
      return;

   string why = "";
   if(!SessionAllowsEntry(now,InpSessionStart,InpSessionEnd,InpFridayEnd,why))
      return;                  // silent: this is the common case, not an event

   if(!DayGuardAllowsEntry(g_day,InpDailyLossLimit,InpDailyProfitTarget,InpMaxTradesPerDay))
      return;                  // DayGuardAllowsEntry logs the halt once

//--- 3. Regime. The chop filter is the main defence against paying the spread
//---    forty times in a flat market.
   const double separation = MathAbs(emaFast-emaSlow);
   if(InpMinSepAtr>0.0 && separation < InpMinSepAtr*atr)
      return;

   const bool up   = (emaFast > emaSlow);
   const bool down = (emaFast < emaSlow);
   if(!up && !down)
      return;

//--- 4. Pullback, then resumption, then confirmation.
   const double shortBand = 100.0-InpRsiPullback;
   const bool longSignal  = up   && rsiPrev<=InpRsiPullback && rsi>InpRsiPullback && close>emaFast;
   const bool shortSignal = down && rsiPrev>=shortBand      && rsi<shortBand      && close<emaFast;
   if(!longSignal && !shortSignal)
      return;

//--- 5. The live spread guard, before the bracket is even priced.
   if(InpMaxSpreadPoints>0)
     {
      const long spreadPoints = SymbolInfoInteger(_Symbol,SYMBOL_SPREAD);
      if(spreadPoints>InpMaxSpreadPoints)
        {
         PrintFormat("entry suppressed: spread %d points is above the %d-point limit",
                     (int)spreadPoints,InpMaxSpreadPoints);
         return;
        }
     }

//--- 6. The bracket, and with it the cost gate.
   const ScalpBracket bracket = BuildBracket(_Symbol,atr,InpAtrStopMult,InpRewardRisk,
                                             InpMinStopPoints,InpMinTpSpread);
   if(!bracket.valid)
     {
      PrintFormat("entry suppressed: %s",bracket.reason);
      return;
     }
   if(bracket.reason!="")
      Print(bracket.reason);

//--- 7. Size it.
   double volume = g_trader.Lots();
   if(InpUseRiskSizing)
     {
      const double riskMoney = AccountInfoDouble(ACCOUNT_BALANCE)*InpRiskPercent/100.0;
      volume = g_trader.LotsForRisk(riskMoney,bracket.stopDistance);
      if(volume<=0.0)
        {
         Print("entry suppressed: risk sizing produced no tradable volume. Not falling "
               "back to a default size - a sizing failure must not become a position.");
         return;
        }
     }

   const ENUM_POSITION_TYPE side = longSignal ? POSITION_TYPE_BUY : POSITION_TYPE_SELL;
   const string reason = StringFormat("pullback %s: rsi %.1f -> %.1f, close %s %s emaFast, "
                                      "ema sep %.*f (%.2f ATR)",
                                      (longSignal?"long":"short"),rsiPrev,rsi,
                                      DoubleToString(close,g_trader.Digits()),
                                      (longSignal?"above":"below"),
                                      g_trader.Digits(),separation,separation/atr);

   g_trader.OpenBracket(side,volume,bracket.stopDistance,bracket.takeDistance,reason);
  }

//+------------------------------------------------------------------+
//| Manage a held position. Returns true if it was closed.            |
//|                                                                   |
//| Order is deliberate: the two reasons to LEAVE are tested before   |
//| the two ways to tighten, so a bar that both ends the session and  |
//| would have moved the stop simply exits instead of adjusting a     |
//| position it is about to close.                                    |
//+------------------------------------------------------------------+
bool ManageOpenPosition(const GoldPosition &pos,const double atr,const datetime now)
  {
//--- (a) Session end. An intraday strategy holding overnight is a different
//---     strategy: it pays swap and it carries the gap the stop cannot cover.
   string why = "";
   if(InpCloseAtSessionEnd && !SessionAllowsEntry(now,InpSessionStart,InpSessionEnd,
                                                  InpFridayEnd,why))
     {
      g_trader.CloseAll(StringFormat("session end (%s) - intraday positions are not "
                                     "carried overnight",why));
      return true;
     }

//--- (b) Time stop. A scalp that has not resolved in InpMaxBarsInTrade bars is
//---     no longer the trade that was entered; it is a small position sitting in
//---     the market paying financing while it waits to become one.
   if(InpMaxBarsInTrade>0)
     {
      const int barsHeld = iBarShift(_Symbol,g_tf,pos.openTime,false);
      if(barsHeld>=InpMaxBarsInTrade)
        {
         g_trader.CloseAll(StringFormat("time stop: %d bars held, limit %d",
                                        barsHeld,InpMaxBarsInTrade));
         return true;
        }
     }

   const double R = RiskUnit(pos,atr);
   if(R<=0.0)
      return false;

//--- How far the position has actually travelled, measured on the side it will
//--- be closed at - a long is marked against the bid, a short against the ask.
   const double bid = SymbolInfoDouble(_Symbol,SYMBOL_BID);
   const double ask = SymbolInfoDouble(_Symbol,SYMBOL_ASK);
   if(bid<=0.0 || ask<=0.0)
      return false;
   const double travelled = (pos.side==POSITION_TYPE_BUY) ? (bid-pos.entry) : (pos.entry-ask);
   const double rMultiple = travelled/R;

//--- (c) The ATR trail, once it has earned the right to be armed. Tested before
//---     breakeven because once both apply the trail is always the tighter of
//---     the two, and applying breakeven after it would loosen the stop.
   if(InpTrailStartR>0.0 && InpTrailAtrMult>0.0 && rMultiple>=InpTrailStartR)
     {
      const double extreme = ExtremeSinceEntry(pos);
      const double level = (pos.side==POSITION_TYPE_BUY)
                           ? extreme-InpTrailAtrMult*atr
                           : extreme+InpTrailAtrMult*atr;
      if(TightenStop(pos,NormalizeDouble(level,g_trader.Digits()),
                     StringFormat("trail armed at %.2fR",rMultiple)))
         return false;
     }

//--- (d) Breakeven. InpBreakEvenLockPts of profit rather than a flat entry,
//---     because a stop exactly at entry still loses the spread and the
//---     commission - it is a scratch in price and a small loss in money.
   if(InpBreakEvenR>0.0 && rMultiple>=InpBreakEvenR)
     {
      const double lock = InpBreakEvenLockPts*g_trader.Point();
      const double level = (pos.side==POSITION_TYPE_BUY)
                           ? pos.entry+lock
                           : pos.entry-lock;
      TightenStop(pos,NormalizeDouble(level,g_trader.Digits()),
                  StringFormat("breakeven at %.2fR",rMultiple));
     }

   return false;
  }
