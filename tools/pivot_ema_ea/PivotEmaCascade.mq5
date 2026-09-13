//+------------------------------------------------------------------+
//|                                          PivotEmaCascade.mq5     |
//|  Fibonacci pivots + 10/20/50/100/200 EMA cascade, on M5.         |
//+------------------------------------------------------------------+
//
//  THE RULE
//
//    SELL  a candle CLOSES down through R3, R2, R1 or the pivot, then down
//          through the 10, 20, 50, 100 and finally the 200 EMA - after, or on
//          the very same candle. The close beyond the 200 EMA is the entry.
//    BUY   the mirror, arming on S3, S2, S1 or the pivot.
//    EXIT  the first candle that closes back beyond BOTH the 10 and the 20 EMA.
//
//  Pivots are TradingView's "Pivot Points Standard" with the type set to
//  Fibonacci, drawn from the PREVIOUS day: P = (H+L+C)/3, then P +/- ratio*(H-L)
//  for ratios 0.382, 0.618 and 1.000.
//
//  READ THIS BEFORE YOU RUN IT ON A LIVE ACCOUNT
//
//    This rule has been measured twice and showed no edge either time.
//      * XAUUSD, three months of M5 (docs/decisions.md D-157): 55 trades,
//        -3,399 per 1.00 lot.
//      * BTCUSD, D-140's three windows (D-158): 339 M5 trades. The headline was
//        +4,538 per 1 BTC, and it turned into -7,078 when the trading DAY was
//        re-cut from 17:00 New York to plain UTC.
//
//    That last point is the one that matters here, because this EA cannot avoid
//    it. The pivots come from the previous daily bar, so they depend entirely on
//    when YOUR BROKER ends its day - Vantage's server runs GMT+2/+3. A different
//    broker, or the same broker after a DST change, draws different R and S
//    lines from the same market and can flip the result's sign. Nothing in this
//    file is wrong about that; it is a property of the rule.
//
//    So InpEnableTrading defaults to FALSE. It runs, prints and alerts exactly
//    as it would trade, and places nothing until you set it to true.
//
//  MATCHING THE BACKTEST
//
//  This is a deliberate reimplementation of algo/strategy/pivot_ema_cascade.py
//  in a language that cannot import it, so the details that are easy to get
//  subtly different are called out where they happen:
//
//    * A crossing is CLOSE-based, never wick-based:
//        down:  prev_close >  level >= close
//        up:    prev_close <  level <= close
//    * The EMA compared against is THIS bar's EMA, not the previous bar's.
//    * The exit tests "closed beyond both", not "crossed both on this bar" - a
//      two-bar recovery is still an exit.
//    * No cascade is counted while a position is open. The next entry is a
//      fresh pattern, not the tail of the one already on.
//    * A close back through the last level crossed resets that cascade, and it
//      may re-arm on a pivot break on that same bar.
//    * Everything is decided on CLOSED bars only.
//
//+------------------------------------------------------------------+
#property copyright "algo-trading"
#property version   "1.00"
#property strict

#include <Trade/Trade.mqh>

//--- inputs -------------------------------------------------------------------
input bool   InpEnableTrading   = false;  // place real orders on a LIVE chart
                                          // (the Strategy Tester always trades)
input double InpLots            = 0.01;   // volume per trade
input double InpStopLossPct     = 0.5;    // protective stop, % of entry (0 = none)
input long   InpMagic           = 20260913;
input ulong  InpSlippagePoints  = 30;
input bool   InpAlerts          = true;   // popup/push on entry and exit
input bool   InpDrawPivots      = true;   // draw today's seven lines on the chart

//--- the five EMAs the rule names, fastest first ------------------------------
#define EMA_COUNT 5
int EmaPeriods[EMA_COUNT] = {10, 20, 50, 100, 200};

//--- Fibonacci ratios, in the order R1/S1, R2/S2, R3/S3 -----------------------
#define FIB_COUNT 3
double FibRatios[FIB_COUNT] = {0.382, 0.618, 1.000};

//--- state --------------------------------------------------------------------
int      g_ema_handle[EMA_COUNT];
CTrade   g_trade;
datetime g_last_bar_time = 0;   // the M5 bar we last acted on
datetime g_pivot_day     = 0;   // the D1 bar the current pivots came from

