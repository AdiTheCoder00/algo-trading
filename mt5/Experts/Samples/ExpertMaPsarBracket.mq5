//+------------------------------------------------------------------+
//| ExpertMaPsarBracket.mq5                                          |
//|                                                                  |
//| ExpertMAPSAR (the MetaQuotes CExpert sample) with a real stop    |
//| loss and take profit added.                                      |
//|                                                                  |
//| ====================================================================
//| WHY THIS FILE EXISTS
//| ====================================================================
//| The stock sample trades with NO stop and NO target. That is not  |
//| an oversight in the sample - it is a framework demo - but it is  |
//| worth stating precisely, because it is easy to miss:             |
//|                                                                  |
//|   CExpertSignal's constructor sets m_stop_level(0.0) and         |
//|   m_take_level(0.0), and CExpertSignal::OpenLongParams does      |
//|     sl = (m_stop_level==0.0) ? 0.0 : ...                         |
//|                                                                  |
//| so with the defaults the orders go out with sl=0 and tp=0. The   |
//| only thing bounding a loss is the Parabolic SAR trail, and that  |
//| does not engage until price has moved in your favour: its        |
//| CheckTrailingStopLong uses                                       |
//|     base = (pos_sl==0.0) ? PriceOpen() : pos_sl                  |
//|     if(new_sl > base && new_sl < level) sl = new_sl;             |
//| so with no initial stop, `base` IS the entry price and the trail |
//| can only ever move the stop above entry. A position that goes    |
//| straight against you is completely unprotected until the         |
//| opposite signal arrives.                                         |
//|                                                                  |
//| Adding the bracket changes that: `base` becomes the initial stop |
//| from the first tick, and the PSAR tightens from there.           |
//|                                                                  |
//| ====================================================================
//| THE UNITS ARE "ADJUSTED POINTS", NOT POINTS
//| ====================================================================
//| StopLevel/TakeLevel are multiplied by CExpertBase::PriceLevelUnit|
//| which returns m_adjusted_point, and CExpert::Init computes it as |
//|                                                                  |
//|   digits_adjust   = (Digits()==3 || Digits()==5) ? 10 : 1        |
//|   m_adjusted_point= Point() * digits_adjust                      |
//|                                                                  |
//| i.e. the classic "pip". On this broker that lands at 0.01 price  |
//| for XAUUSD (2 digits), BTCUSD (2 digits) AND FixedVol100 (3      |
//| digits), so 400 means a 4.00 stop on all three. Do not assume it |
//| generalises - a 5-digit FX pair gives 400 -> 0.00400.            |
//| The resolved PRICE distance is printed on init rather than left  |
//| to be inferred.                                                  |
//|                                                                  |
//| ====================================================================
//| THE BROKER MINIMUM IS CHECKED, BECAUSE IT SILENTLY KILLS EAs
//| ====================================================================
//| A stop closer to price than SYMBOL_TRADE_STOPS_LEVEL is rejected |
//| outright. Measured on this account: XAUUSD 0.20, BTCUSD 0.00,    |
//| FixedVol100 7.123 - a 35x spread. A gold-tuned 4.00 stop is      |
//| simply illegal on FixedVol100, and an EA configured that way     |
//| sends orders that are ALL rejected while looking perfectly       |
//| healthy from the outside (observed: 1,027 sends, 2,054           |
//| rejections, zero fills, no obvious error anywhere but the log).  |
//| So OnInit refuses to start rather than let that happen quietly.  |
//+------------------------------------------------------------------+
#property copyright "Derived from the MetaQuotes ExpertMAPSAR sample"
#property link      "https://www.mql5.com"
#property version   "1.00"
#property description "ExpertMAPSAR with an explicit stop loss and take profit."
#property description "DO NOT TRADE: measured at profit factor 1.06/0.77/0.81 on M1 and"
#property description "0.90/0.58/1.04 on M15 across three XAUUSD windows - no edge (D-142)."
#property description "The bracket, gates and pre-flight are the reusable part."
//+------------------------------------------------------------------+
//| Include                                                          |
//+------------------------------------------------------------------+
#include <Expert\Expert.mqh>
#include <Expert\Signal\SignalMA.mqh>
#include <Expert\Trailing\TrailingParabolicSAR.mqh>
#include <Expert\Money\MoneyNone.mqh>
//--- The cost-side gates from the AlgoGold work. Reused rather than rewritten:
//--- they are the one part of D-140 that survived, and a second copy would be
//--- the "two copies that quietly drift" hazard mt5/README.md warns about.
#include <AlgoGold\ScalpFilters.mqh>
//+------------------------------------------------------------------+
//| Inputs                                                           |
//+------------------------------------------------------------------+
//--- inputs for expert
input string             Inp_Expert_Title                 ="ExpertMaPsarBracket";
int                      Expert_MagicNumber               =14599;   // NOT 14598 - the stock sample owns that
bool                     Expert_EveryTick                 =false;
//--- inputs for signal
input int                Inp_Signal_MA_Period             =12;
input int                Inp_Signal_MA_Shift              =6;
input ENUM_MA_METHOD     Inp_Signal_MA_Method             =MODE_SMA;
input ENUM_APPLIED_PRICE Inp_Signal_MA_Applied            =PRICE_CLOSE;
//--- THE ADDITION: bracket, in adjusted points (see the header)
input group "--- Bracket (adjusted points = pips, NOT raw points) ---"
input int                Inp_Signal_StopLevel             =400;     // Stop loss. 0 = none (the stock sample's behaviour)
input int                Inp_Signal_TakeLevel             =800;     // Take profit. 0 = none
//--- Pattern weights, exposed because pattern 0 interacts badly with a stop.
//---
//--- CSignalMA votes with four models. Pattern 0 - "price is on the necessary
//--- side of the indicator" - is a persistent STATE, not an event, and its
//--- default weight of 80 clears CExpertSignal's threshold_open of 50 on its
//--- own. Without a stop that is harmless: one position is opened and held
//--- until the opposite signal. With a stop it is not: the position is stopped
//--- out, the state is still true, and it re-enters immediately.
//---
//--- Measured on XAUUSD M1 2026.06-08: the same expert took 51 trades with no
//--- bracket and 4,936 with one, and the difference was almost entirely this.
//--- 4,936 round trips at ~$0.28 spread is ~$1,382 of cost against a gross
//--- result near +$208.
//---
//--- Patterns 1/2/3 are crossings and piercings - events, which fire once - so
//--- setting Pattern_0 to 0 is the way to keep the bracket without the churn.
input group "--- Signal pattern weights (library defaults 80/10/60/60) ---"
input int                Inp_Signal_Pattern_0             =80;      // "price on the right side of MA" - a STATE. 0 disables
input int                Inp_Signal_Pattern_1             =10;      // crossed with opposite direction
input int                Inp_Signal_Pattern_2             =60;      // crossed with same direction
input int                Inp_Signal_Pattern_3             =60;      // piercing
//--- inputs for trailing
input double             Inp_Trailing_ParabolicSAR_Step   =0.02;
input double             Inp_Trailing_ParabolicSAR_Maximum=0.2;
//--- Cost and exposure gates.
//---
//--- ALL DEFAULT TO OFF, so the expert's measured behaviour is unchanged until
//--- they are switched on. That is deliberate: D-142 recorded this strategy at
//--- profit factor 1.06 / 0.77 / 0.81 across three windows, and a filter set
//--- that silently altered the baseline would make the comparison worthless.
//---
//--- These are not tuning knobs for the SIGNAL. They exist because every result
//--- in this repo keeps landing on the same cause - the spread is charged per
//--- round trip and dominates gross P&L (D-124, D-139, D-140, D-141). A signal
//--- with no edge cannot be rescued by them; a signal with a thin one can be
//--- destroyed by their absence. Turning them on is testable either way.
input group "--- Cost gates (all 0/off by default - see the header) ---"
input int                Inp_Filter_MaxSpreadPoints       =0;       // Block entries above this spread, points. 0 = off
input double             Inp_Filter_MinTpSpreadMult       =0.0;     // Target must clear this many spreads. 0 = off
input int                Inp_Filter_CooldownBars          =0;       // Bars of silence after an exit. 0 = off
input group "--- Session (SERVER hours; start==end means all day) ---"
input int                Inp_Filter_SessionStart          =0;       // Entries allowed from this server hour
input int                Inp_Filter_SessionEnd            =0;       // ...until this one
input int                Inp_Filter_FridayEnd             =-1;      // No entries Friday from here. -1 = off
input group "--- Daily governors ---"
input double             Inp_Filter_DailyLossLimit        =0.0;     // Halt for the day at this realised loss. 0 = off
input int                Inp_Filter_MaxTradesPerDay       =0;       // Halt after this many entries. 0 = off
//+------------------------------------------------------------------+
//| Global expert object                                             |
//+------------------------------------------------------------------+
CExpert  ExtExpert;
DayGuard ExtDay;
//+------------------------------------------------------------------+
//| CSignalMA with the cost gates applied.                            |
//|                                                                   |
//| Overriding the two vote methods is the right seam: CExpertSignal  |
//| sums the models' weights and compares against threshold_open, so  |
//| returning 0 suppresses the entry without touching the rest of the |
//| framework's ordering, sizing or trailing.                         |
//|                                                                   |
//| Only ENTRIES are gated. Nothing here can block an exit - refusing  |
//| to leave a position because leaving is expensive is how a small   |
//| loss becomes a large one, and CExpert's close path never consults |
//| these methods anyway.                                             |
//+------------------------------------------------------------------+
class CFilteredSignalMA : public CSignalMA
  {
private:
   bool              EntryAllowed(void);
public:
   virtual int       LongCondition(void)  { return EntryAllowed() ? CSignalMA::LongCondition()  : 0; }
   virtual int       ShortCondition(void) { return EntryAllowed() ? CSignalMA::ShortCondition() : 0; }
  };
