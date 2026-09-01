//+------------------------------------------------------------------+
//| ScalpFilters.mqh                                                 |
//|                                                                  |
//| The gates an intraday scalper needs and the swing experts do not.|
//|                                                                  |
//| ====================================================================
//| WHY THIS FILE EXISTS AT ALL
//| ====================================================================
//| mt5/README.md records what was actually measured on 2.11 years of |
//| real XAUUSD bars: MACD made -$230,052 on M15 and +$190,186 on H1, |
//| breakout -$14,779 on M15 against +$136,477 on H1. The stated     |
//| cause is not the signal. It is that trade count roughly halves   |
//| per step to a slower interval while the $0.29 round-trip spread  |
//| is charged PER ROUND TRIP. Cost is the dominant term, and a      |
//| scalper is that term turned up.                                  |
//|                                                                  |
//| A scalping expert that treats spread the way the swing experts   |
//| do - an optional guard, off by default, "because enabling it     |
//| makes live diverge from the backtest" - would be repeating the   |
//| exact mistake the measurements already found. So here the cost   |
//| gate is MANDATORY and on by default, and it is expressed as a    |
//| ratio rather than an absolute: a target is only taken if it      |
//| clears the spread by a stated multiple. That is the one filter   |
//| the measured numbers actually argue for.                         |
//|                                                                  |
//| ====================================================================
//| DISTANCES ARE ATR-RELATIVE, NOT PERCENTAGES
//| ====================================================================
//| ProtectiveExits.mqh is anchored in percent of price because the  |
//| Python strategies it ports are. On XAUUSD near 4,600 its 0.5%    |
//| default stop is about $23. A scalp does not survive a $23 stop   |
//| being the smallest unit of risk it can express - that is a swing |
//| stop. Everything here is a multiple of ATR instead, which also   |
//| lets one set of inputs mean the same thing in a quiet session    |
//| and a violent one. The two modules are not interchangeable and   |
//| are deliberately not merged.                                     |
//|                                                                  |
//| ====================================================================
//| THE GOVERNORS ARE NOT OPTIONAL DECORATION
//| ====================================================================
//| A swing expert taking one trade a week cannot lose an account in |
//| an afternoon. A scalper can, and the usual mechanism is not one  |
//| bad trade but forty of them after the edge stopped working. The  |
//| daily loss limit and the trade counter are the only things in    |
//| this system that bound a bad DAY rather than a bad TRADE.        |
//+------------------------------------------------------------------+
#ifndef ALGOGOLD_SCALP_FILTERS_MQH
#define ALGOGOLD_SCALP_FILTERS_MQH

//+------------------------------------------------------------------+
//| Current spread expressed in PRICE, not points.                    |
//|                                                                   |
//| Read from the live book rather than from SYMBOL_SPREAD, because   |
//| SYMBOL_SPREAD is an integer of points and rounds a 2.7-point book |
//| to 2 or 3. At scalping distances that rounding is a real fraction |
//| of the edge being tested against.                                 |
//+------------------------------------------------------------------+
double SpreadPrice(const string symbol)
  {
   const double bid = SymbolInfoDouble(symbol,SYMBOL_BID);
   const double ask = SymbolInfoDouble(symbol,SYMBOL_ASK);
   if(bid<=0.0 || ask<=0.0 || ask<bid)
      return 0.0;
   return ask-bid;
  }

//+------------------------------------------------------------------+
//| SESSION GATE                                                      |
//|                                                                   |
//| Hours are SERVER hours, which is what the chart shows and what    |
//| every broker-side timestamp uses. They are not your wall clock,   |
//| and the offset changes twice a year on both sides independently.  |
//| The expert prints the current server hour on init so the window   |
//| can be set by looking rather than by arithmetic.                  |
//|                                                                   |
//| Windows may wrap midnight (start 22, end 6 is a valid Asia        |
//| window), so the comparison is wrap-aware rather than a plain      |
//| range test.                                                       |
//|                                                                   |
//| The Friday cutoff exists because holding a scalp into the weekend |
//| gap converts a position with a 10-point stop into one with an     |
//| unbounded one - the gap opens past the stop and fills wherever    |
//| the book reopens.                                                 |
//+------------------------------------------------------------------+
bool HourInWindow(const int hour,const int startHour,const int endHour)
  {
//--- start == end means "all day", not "one instant".
   if(startHour==endHour)
      return true;
   if(startHour < endHour)
      return (hour>=startHour && hour<endHour);
//--- Wrapped window: 22..6 is 22,23,0,1,..,5.
   return (hour>=startHour || hour<endHour);
  }

