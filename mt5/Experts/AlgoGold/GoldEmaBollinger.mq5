//+------------------------------------------------------------------+
//| GoldEmaBollinger.mq5                                             |
//|                                                                  |
//| A port of algo/strategy/ema_bb.py (strategy_id                   |
//| "xauusd_ema_bb_v1") to a MetaTrader 5 expert advisor.            |
//|                                                                  |
//| ====================================================================
//| READ THIS BEFORE ATTACHING IT TO ANYTHING
//| ====================================================================
//| The strategy this ports HAS NO MEASURED EDGE. That is not a       |
//| suspicion, it is two decision entries:                            |
//|                                                                   |
//| D-152 ran both readings across three timeframes and three         |
//| windows. `pullback` was below break-even in EVERY window on M30   |
//| (PF 0.60 / 0.44 / 0.92), and the cells it "won" had 7-16 trades.  |
//| `breakout` cleared PF 1.0 in seven of nine cells - and still lost |
//| to doing nothing: buy-and-hold made $116,459 over the same        |
//| windows against the strategy's $69,458.                           |
//|                                                                   |
//| D-153 took the one promising slice - long-only, since the shorts  |
//| lost in eight of nine cells - PRE-REGISTERED it, and rejected it  |
//| on seven years of data the first study never saw: profit factor   |
//| 1.04 on H1 and 0.99 on M30, 48% and 43% of folds positive, while  |
//| buy-and-hold returned +$195,893 at a third of the drawdown.       |
//|                                                                   |
//| So this expert exists because it was asked for, and it is built   |
//| properly. It is not evidence that the rules work, and nothing     |
//| here should be read as a recommendation to trade them. Demo.      |
//|                                                                   |
//| ====================================================================
//| ONE EXPERT, TWO OPPOSITE STRATEGIES
//| ====================================================================
//| "The 50 EMA and Bollinger band strategy" names two setups that    |
//| trade in OPPOSITE DIRECTIONS on the same bar, and which one is    |
//| meant is almost never stated:                                     |
//|                                                                   |
//|   InpMode = MODE_PULLBACK  fades a band touch. Price above the    |
//|     50 EMA, a close back UP through the lower band after being    |
//|     below it, exiting at the middle band.                         |
//|   InpMode = MODE_BREAKOUT  follows one. Price above the 50 EMA    |
//|     and a close OUTSIDE the upper band, held while price walks    |
//|     the band, exited when it closes back inside.                  |
//|                                                                   |
//| The Python keeps both behind one `mode` for the reason its module |
//| docstring gives - picking one silently would be choosing the      |
//| answer before measuring it - and so does this.                    |
//|                                                                   |
//| ====================================================================
//| WHAT THE PORT PRESERVES EXACTLY
//| ====================================================================
//| - THE EMA IS STEPPED FIRST, before the protective-exit check and  |
//|   before the warmup gate. `MacdCrossover` has a known divergence  |
//|   here (mt5/README.md records it): when an exit fires it returns  |
//|   before updating its histogram, so the next bar compares against |
//|   the value from two bars ago. That is preserved in the MACD port |
//|   because a measured backtest depends on it. This strategy has no |
//|   such history, and `ema_bb.py` deliberately does not reproduce   |
//|   the wart - so neither does this. A bar on which a stop fires is |
//|   still a bar the EMA saw.                                        |
//|                                                                   |
//| - POPULATION STANDARD DEVIATION, divide by N, not N-1. That is    |
//|   what `indicators.bollinger()` pins and what MT5's own iBands    |
//|   computes. The two forms differ by sqrt(N/(N-1)) - about 2.6% of |
//|   the half-width at period 20 - which is small everywhere except  |
//|   on the touches this indicator exists to flag.                   |
//|                                                                   |
//| - THE PULLBACK NEEDS THE RE-ENTRY, not the excursion. The rule is |
//|   `prev_close < prev_lower <= close`: price was below the band on |
//|   the previous bar and is at or above it now. Entering on the     |
//|   first close BELOW the band would be catching the knife the rule |
//|   exists to wait out, and is the single most likely way to get    |
//|   this strategy wrong.                                            |
//|                                                                   |
//| - BOTH MODES EXIT ON THE MIDDLE BAND, from opposite sides. The    |
//|   pullback has reached its target there; the breakout has lost    |
//|   the expansion that justified it. One comparison, two meanings.  |
//|                                                                   |
//| ====================================================================
//| THE GIVE-BACK TRAIL (InpGivebackFrac) - ADDED, MEASURED, REJECTED |
//| ====================================================================
//| Every exit above is a give-up: the flat stop, or the middle band. |
//| InpTrailPct existed but ships at 0, so a winner ran to the middle |
//| band and handed back whatever it had made getting there.          |
//|                                                                   |
//| InpGivebackFrac closes a position once it has surrendered that     |
//| fraction of the best unrealised profit it ever showed - at 0.5 the |
//| level sits halfway between entry and the peak, and rises with it.  |
//|                                                                   |
//| It is NOT InpTrailPct with a different number. InpTrailPct gives   |
//| back a percentage of the PEAK PRICE, so it scales with gold; this  |
//| gives back a fraction of the BANKED MOVE, so it scales with how    |
//| well the trade went. Read the header of ProtectiveExits.mqh for    |
//| how the two resolve when both cross on one bar.                    |
//|                                                                   |
//| IT IS A FRACTION, NOT A PERCENT. 0.5 is half. Entering 50 is       |
//| rejected at init rather than clamped, because it would otherwise   |
//| behave as a trail that exits at cost with nothing saying why.      |
//|                                                                   |
//| ---- IT IS ON AT 0.5, BY REQUEST AND AGAINST THE MEASUREMENT ----  |
//| The default was 0. It was turned on deliberately after D-154 was  |
//| reported, with the activation gate raised from 0.25% to 2% at the |
//| same time - which is the difference between the setting that       |
//| measured as destructive and the one that measured as inert. Read   |
//| the numbers below as what to expect, not as a recommendation.      |
//|                                                                   |
//| AT A 2% GATE THIS TRAIL BARELY ACTS. In the sharpest cell it fired |
//| three times in fifty-three trades and finished $556 from baseline  |
//| - noise. On an M1 chart it will essentially never arm: 2% of gold  |
//| near 4,400 is about $88, and the M1 round trips this expert is     |
//| currently taking last six to twelve minutes and a few dollars. The |
//| middle band will keep taking the exits.                           |
//|                                                                   |
//| ---- WHAT D-154 MEASURED, WHICH STILL STANDS ----                  |
//| scripts/measure_ema_bb_giveback_xauusd.py ran both modes across    |
//| three timeframes, three windows and four activation gates, each    |
//| cell against its own giveback-off baseline. The trail beat that    |
//| baseline in 3 OF 72 CELLS, and all three are degenerate: two are   |
//| +$556 and +$806 where it fired three or four times out of 245      |
//| trades, and the third merely loses less (-$17,309 vs -$21,912) in  |
//| a cell where both readings are heavy losses. Everywhere else it is |
//| worse, often catastrophically - H1 breakout over 2026.06-08 goes   |
//| from +$37,725 at PF 1.54 to -$70,636 at PF 0.10.                   |
//|                                                                   |
//| The reason is arithmetic, not fit. At frac 0.5 a trail armed at    |
//| `a` first fires at a/2 of profit while the flat stop still lets a  |
//| loser run to InpStopLossPct. At a 0.25% gate against a 0.5% stop   |
//| that is a 1:4 reward-to-risk floor on every trade it touches, and  |
//| no entry rule survives it. The results are monotone in the gate    |
//| for that reason: the wider it is set the closer to baseline it     |
//| lands, because it fires less. ITS BEST MEASURED BEHAVIOUR IS NOT   |
//| FIRING AT ALL.                                                     |
//|                                                                   |
//| The input is kept rather than deleted, for the reason InpLongOnly  |
//| is kept: the next person to notice this expert hands its winners   |
//| back should find the falsification attached to the fix. A give-    |
//| back trail is only coherent when it arms well ABOVE the stop       |
//| distance - and this data says even then the middle band was        |
//| already the better exit. D-152 and D-153 predate it entirely and   |
//| their scripts pin it to 0.                                         |
//|                                                                   |
//| - IT DOES NOT REVERSE IN ONE STEP. Entries are only taken flat.   |
//|                                                                   |
//| ====================================================================
//| WHAT THE PORT DELIBERATELY CHANGES, AND WHY
//| ====================================================================
//| IT DOES NOT USE iBands() OR iMA(). Both would be handles whose    |
//| warm-up this expert does not control, and the EMA in particular   |
//| is path-dependent: `indicators.ema()` seeds with the FIRST close  |
//| (pandas `adjust=False`), while MT5's iMA seeds an EMA with an SMA |
//| of the first `period` values. Those differ, and a signal here     |
//| disagreeing with the Python about where the 50 EMA is would       |
//| defeat the point of a port. The EMA is stepped recursively in the |
//| expert, and the bands are computed from the same closes in the    |
//| same 64-bit floating point.                                       |
//|                                                                   |
//| INDICATOR STATE IS SEEDED FROM HISTORY, NOT PERSISTED. Same       |
//| reasoning as GoldMacdCrossover: an expert is reloaded far more    |
//| often than a Python process, so it replays InpSeedBars closed     |
//| bars forward on every init. Deterministic, no state file. For a   |
//| 50-period EMA alpha is 2/51 = 0.0392, so after 1,000 bars the     |
//| seeding residue is of order e^-40 - many orders of magnitude      |
//| below a $0.01 tick.                                               |
//|                                                                   |
//| WARMUP IS ema_period + bb_period + 2, NOT ema_period. Seeded with |
//| the first close, a 50-period EMA still carries about 13% of that  |
//| seed after 50 bars. The Python's own docstring works the residue  |
//| out and lands on 72; this uses the identical expression.          |
//+------------------------------------------------------------------+
#property copyright "algo trading - GOLDM/XAUUSD engine"
#property link      ""
#property version   "1.00"
#property description "50 EMA + Bollinger on XAUUSD, ported from algo/strategy/ema_bb.py. NO MEASURED EDGE - see D-152/D-153."
#property strict

