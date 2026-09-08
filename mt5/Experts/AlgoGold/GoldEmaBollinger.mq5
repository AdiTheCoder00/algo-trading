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
input double InpTrailActivationPct = 2.0;   // Profit % at which the trail arms
input double InpTrailPct         = 0.0;     // Trail distance, % behind peak. 0 disables

input group "--- Execution ---"
input double InpLots             = 0.05;    // Volume in MT5 LOTS (1.00 = 100 oz)
input long   InpMagic            = 20260906;// Distinct from 20260828/01/02/03/04, and 05 (Camarilla)
input ulong  InpSlippagePoints   = 30;      // Max deviation, points
input int    InpMaxSpreadPoints  = 0;       // Block NEW entries above this spread. 0 = off
input bool   InpAllowNewEntries  = true;    // false = manage open positions only
input bool   InpShowDashboard    = true;    // Draw the on-chart panel
input string InpComment          = "AlgoGold EMA/BB"; // Cosmetic only - MT5 overwrites it

//+------------------------------------------------------------------+
//| State                                                            |
//+------------------------------------------------------------------+
CGoldTrader     g_trader;
CGoldDashboard  g_dash;
TrailState      g_trail;
ENUM_TIMEFRAMES g_tf          = PERIOD_CURRENT;
datetime        g_lastBarTime = 0;

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
   if(!GoldPreflight(InpMagic,InpStopLossPct,InpTrailActivationPct,InpTrailPct))
      return INIT_PARAMETERS_INCORRECT;

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
                                            InpStopLossPct,InpTrailActivationPct,InpTrailPct);
      g_trader.ApplyStop(sl);
      PrintFormat("adopted an existing %s position of %.2f lots at %.2f (magic %d)",
                  (pos.side==POSITION_TYPE_BUY?"BUY":"SELL"),pos.volume,pos.entry,(int)InpMagic);
     }

   g_lastBarTime = iTime(_Symbol,g_tf,0);
   if(InpShowDashboard)
      g_dash.Create("AlgoGoldEmaBb_","EMA/BB "+_Symbol);

   PrintFormat("EMA(%d) + BB(%d, %.1f) %s%s on %s %s | stop %.2f%% | magic %d",
               InpEmaPeriod,InpBbPeriod,InpBbStdev,
               (InpMode==MODE_PULLBACK?"PULLBACK":"BREAKOUT"),
               (InpLongOnly?", LONG ONLY":""),
               _Symbol,EnumToString(g_tf),InpStopLossPct,(int)InpMagic);
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
                                                  high,low,InpStopLossPct,
                                                  InpTrailActivationPct,InpTrailPct);
      if(fired!=EXIT_NONE)
        {
         //--- Normally the broker-side SL placed last bar has already fired
         //--- intrabar and this finds nothing to do. It is the backstop for the
         //--- bar where the stop could not be placed (freeze band, rejected
         //--- modify, a position adopted at init) - closed at market instead.
         g_trader.CloseAll(StringFormat("%s: %.2f%% level against a %s position, entry %.2f",
                                        ExitKindName(fired),
                                        (fired==EXIT_STOP?InpStopLossPct:InpTrailPct),
                                        (pos.side==POSITION_TYPE_BUY?"BUY":"SELL"),pos.entry));
         TrailClear(g_trail);
         Repaint(pos);
         return;
        }
      const double sl = ProtectiveStopPrice(g_trail,pos.side,pos.entry,
                                            InpStopLossPct,InpTrailActivationPct,InpTrailPct);
      g_trader.ApplyStop(sl);
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
      TrailStart(g_trail,opened.entry,opened.side);
  }

//+------------------------------------------------------------------+
//| Dashboard                                                        |
//+------------------------------------------------------------------+
void Repaint(const GoldPosition &pos)
  {
   if(!g_dash.Active())
      return;
   g_dash.Refresh("EMA/BB "+_Symbol);

   g_dash.Set(0,"mode",
              StringFormat("%s%s",(InpMode==MODE_PULLBACK?"pullback":"breakout"),
                           (InpLongOnly?" (long only)":"")),clrAqua);
   if(pos.exists)
      g_dash.Set(1,"position",StringFormat("%s %.2f @ %.2f",
                 (pos.side==POSITION_TYPE_BUY?"BUY":"SELL"),pos.volume,pos.entry),
                 (pos.side==POSITION_TYPE_BUY?clrLime:clrTomato));
   else
      g_dash.Set(1,"position","flat",clrSilver);

   g_dash.Set(2,StringFormat("EMA(%d)",InpEmaPeriod),
              DoubleToString(g_ema,g_trader.Digits()),clrWhite);

   double m,u,l;
   if(BandsAt(1,m,u,l))
     {
      g_dash.Set(3,"upper",DoubleToString(u,g_trader.Digits()),clrSilver);
      g_dash.Set(4,"middle",DoubleToString(m,g_trader.Digits()),clrSilver);
      g_dash.Set(5,"lower",DoubleToString(l,g_trader.Digits()),clrSilver);
     }
   g_dash.Set(6,"warmup",
              StringFormat("%d / %d bars",g_barsSeen,WarmupBars()),
              (g_barsSeen>=WarmupBars()?clrLime:clrGold));
   g_dash.Set(7,"NOTE","no measured edge - D-152/153",clrTomato);
  }
//+------------------------------------------------------------------+