//+------------------------------------------------------------------+
//| Whether NEW entries are allowed at `when`.                        |
//|                                                                   |
//| `fridayEndHour` < 0 disables the Friday cutoff. Saturday and      |
//| Sunday are refused outright: a broker quoting then is quoting a   |
//| book nobody is making.                                            |
//+------------------------------------------------------------------+
bool SessionAllowsEntry(const datetime when,const int startHour,const int endHour,
                        const int fridayEndHour,string &why)
  {
   MqlDateTime t;
   TimeToStruct(when,t);

   if(t.day_of_week==0 || t.day_of_week==6)
     {
      why = "weekend";
      return false;
     }
   if(!HourInWindow(t.hour,startHour,endHour))
     {
      why = StringFormat("outside the %02d:00-%02d:00 server window (now %02d:%02d)",
                         startHour,endHour,t.hour,t.min);
      return false;
     }
   if(fridayEndHour>=0 && t.day_of_week==5 && t.hour>=fridayEndHour)
     {
      why = StringFormat("past the Friday %02d:00 cutoff - a scalp held over the weekend "
                         "gap has an unbounded stop",fridayEndHour);
      return false;
     }
   why = "";
   return true;
  }

//+------------------------------------------------------------------+
//| DAILY GOVERNORS                                                   |
//|                                                                   |
//| Realised P&L is recomputed from the deal history each time rather |
//| than accumulated in a variable, for the same reason the trail is  |
//| replayed rather than persisted in Trader.mqh: an expert is        |
//| reloaded on recompile, on a chart change, on a terminal restart.  |
//| A counter held in memory would silently reset to zero mid-day and |
//| hand back a full loss budget that had already been spent - the    |
//| single most dangerous way for this particular guard to fail.      |
//+------------------------------------------------------------------+
struct DayGuard
  {
   datetime          dayStart;      // server midnight of the day being counted
   int               trades;        // our entry deals since dayStart
   double            realised;      // our profit+swap+commission since dayStart
   bool              halted;        // a limit has been hit; no new entries today
   string            haltReason;
  };

void DayGuardReset(DayGuard &g)
  {
   g.dayStart   = 0;
   g.trades     = 0;
   g.realised   = 0.0;
   g.halted     = false;
   g.haltReason = "";
  }

//+------------------------------------------------------------------+
//| Server midnight of the day `when` falls in.                       |
//+------------------------------------------------------------------+
datetime DayStartOf(const datetime when)
  {
   MqlDateTime t;
   TimeToStruct(when,t);
   t.hour = 0;
   t.min  = 0;
   t.sec  = 0;
   return StructToTime(t);
  }

//+------------------------------------------------------------------+
//| Re-read today's deals for `magic` on `symbol`.                    |
//|                                                                   |
//| Commission is summed on EVERY deal, entries included, because a   |
//| commission charged on the way in is money already gone whether or |
//| not the position has closed. Only profit and swap are exit-side.  |
//| Getting this wrong understates the day's loss by exactly the      |
//| entry commission, in the optimistic direction.                    |
//|                                                                   |
//| Open positions are NOT marked to market here. The limit is a      |
//| REALISED one: an unrealised drawdown that recovers should not     |
//| halt the day, and one that does not recover will realise itself   |
//| through the stop and be counted then.                             |
//+------------------------------------------------------------------+
void DayGuardRefresh(DayGuard &g,const string symbol,const long magic,const datetime now)
  {
   const datetime start = DayStartOf(now);
   if(start!=g.dayStart)
     {
      //--- A new server day. The halt does not survive it.
      g.dayStart   = start;
      g.halted     = false;
      g.haltReason = "";
     }

   g.trades   = 0;
   g.realised = 0.0;

//--- +1 day of headroom: the upper bound is inclusive and a deal timestamped
//--- in the same second as `now` is otherwise racy to catch.
   if(!HistorySelect(start,now+86400))
     {
      Print("WARNING: HistorySelect failed - the daily loss limit cannot be evaluated "
            "this bar and is treated as NOT hit. Check the terminal history depth.");
      return;
     }

   const int total = HistoryDealsTotal();
   for(int i=0; i<total; i++)
     {
      const ulong ticket = HistoryDealGetTicket(i);
      if(ticket==0)
         continue;
      if(HistoryDealGetString(ticket,DEAL_SYMBOL)!=symbol)
         continue;
      if(HistoryDealGetInteger(ticket,DEAL_MAGIC)!=magic)
         continue;

      const ENUM_DEAL_ENTRY entry = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(ticket,DEAL_ENTRY);

      g.realised += HistoryDealGetDouble(ticket,DEAL_COMMISSION);
      if(entry==DEAL_ENTRY_IN)
         g.trades++;
      else
         g.realised += HistoryDealGetDouble(ticket,DEAL_PROFIT)
                       + HistoryDealGetDouble(ticket,DEAL_SWAP);
     }
  }