//+------------------------------------------------------------------+
//| Every gate, in the order that makes the cheapest check first.     |
//+------------------------------------------------------------------+
bool CFilteredSignalMA::EntryAllowed(void)
  {
   const datetime now = TimeCurrent();
//--- session
   if(Inp_Filter_SessionStart != Inp_Filter_SessionEnd || Inp_Filter_FridayEnd >= 0)
     {
      string why="";
      if(!SessionAllowsEntry(now,Inp_Filter_SessionStart,Inp_Filter_SessionEnd,
                             Inp_Filter_FridayEnd,why))
         return false;
     }
//--- live spread
   if(Inp_Filter_MaxSpreadPoints > 0)
     {
      if(SymbolInfoInteger(_Symbol,SYMBOL_SPREAD) > Inp_Filter_MaxSpreadPoints)
         return false;
     }
//--- cost gate: is the target even worth the spread it must pay?
   if(Inp_Filter_MinTpSpreadMult > 0.0 && Inp_Signal_TakeLevel > 0)
     {
      const double spread = SpreadPrice(_Symbol);
      const double target = Inp_Signal_TakeLevel * AdjustedPoint();
      if(spread > 0.0 && target < spread*Inp_Filter_MinTpSpreadMult)
         return false;
     }
//--- cooldown, derived from deal history so a reload cannot reset it
   if(Inp_Filter_CooldownBars > 0)
     {
      const int since = BarsSinceLastExit(_Symbol,Expert_MagicNumber,(ENUM_TIMEFRAMES)_Period,
                                          now,3,1000000);
      if(since < Inp_Filter_CooldownBars)
         return false;
     }
//--- daily budget
   if(Inp_Filter_DailyLossLimit > 0.0 || Inp_Filter_MaxTradesPerDay > 0)
     {
      DayGuardRefresh(ExtDay,_Symbol,Expert_MagicNumber,now);
      if(!DayGuardAllowsEntry(ExtDay,Inp_Filter_DailyLossLimit,0.0,Inp_Filter_MaxTradesPerDay))
         return false;
     }
   return true;
  }