double   g_p, g_r1, g_r2, g_r3, g_s1, g_s2, g_s3;
bool     g_pivots_ready = false;
bool     g_warned_no_daily = false;

// One cascade per direction. `stage` is how many of the six steps are done:
// 0 = not armed, 1 = a pivot line is broken, 2..6 = through that many EMAs.
// `last` is the level most recently crossed - a close back through it resets.
struct Cascade
{
   int    stage;
   double last;
   string pivot_name;
};
Cascade g_short;
Cascade g_long;

//+------------------------------------------------------------------+
//| May this run place orders?                                        |
//|                                                                   |
//| InpEnableTrading exists to stop an unmeasured rule reaching a real |
//| account by accident. The Strategy Tester is not a real account, so |
//| applying it there bought nothing and cost everything: the first    |
//| tester run of this EA produced ZERO trades and looked like a       |
//| broken strategy rather than a switch left off. A backtest that     |
//| cannot trade is not a safer backtest, it is a useless one.         |
//+------------------------------------------------------------------+
bool TradingAllowed()
{
   return(InpEnableTrading || (bool)MQLInfoInteger(MQL_TESTER));
}

//--- diagnostics: how far every armed cascade got before it died.
//--- Counted so that "no trades" always says WHICH step stopped them,
//--- mirroring the funnel the Python study prints (see D-157).
int  g_reached[EMA_COUNT + 2];   // [1]=pivot break .. [6]=through the 200 EMA
int  g_signals = 0;              // cascades completed
int  g_orders  = 0;              // orders actually placed

void CreditAttempt(const int depth)
{
   for(int s = 1; s <= depth && s <= EMA_COUNT + 1; s++)
      g_reached[s]++;
}

void ResetCascade(Cascade &c)
{
   c.stage      = 0;
   c.last       = 0.0;
   c.pivot_name = "";
}

//+------------------------------------------------------------------+
int OnInit()
{
   if(Period() != PERIOD_M5)
      Print("PivotEmaCascade: WARNING - the rule names M5 and this chart is ",
            EnumToString((ENUM_TIMEFRAMES)Period()),
            ". It will run, but it is not the rule that was measured.");

   for(int i = 0; i < EMA_COUNT; i++)
   {
      g_ema_handle[i] = iMA(_Symbol, PERIOD_CURRENT, EmaPeriods[i], 0, MODE_EMA, PRICE_CLOSE);
      if(g_ema_handle[i] == INVALID_HANDLE)
      {
         Print("PivotEmaCascade: could not create the ", EmaPeriods[i], " EMA: ",
               GetLastError());
         return(INIT_FAILED);
      }
   }

   g_trade.SetExpertMagicNumber((ulong)InpMagic);
   g_trade.SetDeviationInPoints(InpSlippagePoints);
   g_trade.SetTypeFillingBySymbol(_Symbol);

   ResetCascade(g_short);
   ResetCascade(g_long);

   Print("PivotEmaCascade started on ", _Symbol, " ",
         EnumToString((ENUM_TIMEFRAMES)Period()),
         ". Trading is ",
         (MQLInfoInteger(MQL_TESTER) ? "ENABLED (Strategy Tester)"
                                     : (InpEnableTrading ? "ENABLED"
                                                         : "OFF - alert only, set "
                                                           "InpEnableTrading=true to place orders")),
         ".");
   Print("PivotEmaCascade: this rule measured NO EDGE on XAUUSD (D-157) and ",
         "BTCUSD (D-158), and its result flips when the broker's day boundary ",
         "moves. Treat every signal as information, not advice.");
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   for(int i = 0; i < EMA_COUNT; i++)
      if(g_ema_handle[i] != INVALID_HANDLE)
         IndicatorRelease(g_ema_handle[i]);
   ObjectsDeleteAll(0, "PEC_");
   ReportFunnel();
}