//+------------------------------------------------------------------+
//| Whether the day's budget still permits a new entry.               |
//|                                                                   |
//| `lossLimit` is a POSITIVE number of account currency. 0 disables. |
//| `profitTarget` 0 disables. `maxTrades` 0 disables.                |
//|                                                                   |
//| Stopping on a profit target is not superstition here: it bounds   |
//| the number of round trips, and round trips are what the measured  |
//| numbers say costs are charged per.                                |
//+------------------------------------------------------------------+
bool DayGuardAllowsEntry(DayGuard &g,const double lossLimit,const double profitTarget,
                         const int maxTrades)
  {
   if(g.halted)
      return false;

   if(lossLimit>0.0 && g.realised <= -lossLimit)
     {
      g.halted     = true;
      g.haltReason = StringFormat("daily loss limit: realised %.2f is at or past -%.2f",
                                  g.realised,lossLimit);
      Print("HALTED FOR THE DAY - ",g.haltReason);
      return false;
     }
   if(profitTarget>0.0 && g.realised >= profitTarget)
     {
      g.halted     = true;
      g.haltReason = StringFormat("daily profit target: realised %.2f is at or past %.2f",
                                  g.realised,profitTarget);
      Print("HALTED FOR THE DAY - ",g.haltReason);
      return false;
     }
   if(maxTrades>0 && g.trades>=maxTrades)
     {
      g.halted     = true;
      g.haltReason = StringFormat("trade cap: %d entries today, limit %d",g.trades,maxTrades);
      Print("HALTED FOR THE DAY - ",g.haltReason);
      return false;
     }
   return true;
  }

//+------------------------------------------------------------------+
//| THE BRACKET                                                       |
//|                                                                   |
//| Stop and target distances in price, derived from ATR and then     |
//| forced through three constraints in a fixed order:                |
//|                                                                   |
//|  1. the broker's SYMBOL_TRADE_STOPS_LEVEL - a stop nearer than    |
//|     this is simply rejected, so the distance is widened to it;    |
//|  2. an explicit floor in points, because a stop one tick outside  |
//|     the stops level is still noise on any real book;              |
//|  3. the COST GATE - the target must clear the spread by           |
//|     `minTpSpreadMult`, or no trade is taken at all.               |
//|                                                                   |
//| Order matters. Widening the stop for (1) or (2) widens the target |
//| with it, so the gate in (3) is tested against the FINAL target    |
//| rather than the requested one. Testing the requested one would    |
//| pass trades whose real target had already been moved.             |
//|                                                                   |
//| The gate REJECTS rather than adjusts. Widening the target until   |
//| it clears the spread would silently turn a scalp into a swing     |
//| trade carrying a scalp's stop, which is the worst of both.        |
//+------------------------------------------------------------------+
struct ScalpBracket
  {
   bool              valid;
   double            stopDistance;   // price, always positive
   double            takeDistance;   // price, always positive
   string            reason;         // why !valid, or how it was adjusted
  };

ScalpBracket BuildBracket(const string symbol,const double atr,
                          const double atrStopMult,const double rewardRisk,
                          const int minStopPoints,const double minTpSpreadMult)
  {
   ScalpBracket b;
   b.valid        = false;
   b.stopDistance = 0.0;
   b.takeDistance = 0.0;
   b.reason       = "";

   if(atr<=0.0)
     {
      b.reason = "ATR is not positive - the indicator has not converged";
      return b;
     }

   const int    digits     = (int)SymbolInfoInteger(symbol,SYMBOL_DIGITS);
   const double point      = SymbolInfoDouble(symbol,SYMBOL_POINT);
   const double stopsLevel = (double)SymbolInfoInteger(symbol,SYMBOL_TRADE_STOPS_LEVEL)*point;

   double sl = atr*atrStopMult;

//--- (1) and (2): the two floors, widest wins.
   const double floorPrice = MathMax(stopsLevel, minStopPoints*point);
   if(floorPrice>0.0 && sl<floorPrice)
     {
      b.reason = StringFormat("stop widened from %.*f to %.*f by the broker stops level / "
                              "minimum floor",digits,sl,digits,floorPrice);
      sl = floorPrice;
     }

   double tp = sl*rewardRisk;

//--- The target has to satisfy the stops level too, or the order comes back
//--- with the SL accepted and the TP refused.
   if(tp < stopsLevel)
      tp = stopsLevel;

//--- (3) The cost gate, against the FINAL target.
   const double spread = SpreadPrice(symbol);
   if(minTpSpreadMult>0.0 && spread>0.0 && tp < spread*minTpSpreadMult)
     {
      b.reason = StringFormat("cost gate: target %.*f is under %.1fx the %.*f spread. At "
                              "this book the trade pays more in spread than the move it "
                              "is aiming at is worth",
                              digits,tp,minTpSpreadMult,digits,spread);
      return b;
     }

   b.valid        = true;
   b.stopDistance = sl;
   b.takeDistance = tp;
   return b;
  }

#endif // ALGOGOLD_SCALP_FILTERS_MQH