//--- ProtectiveExits FIRST: Trader.mqh's RebuildTrail takes a TrailState,
//--- which ProtectiveExits.mqh declares, so Trader.mqh does not compile on
//--- its own. Reordering these reports errors inside Trader.mqh and none here.
#include <AlgoGold\ProtectiveExits.mqh>
#include <AlgoGold\Trader.mqh>
#include <AlgoGold\Dashboard.mqh>

enum EmaBbMode
  {
   MODE_PULLBACK = 0,  // Fade the band: close back INSIDE, with the trend
   MODE_BREAKOUT = 1   // Follow the band: close OUTSIDE, with the trend
  };

//+------------------------------------------------------------------+
//| Inputs. Every default is the Python default for the same name.   |
//+------------------------------------------------------------------+
input group "--- Signal (algo/strategy/ema_bb.py) ---"
input EmaBbMode InpMode          = MODE_BREAKOUT; // `mode`. D-152: pullback was worse
input int    InpEmaPeriod        = 50;      // `ema_period`, the trend filter
input int    InpBbPeriod         = 20;      // `bb_period`, the band's SMA length
input double InpBbStdev          = 2.0;     // `bb_stdev`. POPULATION sigma, as iBands uses
input bool   InpLongOnly         = false;   // `long_only`. D-153 pre-registered and REJECTED this
input int    InpSeedBars         = 1000;    // Closed bars replayed on init to settle the EMA

input group "--- Protective exits (percent of price, NOT points) ---"
input double InpStopLossPct      = 0.5;     // Flat stop, % of entry. 0 disables
input double InpTrailActivationPct = 2.0;   // Profit % at which BOTH trails arm. ~$88 on gold near 4,400
input double InpTrailPct         = 0.0;     // Trail distance, % behind peak. 0 disables
input double InpGivebackFrac     = 0.5;     // Give-back trail: FRACTION of peak profit surrendered. ON by request, against D-154 - see the header. 0 disables

input group "--- Targets and alternative stop units (money > points > percent) ---"
//--- Three ways to say the same distance, resolved in that order, exactly as
//--- GoldTrendlineBreakout resolves them. Money is first because it is the only
//--- one that still means the same thing after InpLots changes; percent is last
//--- because it is what the Python counterpart uses, and so what every measured
//--- study was run on. 0 at every level means that exit is off.
input double InpTakeProfitPct    = 0.0;     // Target, % of entry. 0 disables. NO Python counterpart - unmeasured
input int    InpStopLossPoints   = 0;       // Stop in POINTS. >0 overrides InpStopLossPct
input int    InpTakeProfitPoints = 0;       // Target in POINTS. >0 overrides InpTakeProfitPct
input double InpStopLossMoney    = 0.0;     // Stop as LOSS in account currency. Overrides points and percent
input double InpTakeProfitMoney  = 0.0;     // Target as PROFIT in account currency. Overrides points and percent
input double InpBreakEvenPct     = 0.0;     // Move the stop to entry once this profit % is reached. 0 = off
input int    InpBreakEvenLockPts = 0;       // Points of profit locked when it moves. 0 = flat entry

