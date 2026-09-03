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
#property description "Units are adjusted points (pips); resolved price distances"
#property description "are printed on init and validated against the broker minimum."
//+------------------------------------------------------------------+
//| Include                                                          |
//+------------------------------------------------------------------+
#include <Expert\Expert.mqh>
#include <Expert\Signal\SignalMA.mqh>
#include <Expert\Trailing\TrailingParabolicSAR.mqh>
#include <Expert\Money\MoneyNone.mqh>
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
//+------------------------------------------------------------------+
//| Global expert object                                             |
//+------------------------------------------------------------------+
CExpert ExtExpert;
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
//--- Initializing expert
   if(!ExtExpert.Init(Symbol(),Period(),Expert_EveryTick,Expert_MagicNumber))
     {
      printf(__FUNCTION__+": error initializing expert");
      ExtExpert.Deinit();
      return(-1);
     }
//--- Creation of signal object
   CSignalMA *signal=new CSignalMA;
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