//+------------------------------------------------------------------+
//| The same unit CExpert::Init will compute, so the value printed   |
//| and validated here is the one actually used.                     |
//+------------------------------------------------------------------+
double AdjustedPoint(void)
  {
   const int digits =(int)SymbolInfoInteger(_Symbol,SYMBOL_DIGITS);
   const double point=SymbolInfoDouble(_Symbol,SYMBOL_POINT);
   return point*((digits==3 || digits==5) ? 10 : 1);
  }
//+------------------------------------------------------------------+
//| Refuse to run with a bracket the broker will reject.             |
//+------------------------------------------------------------------+
bool ValidateBracket(void)
  {
   const int    digits =(int)SymbolInfoInteger(_Symbol,SYMBOL_DIGITS);
   const double point  =SymbolInfoDouble(_Symbol,SYMBOL_POINT);
   const double unit   =AdjustedPoint();
   const double stops  =(double)SymbolInfoInteger(_Symbol,SYMBOL_TRADE_STOPS_LEVEL)*point;
   const double slPrice=Inp_Signal_StopLevel*unit;
   const double tpPrice=Inp_Signal_TakeLevel*unit;

   PrintFormat("%s on %s: 1 adjusted point = %.*f price (digits %d)",
               Inp_Expert_Title,_Symbol,digits,unit,digits);
   PrintFormat("  stop  %d -> %.*f price   take %d -> %.*f price",
               Inp_Signal_StopLevel,digits,slPrice,Inp_Signal_TakeLevel,digits,tpPrice);
   PrintFormat("  broker minimum stop distance: %.*f price (%d points)",
               digits,stops,(int)SymbolInfoInteger(_Symbol,SYMBOL_TRADE_STOPS_LEVEL));

   if(Inp_Signal_StopLevel<0 || Inp_Signal_TakeLevel<0)
     {
      Print("FATAL: stop and take levels cannot be negative");
      return false;
     }
   if(Inp_Signal_StopLevel==0)
      Print("WARNING: stop loss is 0 - the position is unprotected until the PSAR "
            "trail engages, which it cannot do until price moves in your favour. "
            "This is the stock sample's behaviour, stated so it is a choice.");

   if(stops>0.0 && slPrice>0.0 && slPrice<stops)
     {
      PrintFormat("FATAL: a %.*f stop is inside the broker's %.*f minimum on %s. Every "
                  "order would be rejected as 'Invalid stops' while the expert looked "
                  "healthy. Raise Inp_Signal_StopLevel to at least %d.",
                  digits,slPrice,digits,stops,_Symbol,
                  (int)MathCeil(stops/unit));
      return false;
     }
   if(stops>0.0 && tpPrice>0.0 && tpPrice<stops)
     {
      PrintFormat("FATAL: a %.*f target is inside the broker's %.*f minimum on %s. "
                  "Raise Inp_Signal_TakeLevel to at least %d.",
                  digits,tpPrice,digits,stops,_Symbol,(int)MathCeil(stops/unit));
      return false;
     }
   return true;
  }