input group "--- Session and daily governors (SERVER hours) ---"
//--- The expert prints the current server hour on init, because "server hour"
//--- is not the wall clock and guessing it is how a session filter ends up
//--- three hours out with nobody noticing.
input int    InpSessionStartHour = 0;       // Entries allowed from this server hour. start==end means all day
input int    InpSessionEndHour   = 0;       // ...until this one. A window may wrap midnight
input bool   InpCloseAtSessionEnd = false;  // Flatten when the window shuts, so nothing carries swap overnight
input double InpDailyLossLimit   = 0.0;     // Halt NEW entries for the day at this realised loss. 0 = off
input double InpDailyProfitTarget = 0.0;    // Halt NEW entries for the day at this realised profit. 0 = off
input int    InpMaxTradesPerDay  = 0;       // Entry cap per day. 0 = off

input group "--- Execution ---"
input double InpLots             = 0.05;    // Volume in MT5 LOTS (1.00 = 100 oz)
input long   InpMagic            = 20260906;// Distinct from 20260828/01/02/03/04, and 05 (Camarilla)
input ulong  InpSlippagePoints   = 30;      // Max deviation, points
input int    InpMaxSpreadPoints  = 0;       // Block NEW entries above this spread. 0 = off
input bool   InpAllowNewEntries  = true;    // false = manage open positions only
input bool   InpShowDashboard    = true;    // Draw the on-chart panel
input int    InpPanelX           = 12;      // Panel X, pixels from the left
input int    InpPanelY           = 112;     // Panel Y. 112 clears MT5's one-click trading widget
input int    InpPanelWidth       = 260;     // Panel width, pixels
input string InpComment          = "AlgoGold EMA/BB"; // Cosmetic only - MT5 overwrites it

//+------------------------------------------------------------------+
//| State                                                            |
//+------------------------------------------------------------------+
CGoldTrader     g_trader;
CGoldDashboard  g_dash;
TrailState      g_trail;
ENUM_TIMEFRAMES g_tf          = PERIOD_CURRENT;
datetime        g_lastBarTime = 0;
datetime        g_lastPaint   = 0;

double          g_ema         = 0.0;
bool            g_hasEma      = false;
int             g_barsSeen    = 0;

//+------------------------------------------------------------------+
//| Bars before a signal means anything.                             |
//| ema_bb.py: ema_period + bb_period + 2. A residue argument, not   |
//| a tidy number - see the header.                                   |
//+------------------------------------------------------------------+
int WarmupBars()
  {
   return InpEmaPeriod + InpBbPeriod + 2;
  }

//+------------------------------------------------------------------+
//| One recursive EMA step, seeded with the first close.             |
//+------------------------------------------------------------------+
double EmaStep(const double close,const double previous,const bool has)
  {
   if(!has)
      return close;
   const double alpha = 2.0/(InpEmaPeriod+1.0);
   return alpha*close + (1.0-alpha)*previous;
  }

//+------------------------------------------------------------------+
//| Bollinger bands over the `InpBbPeriod` closes ENDING at `shift`. |
//|                                                                  |
//| Population sigma. Returns false when there is not enough history, |
//| rather than averaging whatever exists - an SMA of three closes is |
//| not a warming-up 20-period SMA, it is a different statistic.      |
//+------------------------------------------------------------------+
bool BandsAt(const int shift,double &middle,double &upper,double &lower)
  {
   if(Bars(_Symbol,g_tf) < shift+InpBbPeriod)
      return false;

   double sum = 0.0;
   for(int i=0; i<InpBbPeriod; i++)
      sum += iClose(_Symbol,g_tf,shift+i);
   middle = sum/InpBbPeriod;

   double variance = 0.0;
   for(int i=0; i<InpBbPeriod; i++)
     {
      const double d = iClose(_Symbol,g_tf,shift+i) - middle;
      variance += d*d;
     }
   variance /= InpBbPeriod;              // POPULATION, divide by N

   const double sd = MathSqrt(variance);
   upper = middle + InpBbStdev*sd;
   lower = middle - InpBbStdev*sd;
   return true;
  }

//+------------------------------------------------------------------+
//| Replay closed bars forward so the EMA arrives settled.           |
//+------------------------------------------------------------------+
bool SeedIndicators()
  {
   const int available = Bars(_Symbol,g_tf);
   if(available < WarmupBars()+2)
     {
      PrintFormat("FATAL: only %d bars on this chart, need at least %d. Scroll the "
                  "chart back or pick a faster timeframe.",available,WarmupBars()+2);
      return false;
     }

   const int seed = MathMin(InpSeedBars,available-2);
   g_ema      = 0.0;
   g_hasEma   = false;
   g_barsSeen = 0;

   for(int shift=seed; shift>=1; shift--)
     {
      const double close = iClose(_Symbol,g_tf,shift);
      g_ema    = EmaStep(close,g_ema,g_hasEma);
      g_hasEma = true;
      g_barsSeen++;
     }

   PrintFormat("seeded from %d closed bars: EMA(%d) = %.2f, %d bars seen (warmup %d)",
               seed,InpEmaPeriod,g_ema,g_barsSeen,WarmupBars());
   return true;
  }

//+------------------------------------------------------------------+
//| Realised P&L for OUR magic since `from`.                         |
//|                                                                   |
//| Deal history, not position history: a closed position leaves no    |
//| PositionGet* to read, and the deals are where profit, swap and     |
//| commission are actually recorded. All three are summed - a "profit" |
//| that ignores the commission it cost to earn is not the number      |
//| anyone means by today's P&L.                                       |
//|                                                                   |
//| Filtered by symbol AND magic, so a panel on one chart never counts  |
//| another expert's trades, or this expert's on a different symbol.    |
//+------------------------------------------------------------------+
double RealisedSince(const datetime from)
  {
   if(!HistorySelect(from,TimeCurrent()+86400))
      return 0.0;
   double sum = 0.0;
   const int total = HistoryDealsTotal();
   for(int i=0; i<total; i++)
     {
      const ulong ticket = HistoryDealGetTicket(i);
      if(ticket==0)
         continue;
      if(HistoryDealGetString(ticket,DEAL_SYMBOL)!=_Symbol)
         continue;
      if(HistoryDealGetInteger(ticket,DEAL_MAGIC)!=InpMagic)
         continue;
      sum += HistoryDealGetDouble(ticket,DEAL_PROFIT)
             + HistoryDealGetDouble(ticket,DEAL_SWAP)
             + HistoryDealGetDouble(ticket,DEAL_COMMISSION);
     }
   return sum;
  }