//+------------------------------------------------------------------+
//| Where the cascades died. Printed at the end of every run.         |
//|                                                                   |
//| A bare trade count cannot tell "the market never offered this"     |
//| from "a later step killed them all" from "the switch was off", and |
//| those want completely different responses. This says which.        |
//+------------------------------------------------------------------+
void ReportFunnel()
{
   Print("PivotEmaCascade ---- where the cascades got to ----");
   Print("  (in the Strategy Tester this prints to the tester's Journal tab, "
         "not Experts; Alert() does nothing there)");
   PrintFormat("  armed on a pivot line : %d", g_reached[1]);
   for(int i = 0; i < EMA_COUNT; i++)
      PrintFormat("  through the %3d EMA   : %d", EmaPeriods[i], g_reached[i + 2]);
   PrintFormat("  signals               : %d", g_signals);
   PrintFormat("  orders placed         : %d", g_orders);

   if(g_signals == 0 && g_reached[1] == 0)
      Print("  -> nothing ever armed. Either no pivot line was closed through in "
            "this range, or the previous DAILY bar was missing so no lines were "
            "drawn. Check the journal for a 'pivots from ...' line.");
   else if(g_signals == 0)
      Print("  -> cascades armed but none completed. The step where the numbers "
            "collapse above is the one the market did not deliver.");
   else if(g_orders == 0)
      Print("  -> signals fired but NO order was placed. On a live chart that is "
            "InpEnableTrading=false; otherwise check the journal for REJECTED.");
}

//+------------------------------------------------------------------+
//| Fibonacci pivots from the PREVIOUS completed daily bar.           |
//|                                                                   |
//| Redrawn only when that bar changes, so the levels are fixed before |
//| the day they are used on - which is the whole point of a pivot.    |
//+------------------------------------------------------------------+
bool RefreshPivots()
{
   datetime day = iTime(_Symbol, PERIOD_D1, 1);
   if(day == 0)
   {
      // Said ONCE, loudly. Without a previous daily bar there are no pivot
      // lines, so nothing can ever arm and the EA is inert with nothing in the
      // log to explain it - which is exactly how a working rule looks broken.
      if(!g_warned_no_daily)
      {
         g_warned_no_daily = true;
         Print("PivotEmaCascade: NO PREVIOUS DAILY BAR for ", _Symbol,
               " - no pivot lines can be drawn, so this run will take no trades. "
               "Open a D1 chart of this symbol to download that history "
               "(Tools > Options > Charts > Max bars in chart), then re-run.");
      }
      return(false);
   }
   if(day == g_pivot_day && g_pivots_ready)
      return(true);                  // same session, same lines

   double h = iHigh(_Symbol, PERIOD_D1, 1);
   double l = iLow(_Symbol, PERIOD_D1, 1);
   double c = iClose(_Symbol, PERIOD_D1, 1);
   if(h <= 0.0 || l <= 0.0 || h < l)
      return(false);

   g_p  = (h + l + c) / 3.0;
   double span = h - l;
   g_r1 = g_p + FibRatios[0] * span;
   g_r2 = g_p + FibRatios[1] * span;
   g_r3 = g_p + FibRatios[2] * span;
   g_s1 = g_p - FibRatios[0] * span;
   g_s2 = g_p - FibRatios[1] * span;
   g_s3 = g_p - FibRatios[2] * span;

   g_pivot_day    = day;
   g_pivots_ready = true;

   // A new session is a new set of lines, so a cascade counted against the old
   // ones is no longer making a claim about anything.
   CreditAttempt(g_short.stage);
   CreditAttempt(g_long.stage);
   ResetCascade(g_short);
   ResetCascade(g_long);

   if(InpDrawPivots)
      DrawPivots();

   PrintFormat("PivotEmaCascade: pivots from %s  P=%.*f R1=%.*f R2=%.*f R3=%.*f "
               "S1=%.*f S2=%.*f S3=%.*f",
               TimeToString(day, TIME_DATE),
               _Digits, g_p, _Digits, g_r1, _Digits, g_r2, _Digits, g_r3,
               _Digits, g_s1, _Digits, g_s2, _Digits, g_s3);
   return(true);
}

void DrawLine(const string name, const double price, const color clr)
{
   const string id = "PEC_" + name;
   if(ObjectFind(0, id) < 0)
      ObjectCreate(0, id, OBJ_HLINE, 0, 0, price);
   ObjectSetDouble(0, id, OBJPROP_PRICE, price);
   ObjectSetInteger(0, id, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, id, OBJPROP_STYLE, STYLE_DOT);
   ObjectSetInteger(0, id, OBJPROP_BACK, true);
   ObjectSetString(0, id, OBJPROP_TOOLTIP, name);
}