//+------------------------------------------------------------------+
//| Initialization function of the expert                            |
//+------------------------------------------------------------------+
int OnInit(void)
  {
//--- validate the bracket BEFORE anything else, so a bad configuration
//--- fails loudly at attach instead of silently at every order.
   if(!ValidateBracket())
      return(INIT_PARAMETERS_INCORRECT);
   DayGuardReset(ExtDay);
//--- Say which gates are live. A filter that is silently off looks exactly like
//--- a filter that is on and never triggers, and this session has paid for that
//--- confusion enough times.
   PrintFormat("  gates: spread<=%d pts | tp>=%.1fx spread | cooldown %d bars | "
               "session %02d:00-%02d:00 (friday %d) | daily loss %.2f | max trades %d",
               Inp_Filter_MaxSpreadPoints,Inp_Filter_MinTpSpreadMult,Inp_Filter_CooldownBars,
               Inp_Filter_SessionStart,Inp_Filter_SessionEnd,Inp_Filter_FridayEnd,
               Inp_Filter_DailyLossLimit,Inp_Filter_MaxTradesPerDay);
   if(Inp_Filter_MaxSpreadPoints==0 && Inp_Filter_MinTpSpreadMult<=0.0 &&
      Inp_Filter_CooldownBars==0 && Inp_Filter_DailyLossLimit<=0.0 &&
      Inp_Filter_MaxTradesPerDay==0 &&
      Inp_Filter_SessionStart==Inp_Filter_SessionEnd && Inp_Filter_FridayEnd<0)
      Print("  ALL GATES OFF - this is the unfiltered baseline (D-142: PF 1.06 / "
            "0.77 / 0.81 across three windows, i.e. no edge)");
//--- Initializing expert
   if(!ExtExpert.Init(Symbol(),Period(),Expert_EveryTick,Expert_MagicNumber))
     {
      printf(__FUNCTION__+": error initializing expert");
      ExtExpert.Deinit();
      return(-1);
     }
//--- Creation of signal object
   CFilteredSignalMA *signal=new CFilteredSignalMA;
   if(signal==NULL)
     {
      printf(__FUNCTION__+": error creating signal");
      ExtExpert.Deinit();
      return(-2);
     }
//--- Add signal to expert (will be deleted automatically)
   if(!ExtExpert.InitSignal(signal))
     {
      printf(__FUNCTION__+": error initializing signal");
      ExtExpert.Deinit();
      return(-3);
     }
//--- Set signal parameters
   signal.PeriodMA(Inp_Signal_MA_Period);
   signal.Shift(Inp_Signal_MA_Shift);
   signal.Method(Inp_Signal_MA_Method);
   signal.Applied(Inp_Signal_MA_Applied);
//--- THE ADDITION. These must be set before ValidationSettings(), and they are
//--- what CExpertSignal::OpenLongParams/OpenShortParams turn into the order's
//--- sl and tp. Left at 0 they produce sl=0/tp=0, which is the stock behaviour.
   signal.StopLevel(Inp_Signal_StopLevel);
   signal.TakeLevel(Inp_Signal_TakeLevel);
//--- Pattern weights. Setting Pattern_0 to 0 removes the state-based entry that
//--- makes a stopped-out position re-enter on the very next bar.
   signal.Pattern_0(Inp_Signal_Pattern_0);
   signal.Pattern_1(Inp_Signal_Pattern_1);
   signal.Pattern_2(Inp_Signal_Pattern_2);
   signal.Pattern_3(Inp_Signal_Pattern_3);
   if(Inp_Signal_Pattern_0==0)
      Print("  pattern 0 disabled - entries come only from crossings/piercings, "
            "so a stop-out cannot immediately re-enter on a still-true state");
//--- Check signal parameters
   if(!signal.ValidationSettings())
     {
      printf(__FUNCTION__+": error signal parameters");
      ExtExpert.Deinit();
      return(-4);
     }
//--- Creation of trailing object
   CTrailingPSAR *trailing=new CTrailingPSAR;
   if(trailing==NULL)
     {
      printf(__FUNCTION__+": error creating trailing");
      ExtExpert.Deinit();
      return(-5);
     }
//--- Add trailing to expert (will be deleted automatically)
   if(!ExtExpert.InitTrailing(trailing))
     {
      printf(__FUNCTION__+": error initializing trailing");
      ExtExpert.Deinit();
      return(-6);
     }
//--- Set trailing parameters
   trailing.Step(Inp_Trailing_ParabolicSAR_Step);
   trailing.Maximum(Inp_Trailing_ParabolicSAR_Maximum);
//--- Check trailing parameters
   if(!trailing.ValidationSettings())
     {
      printf(__FUNCTION__+": error trailing parameters");
      ExtExpert.Deinit();
      return(-7);
     }
//--- Creation of money object
   CMoneyNone *money=new CMoneyNone;
   if(money==NULL)
     {
      printf(__FUNCTION__+": error creating money");
      ExtExpert.Deinit();
      return(-8);
     }
//--- Add money to expert (will be deleted automatically)
   if(!ExtExpert.InitMoney(money))
     {
      printf(__FUNCTION__+": error initializing money");
      ExtExpert.Deinit();
      return(-9);
     }
//--- Check money parameters
   if(!money.ValidationSettings())
     {
      printf(__FUNCTION__+": error money parameters");
      ExtExpert.Deinit();
      return(-10);
     }
//--- CMoneyNone sizes every trade at the symbol MINIMUM volume and ignores
//--- balance entirely. Worth knowing before reading any net-profit figure from
//--- this expert: only profit factor and drawdown percent mean anything.
   PrintFormat("  sizing: CMoneyNone - every trade is the symbol minimum (%.2f lots). "
               "Net P&L from this expert is not comparable with anything.",
               SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_MIN));
//--- Tuning of all necessary indicators
   if(!ExtExpert.InitIndicators())
     {
      printf(__FUNCTION__+": error initializing indicators");
      ExtExpert.Deinit();
      return(-11);
     }
//--- succeed
   return(INIT_SUCCEEDED);
  }
//+------------------------------------------------------------------+
//| Deinitialization function of the expert                          |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   ExtExpert.Deinit();
  }
//+------------------------------------------------------------------+
//| Function-event handler "tick"                                    |
//+------------------------------------------------------------------+
void OnTick(void)
  {
   ExtExpert.OnTick();
  }
//+------------------------------------------------------------------+
//| Function-event handler "trade"                                   |
//+------------------------------------------------------------------+
void OnTrade(void)
  {
   ExtExpert.OnTrade();
  }
//+------------------------------------------------------------------+
//| Function-event handler "timer"                                   |
//+------------------------------------------------------------------+
void OnTimer(void)
  {
   ExtExpert.OnTimer();
  }
//+------------------------------------------------------------------+