//+------------------------------------------------------------------+
//| Floating P&L across every ticket under our magic on this symbol. |
//| Swap included: an open position's carry is money already spent.   |
//+------------------------------------------------------------------+
double FloatingPnl(void)
  {
   double floating = 0.0;
   for(int i=PositionsTotal()-1; i>=0; i--)
     {
      if(PositionGetSymbol(i)!=_Symbol)
         continue;
      if(PositionGetInteger(POSITION_MAGIC)!=InpMagic)
         continue;
      floating += PositionGetDouble(POSITION_PROFIT)+PositionGetDouble(POSITION_SWAP);
     }
   return floating;
  }

//+------------------------------------------------------------------+
//| Account currency gained per 1.0 of PRICE movement, at InpLots.    |
//|                                                                   |
//| Tick value over tick size is money per price unit per broker lot;  |
//| times the lot size gives it for the position this expert opens.    |
//| Returns 0 when the symbol reports nothing usable, and every caller  |
//| treats that as "fall through to the next unit" rather than         |
//| inventing a distance out of a zero.                                |
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

//+------------------------------------------------------------------+
//| The stop distance as a PERCENT of refPrice, whichever unit the    |
//| inputs expressed it in. Money, then points, then percent.          |
//|                                                                   |
//| Everything downstream - ProtectiveExits, the give-back trail, the  |
//| panel - is written in percent, so the conversion happens once here  |
//| instead of at each of those call sites.                            |
//+------------------------------------------------------------------+
double EffectiveStopPct(const double refPrice)
  {
   if(InpStopLossMoney>0.0 && refPrice>0.0)
     {
      const double perPrice = MoneyPerPrice();
      if(perPrice>0.0)
         return (InpStopLossMoney/perPrice)/refPrice*100.0;
      Print("WARNING: InpStopLossMoney is set but the symbol reports no usable tick "
            "value - falling back to points, then percent");
     }
   if(InpStopLossPoints>0 && refPrice>0.0)
      return (InpStopLossPoints*_Point)/refPrice*100.0;
   return InpStopLossPct;
  }

//+------------------------------------------------------------------+
//| The target distance as a PERCENT of refPrice. Same order.         |
//| 0 when no target is configured at any level, which every caller    |
//| reads as "no take profit".                                         |
//+------------------------------------------------------------------+
double EffectiveTakeProfitPct(const double refPrice)
  {
   if(InpTakeProfitMoney>0.0 && refPrice>0.0)
     {
      const double perPrice = MoneyPerPrice();
      if(perPrice>0.0)
         return (InpTakeProfitMoney/perPrice)/refPrice*100.0;
      Print("WARNING: InpTakeProfitMoney is set but the symbol reports no usable tick "
            "value - falling back to points, then percent");
     }
   if(InpTakeProfitPoints>0 && refPrice>0.0)
      return (InpTakeProfitPoints*_Point)/refPrice*100.0;
   return InpTakeProfitPct;
  }

//+------------------------------------------------------------------+
//| The absolute take-profit price, or 0 for "no target".             |
//+------------------------------------------------------------------+
double TargetPrice(const ENUM_POSITION_TYPE side,const double entry)
  {
   const double pct = EffectiveTakeProfitPct(entry);
   if(pct<=0.0 || entry<=0.0)
      return 0.0;
   const double move = entry*pct/100.0;
   return (side==POSITION_TYPE_BUY) ? entry+move : entry-move;
  }

//+------------------------------------------------------------------+
//| The break-even stop, once the position has earned it. 0 = not yet.|
//|                                                                   |
//| Measured against the CLOSE side - the bid for a long - because     |
//| that is what the position is worth right now. Locking a few points  |
//| past entry rather than exactly at it is the difference between a   |
//| scratch and a scratch minus the spread.                            |
//+------------------------------------------------------------------+
double BreakEvenStop(const ENUM_POSITION_TYPE side,const double entry)
  {
   if(InpBreakEvenPct<=0.0 || entry<=0.0)
      return 0.0;
   const double bid = SymbolInfoDouble(_Symbol,SYMBOL_BID);
   const double ask = SymbolInfoDouble(_Symbol,SYMBOL_ASK);
   const double movePct = (side==POSITION_TYPE_BUY)
                          ? (bid-entry)/entry*100.0
                          : (entry-ask)/entry*100.0;
   if(movePct < InpBreakEvenPct)
      return 0.0;
   const double lock = InpBreakEvenLockPts*_Point;
   return (side==POSITION_TYPE_BUY) ? entry+lock : entry-lock;
  }

//+------------------------------------------------------------------+
//| Whether the server clock is inside the entry window.              |
//| start==end means all day, matching the scalper's convention; a     |
//| window that wraps midnight (22 to 4) is handled by the OR.         |
//+------------------------------------------------------------------+
bool SessionOpen()
  {
   if(InpSessionStartHour==InpSessionEndHour)
      return true;
   MqlDateTime t;
   TimeToStruct(TimeCurrent(),t);
   if(InpSessionStartHour < InpSessionEndHour)
      return t.hour>=InpSessionStartHour && t.hour<InpSessionEndHour;
   return t.hour>=InpSessionStartHour || t.hour<InpSessionEndHour;
  }

datetime DayStart()
  {
   MqlDateTime t;
   TimeToStruct(TimeCurrent(),t);
   t.hour = 0;
   t.min  = 0;
   t.sec  = 0;
   return StructToTime(t);
  }

//+------------------------------------------------------------------+
//| Entries closed today, from deal history under our magic.          |
//| DEAL_ENTRY_IN only, so a round trip counts once and not twice.     |
//+------------------------------------------------------------------+
int TradesToday()
  {
   if(!HistorySelect(DayStart(),TimeCurrent()+86400))
      return 0;
   int n = 0;
   const int total = HistoryDealsTotal();
   for(int i=0; i<total; i++)
     {
      const ulong ticket = HistoryDealGetTicket(i);
      if(ticket==0)
         continue;
      if(HistoryDealGetString(ticket,DEAL_SYMBOL)!=_Symbol)
         continue;
      if(HistoryDealGetInteger(ticket,DEAL_MAGIC)!=InpMagic)
         continue;
      if(HistoryDealGetInteger(ticket,DEAL_ENTRY)==DEAL_ENTRY_IN)
         n++;
     }
   return n;
  }