void DrawPivots()
{
   DrawLine("R3", g_r3, clrTomato);
   DrawLine("R2", g_r2, clrTomato);
   DrawLine("R1", g_r1, clrTomato);
   DrawLine("P",  g_p,  clrGold);
   DrawLine("S1", g_s1, clrMediumSeaGreen);
   DrawLine("S2", g_s2, clrMediumSeaGreen);
   DrawLine("S3", g_s3, clrMediumSeaGreen);
   ChartRedraw();
}

//+------------------------------------------------------------------+
//| A close-based crossing, in one cascade's direction.               |
//+------------------------------------------------------------------+
bool Crossed(const bool is_short, const double level,
             const double prev_close, const double close)
{
   if(is_short)
      return(prev_close > level && level >= close);
   return(prev_close < level && level <= close);
}

//+------------------------------------------------------------------+
//| The four levels a cascade may arm on, nearest the pivot last.     |
//|                                                                   |
//| The sides are NOT interchangeable: a short arms on resistance,     |
//| a long on support, and both share P. A short armed on an S-line    |
//| would be price reaching support inside a fall it has already made, |
//| not price breaking the resistance the fall began at.               |
//+------------------------------------------------------------------+
void ArmLevels(const bool is_short, double &levels[], string &names[])
{
   ArrayResize(levels, 4);
   ArrayResize(names, 4);
   if(is_short)
   {
      levels[0] = g_p;  names[0] = "P";
      levels[1] = g_r1; names[1] = "R1";
      levels[2] = g_r2; names[2] = "R2";
      levels[3] = g_r3; names[3] = "R3";
   }
   else
   {
      levels[0] = g_s3; names[0] = "S3";
      levels[1] = g_s2; names[1] = "S2";
      levels[2] = g_s1; names[2] = "S1";
      levels[3] = g_p;  names[3] = "P";
   }
}

//+------------------------------------------------------------------+
//| Walk one direction's cascade as far as this bar's close allows.   |
//| Returns true when it completed - that is the entry.               |
//+------------------------------------------------------------------+
bool Advance(const bool is_short, const double close, const double prev_close,
             const double &emas[])
{
   Cascade c;
   if(is_short) c = g_short; else c = g_long;

   if(c.stage > 0)
   {
      // A close back through the last level crossed falsifies the claim this
      // cascade is making, so the count starts again - possibly from a pivot
      // break on this very bar, which the block below still allows.
      bool retraced = (is_short ? (close > c.last) : (close < c.last));
      if(retraced)
      {
         CreditAttempt(c.stage);   // this attempt died here, at this depth
         ResetCascade(c);
      }
   }

   if(c.stage == 0)
   {
      double levels[]; string names[];
      ArmLevels(is_short, levels, names);
      for(int i = 0; i < ArraySize(levels); i++)
      {
         if(Crossed(is_short, levels[i], prev_close, close))
         {
            c.stage      = 1;
            c.last       = levels[i];
            c.pivot_name = names[i];
            break;
         }
      }
   }

   // "after, or on the same candle": keep stepping while this one close is
   // already beyond the next EMA in the sequence.
   while(c.stage > 0 && c.stage <= EMA_COUNT)
   {
      double level = emas[c.stage - 1];
      if(!Crossed(is_short, level, prev_close, close))
         break;
      c.stage++;
      c.last = level;
   }

   bool completed = (c.stage == EMA_COUNT + 1);
   string began_at = c.pivot_name;
   if(completed)
   {
      CreditAttempt(EMA_COUNT + 1);   // reached every step, by definition
      ResetCascade(c);
   }

   if(is_short) g_short = c; else g_long = c;

   if(completed)
      PrintFormat("PivotEmaCascade: %s cascade complete - close %.*f %s the %d EMA "
                  "%.*f, from the %s pivot",
                  (is_short ? "SHORT" : "LONG"), _Digits, close,
                  (is_short ? "below" : "above"), EmaPeriods[EMA_COUNT - 1],
                  _Digits, emas[EMA_COUNT - 1], began_at);
   return(completed);
}

//+------------------------------------------------------------------+
//| The position this EA owns on this symbol, if any.                 |
//+------------------------------------------------------------------+
bool HasPosition(ENUM_POSITION_TYPE &type)
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != InpMagic)
         continue;
      type = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
      return(true);
   }
   return(false);
}

void Announce(const string text)
{
   Print("PivotEmaCascade: ", text);
   if(InpAlerts)
      Alert(_Symbol, " ", text);
}