//+------------------------------------------------------------------+
//| Why new entries are blocked today, or an empty string when they   |
//| are not.                                                           |
//|                                                                    |
//| These govern ENTRIES ONLY. A daily loss limit that also closed an   |
//| open position would be a stop nobody chose, firing at a level set   |
//| by the calendar rather than by the trade.                           |
//+------------------------------------------------------------------+
string DailyHalt()
  {
   if(InpDailyLossLimit>0.0 || InpDailyProfitTarget>0.0)
     {
      const double today = RealisedSince(DayStart());
      if(InpDailyLossLimit>0.0 && today <= -InpDailyLossLimit)
         return StringFormat("daily loss limit: %.2f realised against a %.2f limit",
                             today,InpDailyLossLimit);
      if(InpDailyProfitTarget>0.0 && today >= InpDailyProfitTarget)
         return StringFormat("daily target reached: %.2f realised against a %.2f target",
                             today,InpDailyProfitTarget);
     }
   if(InpMaxTradesPerDay>0)
     {
      const int n = TradesToday();
      if(n >= InpMaxTradesPerDay)
         return StringFormat("daily trade cap: %d entries against a %d cap",
                             n,InpMaxTradesPerDay);
     }
   return "";
  }

//+------------------------------------------------------------------+
//| Init                                                             |
//+------------------------------------------------------------------+
int OnInit()
  {
   g_tf = (ENUM_TIMEFRAMES)_Period;
   TrailClear(g_trail);

   if(InpEmaPeriod < 2)
     {
      Print("FATAL: InpEmaPeriod must be at least 2");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpBbPeriod < 2)
     {
      Print("FATAL: InpBbPeriod must be at least 2");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpBbStdev <= 0.0)
     {
      Print("FATAL: InpBbStdev must be positive");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpSeedBars < WarmupBars())
     {
      PrintFormat("FATAL: InpSeedBars (%d) is below the %d-bar warmup; the EMA would "
                  "never be trusted",InpSeedBars,WarmupBars());
      return INIT_PARAMETERS_INCORRECT;
     }
   if(!GoldPreflight(InpMagic,InpStopLossPct,InpTrailActivationPct,InpTrailPct,
                     InpGivebackFrac))
      return INIT_PARAMETERS_INCORRECT;
   if(InpTakeProfitPct<0.0 || InpBreakEvenPct<0.0)
     {
      Print("FATAL: InpTakeProfitPct and InpBreakEvenPct cannot be negative");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpStopLossPoints<0 || InpTakeProfitPoints<0 || InpBreakEvenLockPts<0)
     {
      Print("FATAL: point distances cannot be negative");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpStopLossMoney<0.0 || InpTakeProfitMoney<0.0
      || InpDailyLossLimit<0.0 || InpDailyProfitTarget<0.0)
     {
      Print("FATAL: money amounts cannot be negative - a limit is a magnitude, "
            "and the sign is applied where it is compared");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpSessionStartHour<0 || InpSessionStartHour>23
      || InpSessionEndHour<0 || InpSessionEndHour>23)
     {
      Print("FATAL: session hours must be 0-23");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpMaxTradesPerDay<0)
     {
      Print("FATAL: InpMaxTradesPerDay cannot be negative. 0 disables the cap");
      return INIT_PARAMETERS_INCORRECT;
     }

   if(!g_trader.Init(_Symbol,InpMagic,InpLots,InpSlippagePoints,InpComment))
      return INIT_FAILED;

   if(!SeedIndicators())
      return INIT_FAILED;

   GoldTimeframeNote(g_tf);

//--- A reload mid-trade must not leave the position naked for a bar.
   const GoldPosition pos = g_trader.Snapshot();
   if(pos.exists)
     {
      RebuildTrail(g_trail,_Symbol,g_tf,pos);
      const double sl = ProtectiveStopPrice(g_trail,pos.side,pos.entry,
                                            EffectiveStopPct(pos.entry),
                                            InpTrailActivationPct,InpTrailPct,
                                            InpGivebackFrac);
      g_trader.ApplyStop(sl,TargetPrice(pos.side,pos.entry));
      PrintFormat("adopted an existing %s position of %.2f lots at %.2f (magic %d)",
                  (pos.side==POSITION_TYPE_BUY?"BUY":"SELL"),pos.volume,pos.entry,(int)InpMagic);
     }

   g_lastBarTime = iTime(_Symbol,g_tf,0);
   if(InpShowDashboard)
      g_dash.Create("AlgoGoldEmaBb_",
                    StringFormat("ALGOGOLD EMA/BB  (%d)",(int)InpMagic),
                    InpPanelX,InpPanelY,InpPanelWidth);

   PrintFormat("EMA(%d) + BB(%d, %.1f) %s%s on %s %s | stop %.2f%% | magic %d",
               InpEmaPeriod,InpBbPeriod,InpBbStdev,
               (InpMode==MODE_PULLBACK?"PULLBACK":"BREAKOUT"),
               (InpLongOnly?", LONG ONLY":""),
               _Symbol,EnumToString(g_tf),InpStopLossPct,(int)InpMagic);
   MqlDateTime nowSrv;
   TimeToStruct(TimeCurrent(),nowSrv);
   PrintFormat("server time is %02d:%02d - session inputs are in THIS clock, not yours",
               nowSrv.hour,nowSrv.min);
   if(InpSessionStartHour!=InpSessionEndHour)
      PrintFormat("entries restricted to server hours %d-%d%s",
                  InpSessionStartHour,InpSessionEndHour,
                  (InpCloseAtSessionEnd?", flattening when it shuts":""));
   if(EffectiveTakeProfitPct(SymbolInfoDouble(_Symbol,SYMBOL_BID))>0.0)
      PrintFormat("take profit ON at %.3f%% of entry - NO Python counterpart, so no "
                  "measured study covers it",
                  EffectiveTakeProfitPct(SymbolInfoDouble(_Symbol,SYMBOL_BID)));
   if(InpDailyLossLimit>0.0 || InpDailyProfitTarget>0.0 || InpMaxTradesPerDay>0)
      PrintFormat("daily governors: loss limit %.2f | profit target %.2f | max trades %d "
                  "(0 = off). These block ENTRIES only; an open position is left alone.",
                  InpDailyLossLimit,InpDailyProfitTarget,InpMaxTradesPerDay);
   if(InpGivebackFrac>0.0)
     {
      PrintFormat("give-back trail ON: closes once %.2f of the peak unrealised profit is "
                  "handed back, armed at %.2f%%",InpGivebackFrac,InpTrailActivationPct);
      if(InpTrailActivationPct>=1.0)   // a gate this wide outruns the bands
         PrintFormat("WARNING: it arms only after a %.2f%% favourable move - about $%.0f on "
                     "gold near %.0f. That is far wider than a Bollinger band, so the middle "
                     "band will almost certainly exit first and this trail will rarely fire. "
                     "Lower TrailActivationPct if you mean it to.",
                     InpTrailActivationPct,
                     SymbolInfoDouble(_Symbol,SYMBOL_BID)*InpTrailActivationPct/100.0,
                     SymbolInfoDouble(_Symbol,SYMBOL_BID));
     }
   Print("NO MEASURED EDGE. D-152: pullback below break-even in every window, breakout "
         "beaten by buy-and-hold. D-153: long-only pre-registered and REJECTED on seven "
         "years of unseen data. Demo only.");
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
//--- The panel repaints on the TICK, not on the bar. P&L, spread and floating
//--- are live numbers, and on H1 a bar-close-only panel would show figures up to
//--- an hour stale while looking current - which is worse than showing nothing.
//--- Throttled to once a second: chart objects are not free, and nothing here
//--- changes faster than a person can read it.
   const datetime now = TimeCurrent();
   if(now!=g_lastPaint)
     {
      g_lastPaint = now;
      Repaint(g_trader.Snapshot());
     }

   const datetime current = iTime(_Symbol,g_tf,0);
   if(current==g_lastBarTime || current==0)
      return;
   g_lastBarTime = current;
   OnClosedBar();
  }

//+------------------------------------------------------------------+
//| The whole strategy, in the order ema_bb.on_bar runs it.          |
//+------------------------------------------------------------------+
void OnClosedBar()
  {
   const double close = iClose(_Symbol,g_tf,1);
   const double high  = iHigh (_Symbol,g_tf,1);
   const double low   = iLow  (_Symbol,g_tf,1);

//--- 1. The EMA is stepped FIRST, unconditionally. See the header.
   g_ema    = EmaStep(close,g_ema,g_hasEma);
   g_hasEma = true;
   g_barsSeen++;

   const GoldPosition pos = g_trader.Snapshot();

//--- 2. Protective exits, before the warmup gate. A held position must
//---    never go unprotected because an indicator has not converged.
   if(pos.exists)
     {
      TrailAdvance(g_trail,high,low);
      const ExitKind fired = ProtectiveExitsCheck(g_trail,pos.exists,pos.side,pos.entry,
                                                  high,low,EffectiveStopPct(pos.entry),
                                                  InpTrailActivationPct,InpTrailPct,
                                                  InpGivebackFrac);
      if(fired!=EXIT_NONE)
        {
         //--- Normally the broker-side SL placed last bar has already fired
         //--- intrabar and this finds nothing to do. It is the backstop for the
         //--- bar where the stop could not be placed (freeze band, rejected
         //--- modify, a position adopted at init) - closed at market instead.
         //--- Built per kind rather than with one ternary over the number: the
         //--- give-back trail's setting is a FRACTION and printing it through a
         //--- "%%" format would report 0.50%% for a trail that actually gave back
         //--- half the move - a log line that reads plausibly and is wrong.
         string level;
         if(fired==EXIT_STOP)
            level = StringFormat("%.2f%% against entry",EffectiveStopPct(pos.entry));
         else if(fired==EXIT_TRAIL)
            level = StringFormat("%.2f%% behind the peak of %.2f",InpTrailPct,g_trail.peak);
         else
            level = StringFormat("gave back %.2f of the move banked to a peak of %.2f, "
                                 "closing at %.2f",InpGivebackFrac,g_trail.peak,
                                 GivebackLevel(g_trail,InpGivebackFrac));
         g_trader.CloseAll(StringFormat("%s: %s, on a %s position, entry %.2f",
                                        ExitKindName(fired),level,
                                        (pos.side==POSITION_TYPE_BUY?"BUY":"SELL"),pos.entry));
         TrailClear(g_trail);
         Repaint(pos);
         return;
        }
      double sl = ProtectiveStopPrice(g_trail,pos.side,pos.entry,
                                      EffectiveStopPct(pos.entry),
                                      InpTrailActivationPct,InpTrailPct,
                                      InpGivebackFrac);

      //--- Break-even takes the stop only when it is BETTER than whatever the
      //--- protective levels already produced. Taking it unconditionally would
      //--- pull an armed trail backwards to entry, giving away banked profit to
      //--- a rule whose whole purpose is to protect it.
      const double be = BreakEvenStop(pos.side,pos.entry);
      if(be>0.0)
         sl = (sl<=0.0) ? be
              : ((pos.side==POSITION_TYPE_BUY) ? MathMax(sl,be) : MathMin(sl,be));

      g_trader.ApplyStop(sl,TargetPrice(pos.side,pos.entry));
     }

//--- 3. Warmup gate.
   if(g_barsSeen < WarmupBars())
     {
      Repaint(pos);
      return;
     }

   double middle,upper,lower, pMiddle,pUpper,pLower;
   if(!BandsAt(1,middle,upper,lower) || !BandsAt(2,pMiddle,pUpper,pLower))
     {
      Repaint(pos);
      return;
     }
   const double prevClose  = iClose(_Symbol,g_tf,2);
   const bool   aboveTrend = close > g_ema;
   const bool   belowTrend = close < g_ema;

//--- 4. Held: both modes exit on the middle band, from opposite sides.
   if(pos.exists)
     {
      //--- Session flatten first: a position the window has closed should not
      //--- get a vote from the band on the way out.
      if(InpCloseAtSessionEnd && !SessionOpen())
        {
         g_trader.CloseAll(StringFormat("session closed (server hours %d-%d): "
                                        "flattening rather than carrying swap",
                                        InpSessionStartHour,InpSessionEndHour));
         TrailClear(g_trail);
         Repaint(pos);
         return;
        }
      const bool isLong = (pos.side==POSITION_TYPE_BUY);
      bool wantsClose = false;
      if(InpMode==MODE_PULLBACK)
         wantsClose = isLong ? (close >= middle) : (close <= middle);
      else
         wantsClose = isLong ? (close <  middle) : (close >  middle);

      if(wantsClose)
        {
         g_trader.CloseAll(StringFormat("EMA/BB %s: %s - close %.2f vs middle %.2f",
                           (InpMode==MODE_PULLBACK?"pullback":"breakout"),
                           (InpMode==MODE_PULLBACK?"reached the middle band"
                                                 :"closed back inside the band"),
                           close,middle));
         TrailClear(g_trail);
        }
      Repaint(pos);
      return;                                  // no reversal in one step
     }

//--- 5. Flat: entries.
   if(!InpAllowNewEntries)
     {
      Repaint(pos);
      return;
     }
   if(!SessionOpen())
     {
      Repaint(pos);
      return;
     }
   const string halt = DailyHalt();
   if(halt!="")
     {
      //--- Logged once per bar rather than per tick: a halt that prints on every
      //--- tick buries the reason it fired under thousands of copies of itself.
      PrintFormat("entry blocked: %s",halt);
      Repaint(pos);
      return;
     }
   if(InpMaxSpreadPoints>0)
     {
      const long spread = SymbolInfoInteger(_Symbol,SYMBOL_SPREAD);
      if(spread > InpMaxSpreadPoints)
        {
         PrintFormat("entry blocked: spread %d points is above the %d-point limit",
                     (int)spread,InpMaxSpreadPoints);
         Repaint(pos);
         return;
        }
     }

   if(InpMode==MODE_PULLBACK)
     {
      //--- The RE-ENTRY is the signal, not the excursion:
      //--- prev_close < prev_lower <= close.
      if(aboveTrend && prevClose < pLower && close >= pLower)
        {
         OpenWith(POSITION_TYPE_BUY,
                  StringFormat("EMA/BB pullback: close %.2f back above the lower band %.2f "
                               "(previous %.2f), price above the %d EMA %.2f",
                               close,lower,prevClose,InpEmaPeriod,g_ema));
        }
      else if(!InpLongOnly && belowTrend && prevClose > pUpper && close <= pUpper)
        {
         OpenWith(POSITION_TYPE_SELL,
                  StringFormat("EMA/BB pullback: close %.2f back below the upper band %.2f "
                               "(previous %.2f), price below the %d EMA %.2f",
                               close,upper,prevClose,InpEmaPeriod,g_ema));
        }
     }
   else
     {
      if(aboveTrend && close > upper)
        {
         OpenWith(POSITION_TYPE_BUY,
                  StringFormat("EMA/BB breakout: close %.2f above the upper band %.2f, "
                               "price above the %d EMA %.2f",
                               close,upper,InpEmaPeriod,g_ema));
        }
      else if(!InpLongOnly && belowTrend && close < lower)
        {
         OpenWith(POSITION_TYPE_SELL,
                  StringFormat("EMA/BB breakout: close %.2f below the lower band %.2f, "
                               "price below the %d EMA %.2f",
                               close,lower,InpEmaPeriod,g_ema));
        }
     }

   Repaint(g_trader.Snapshot());
  }