//+------------------------------------------------------------------+
void OpenTrade(const bool is_short, const double close)
{
   const string what = (is_short ? "SELL" : "BUY");
   g_signals++;
   if(!TradingAllowed())
   {
      Announce(what + " signal at " + DoubleToString(close, _Digits) +
               " - not placed, InpEnableTrading is false");
      return;
   }

   double price = (is_short ? SymbolInfoDouble(_Symbol, SYMBOL_BID)
                            : SymbolInfoDouble(_Symbol, SYMBOL_ASK));
   double sl = 0.0;
   if(InpStopLossPct > 0.0)
   {
      double distance = price * InpStopLossPct / 100.0;
      sl = (is_short ? price + distance : price - distance);
      sl = NormalizeDouble(sl, _Digits);
   }

   bool ok = (is_short ? g_trade.Sell(InpLots, _Symbol, 0.0, sl, 0.0, "pivot/EMA cascade")
                       : g_trade.Buy(InpLots, _Symbol, 0.0, sl, 0.0, "pivot/EMA cascade"));
   if(ok)
   {
      g_orders++;
      Announce(what + " " + DoubleToString(InpLots, 2) + " at " +
               DoubleToString(price, _Digits));
   }
   else
      Print("PivotEmaCascade: ", what, " REJECTED - retcode ", g_trade.ResultRetcode(),
            " ", g_trade.ResultRetcodeDescription());
}

void CloseTrade(const string why)
{
   if(!TradingAllowed())
   {
      Announce("EXIT signal (" + why + ") - nothing to close, trading is off");
      return;
   }
   if(g_trade.PositionClose(_Symbol))
      Announce("EXIT - " + why);
   else
      Print("PivotEmaCascade: close REJECTED - retcode ", g_trade.ResultRetcode(),
            " ", g_trade.ResultRetcodeDescription());
}

//+------------------------------------------------------------------+
//| Everything is decided on the newest CLOSED bar.                   |
//+------------------------------------------------------------------+
void OnTick()
{
   datetime bar_time = iTime(_Symbol, PERIOD_CURRENT, 0);
   if(bar_time == 0 || bar_time == g_last_bar_time)
      return;                       // still inside the forming bar

   // Two closed bars are needed: shift 1 is the one that just closed and
   // shift 2 supplies the previous close a crossing is measured against.
   if(Bars(_Symbol, PERIOD_CURRENT) < EmaPeriods[EMA_COUNT - 1] + 3)
      return;

   double emas[EMA_COUNT];
   for(int i = 0; i < EMA_COUNT; i++)
   {
      double buf[];
      if(CopyBuffer(g_ema_handle[i], 0, 1, 1, buf) != 1)
         return;                    // indicator not ready; try again next tick
      emas[i] = buf[0];
   }

   double close      = iClose(_Symbol, PERIOD_CURRENT, 1);
   double prev_close = iClose(_Symbol, PERIOD_CURRENT, 2);
   if(close <= 0.0 || prev_close <= 0.0)
      return;

   g_last_bar_time = bar_time;      // this bar is now handled, whatever follows

   if(!RefreshPivots())
      return;                       // no complete previous day, so no lines

   ENUM_POSITION_TYPE held;
   if(HasPosition(held))
   {
      // No cascade is counted while a position is open.
      CreditAttempt(g_short.stage);
      CreditAttempt(g_long.stage);
      ResetCascade(g_short);
      ResetCascade(g_long);

      // "Closed back beyond BOTH", not "crossed both on this bar" - a two-bar
      // recovery is still the exit the rule describes.
      bool done = (held == POSITION_TYPE_BUY)
                  ? (close < emas[0] && close < emas[1])
                  : (close > emas[0] && close > emas[1]);
      if(done)
         CloseTrade(StringFormat("close %.*f back %s the %d and %d EMA",
                                 _Digits, close,
                                 (held == POSITION_TYPE_BUY ? "below" : "above"),
                                 EmaPeriods[0], EmaPeriods[1]));
      return;
   }

   // Short first, arbitrarily but consistently: the two cannot both complete on
   // one bar, since one needs a close below every EMA and the other above.
   if(Advance(true, close, prev_close, emas))
   {
      OpenTrade(true, close);
      return;
   }
   if(Advance(false, close, prev_close, emas))
      OpenTrade(false, close);
}
//+------------------------------------------------------------------+