//+------------------------------------------------------------------+
//| Open, and start the trail from the fill.                         |
//+------------------------------------------------------------------+
void OpenWith(const ENUM_POSITION_TYPE side,const string reason)
  {
   if(!g_trader.Open(side,reason))
      return;
   const GoldPosition opened = g_trader.Snapshot();
   if(opened.exists)
     {
      TrailStart(g_trail,opened.entry,opened.side);
      //--- Apply both levels immediately. Waiting for the next bar close would
      //--- leave a new position unprotected for a whole bar, which on H1 is an
      //--- hour, and the target unset for exactly as long.
      const double sl = ProtectiveStopPrice(g_trail,opened.side,opened.entry,
                                            EffectiveStopPct(opened.entry),
                                            InpTrailActivationPct,InpTrailPct,
                                            InpGivebackFrac);
      g_trader.ApplyStop(sl,TargetPrice(opened.side,opened.entry));
     }
  }

//+------------------------------------------------------------------+
//| Dashboard                                                        |
//|                                                                  |
//| Laid out in the same four blocks GoldCamarillaBreakout uses, and  |
//| in the same order, so a person running both reads them the same   |
//| way rather than relearning a layout per chart.                    |
//|                                                                   |
//| P&L IS LAST, AND THAT IS THE POINT. It is the block the eye goes  |
//| to first, so it sits where its position cannot move: every block  |
//| above it writes a FIXED number of rows - the position block fills  |
//| placeholders when flat rather than omitting rows - so nothing      |
//| above can grow and shove the P&L figures to a different height. A  |
//| number that moves around is a number that gets misread.            |
//|                                                                    |
//| ClearFrom() at the end deletes anything below the rows actually    |
//| written, so a layout that ever shrinks cannot leave the tail of a  |
//| taller one on screen showing values from when it was last that     |
//| tall - stale rows that look live.                                  |
//+------------------------------------------------------------------+
void Repaint(const GoldPosition &pos)
  {
   if(!g_dash.Active())
      return;
   g_dash.Refresh(StringFormat("ALGOGOLD EMA/BB  (%d)",(int)InpMagic));

   const int    digits = g_trader.Digits();
   const color  cOk    = C'120,220,140', cBad = C'240,110,110';
   const color  cDim   = C'150,160,180', cHot = C'255,200,90';
   const color  cWhite = C'225,232,242';

   int r = 0;

//--- SIGNAL: the only block MT5 cannot show you anywhere else.
   g_dash.SetSection(r++,"-- SIGNAL --");
   g_dash.Set(r++,"MODE",
              StringFormat("%s%s",(InpMode==MODE_PULLBACK?"pullback":"breakout"),
                           (InpLongOnly?"  (long only)":"")),clrAqua);
   g_dash.Set(r++,StringFormat("EMA(%d)",InpEmaPeriod),
              DoubleToString(g_ema,digits),cWhite);

   double m,u,l;
   if(BandsAt(1,m,u,l))
     {
      g_dash.Set(r++,"UPPER",DoubleToString(u,digits),cDim);
      g_dash.Set(r++,"MIDDLE",DoubleToString(m,digits),cDim);
      g_dash.Set(r++,"LOWER",DoubleToString(l,digits),cDim);
     }
   else
     {
      g_dash.Set(r++,"UPPER","-",cDim);
      g_dash.Set(r++,"MIDDLE","-",cDim);
      g_dash.Set(r++,"LOWER","-",cDim);
     }

   const bool warm = (g_barsSeen>=WarmupBars());
   g_dash.Set(r++,"WARMUP",StringFormat("%d / %d bars",g_barsSeen,WarmupBars()),
              (warm?cOk:cHot));
   g_dash.Set(r++,"NOTE","no measured edge - D-152/153",cBad);

//--- POSITION: a FIXED five rows whether flat or holding, so nothing below
//--- this block ever changes height. See the header.
   g_dash.SetSection(r++,"-- POSITION --");
   const double floating = FloatingPnl();
   if(pos.exists)
     {
      g_dash.Set(r++,"SIDE",
                 StringFormat("%s %.2f @ %s",(pos.side==POSITION_TYPE_BUY?"BUY":"SELL"),
                              pos.volume,DoubleToString(pos.entry,digits)),
                 (pos.side==POSITION_TYPE_BUY?cOk:cBad));
      g_dash.Set(r++,"FLOATING",StringFormat("%+.2f",floating),
                 (floating>=0.0?cOk:cBad));
      const bool armed = TrailIsArmed(g_trail,InpTrailActivationPct);
      g_dash.Set(r++,"PEAK",DoubleToString(g_trail.peak,digits),cDim);
      g_dash.Set(r++,"TRAIL",(armed?"ARMED":StringFormat("arms at %.2f%%",InpTrailActivationPct)),
                 (armed?cOk:cDim));
      const double tp = TargetPrice(pos.side,pos.entry);
      g_dash.Set(r++,"TARGET",(tp>0.0?DoubleToString(tp,digits):"off"),
                 (tp>0.0?cOk:cDim));
      const double be = BreakEvenStop(pos.side,pos.entry);
      g_dash.Set(r++,"BREAK-EVEN",
                 (InpBreakEvenPct<=0.0 ? "off"
                  : (be>0.0 ? "LOCKED" : StringFormat("at %.2f%%",InpBreakEvenPct))),
                 (be>0.0?cOk:cDim));
      if(InpGivebackFrac>0.0)
         g_dash.Set(r++,"GIVE-BACK",
                    (armed?DoubleToString(GivebackLevel(g_trail,InpGivebackFrac),digits)
                          :StringFormat("%.2f of peak",InpGivebackFrac)),
                    (armed?cOk:cDim));
      else
         g_dash.Set(r++,"GIVE-BACK","off  (D-154)",cDim);
     }
   else
     {
      g_dash.Set(r++,"SIDE","flat",cDim);
      g_dash.Set(r++,"FLOATING","0.00",cDim);
      g_dash.Set(r++,"PEAK","-",cDim);
      g_dash.Set(r++,"TRAIL","-",cDim);
      g_dash.Set(r++,"TARGET",
                 (EffectiveTakeProfitPct(SymbolInfoDouble(_Symbol,SYMBOL_BID))>0.0
                  ? "set on entry" : "off"),cDim);
      g_dash.Set(r++,"BREAK-EVEN",(InpBreakEvenPct>0.0
                                   ? StringFormat("at %.2f%%",InpBreakEvenPct) : "off"),cDim);
      g_dash.Set(r++,"GIVE-BACK",(InpGivebackFrac>0.0?"armed when held":"off  (D-154)"),cDim);
     }

//--- MARKET. After the strategy blocks, because it is the part MT5 already
//--- shows elsewhere - it is here for confirmation, not discovery.
   g_dash.SetSection(r++,"-- MARKET --");
   const bool canTrade = (bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)
                         && (bool)MQLInfoInteger(MQL_TRADE_ALLOWED);
   g_dash.Set(r++,"STATUS",
              (canTrade ? (InpAllowNewEntries ? "TRADING" : "MANAGE ONLY") : "ALGO OFF"),
              (canTrade && InpAllowNewEntries) ? cOk : cHot);
   g_dash.Set(r++,"SYMBOL / TF",
              StringFormat("%s  %s",_Symbol,StringSubstr(EnumToString(g_tf),7)),cWhite);
   g_dash.Set(r++,"SPREAD",
              StringFormat("%d pts",(int)SymbolInfoInteger(_Symbol,SYMBOL_SPREAD)),cDim);

//--- GOVERNORS. The panel has to answer "why is it not trading" without anyone
//--- reading the log, so a block that is off says so rather than being absent.
   const bool sessionOn = SessionOpen();
   g_dash.Set(r++,"SESSION",
              (InpSessionStartHour==InpSessionEndHour
               ? "all day"
               : StringFormat("%02d-%02d  %s",InpSessionStartHour,InpSessionEndHour,
                              (sessionOn?"OPEN":"SHUT"))),
              (sessionOn?cOk:cHot));
   const string halt = DailyHalt();
   g_dash.Set(r++,"ENTRIES",
              (!InpAllowNewEntries ? "manage only"
               : (halt!="" ? "HALTED" : (sessionOn ? "allowed" : "out of session"))),
              ((InpAllowNewEntries && halt=="" && sessionOn) ? cOk : cHot));
   g_dash.Set(r++,"TRADES TODAY",
              (InpMaxTradesPerDay>0
               ? StringFormat("%d / %d",TradesToday(),InpMaxTradesPerDay)
               : StringFormat("%d",TradesToday())),cDim);

//--- P&L, last and largest.
   g_dash.SetSection(r++,"-- P&L --");
   MqlDateTime t;
   TimeToStruct(TimeCurrent(),t);
   t.hour = 0; t.min = 0; t.sec = 0;
   const datetime dayStart = StructToTime(t);
   const double today = RealisedSince(dayStart);
   const double week  = RealisedSince(dayStart-6*86400);
   const double net   = today + floating;

//--- Realised PLUS floating: the number a person means by "am I up today". Split
//--- underneath, because a flat +0.00 built from a won trade and a losing open
//--- position is not the same day as one where nothing happened.
   g_dash.SetBig(r++,"NET TODAY",StringFormat("%+.2f",net),(net>=0.0?cOk:cBad));
   g_dash.Set(r++,"  floating",StringFormat("%+.2f",floating),(floating>=0.0?cOk:cBad));
   g_dash.Set(r++,"  realised",StringFormat("%+.2f",today),(today>=0.0?cOk:cBad));
   g_dash.Set(r++,"7 DAYS",StringFormat("%+.2f",week),(week>=0.0?cOk:cBad));
   g_dash.Set(r++,"EQUITY",
              StringFormat("%.2f",AccountInfoDouble(ACCOUNT_EQUITY)),cWhite);

   g_dash.ClearFrom(r);
   ChartRedraw(0);
  }

//+------------------------------------------------------------------+
