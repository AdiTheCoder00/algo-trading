//+------------------------------------------------------------------+
//| ProtectiveExits.mqh                                              |
//|                                                                  |
//| A line-for-line port of algo/strategy/price_stop.py,             |
//| algo/strategy/trailing_profit_stop.py, and the sequencing in     |
//| algo/strategy/protective_exits.py.                               |
//|                                                                  |
//| Both experts share this file for exactly the reason the Python   |
//| shares its module: it is "the shared, tested piece that adds it  |
//| identically to both rather than two copies that could quietly    |
//| drift apart."                                                    |
//|                                                                  |
//| THE ORDER IS FIXED HERE, NOT PER-EXPERT                          |
//| Advance the trail peak first, then test the flat stop, then test |
//| the trail. A bar that crosses both is reported as the stop - the |
//| pessimistic reading algo/risk/exits.py already states.           |
//|                                                                  |
//| PERCENTAGES, NOT POINTS                                          |
//| Every level here is a percentage of a price, matching the Python |
//| inputs (stop_loss_pct, trail_activation_pct, trail_pct). On      |
//| XAUUSD near 4,600 a 0.5% stop is about $23, i.e. about 2,300     |
//| points. Do not read these as pips.                               |
//+------------------------------------------------------------------+
#ifndef ALGOGOLD_PROTECTIVE_EXITS_MQH
#define ALGOGOLD_PROTECTIVE_EXITS_MQH

//--- Which protective exit fired. Mirrors ExitKind in
//--- algo/strategy/protective_exits.py, which exists so a consumer never has
//--- to parse a human-readable reason string to learn what happened.
enum ExitKind
  {
   EXIT_NONE  = 0,
   EXIT_STOP  = 1,   // the flat, entry-anchored percentage stop
   EXIT_TRAIL = 2    // the armed trailing profit stop
  };

//--- The running state a trailing stop needs: which side, what it entered at,
//--- and the best (most favourable) price seen since. Mirrors TrailState.
struct TrailState
  {
   bool               active;
   ENUM_POSITION_TYPE side;
   double             entry;
   double             peak;
  };

//+------------------------------------------------------------------+
//| A fresh trail, peak seeded at entry - nothing banked yet.         |
//+------------------------------------------------------------------+
void TrailStart(TrailState &st,const double entry,const ENUM_POSITION_TYPE side)
  {
   st.active = true;
   st.side   = side;
   st.entry  = entry;
   st.peak   = entry;
  }

void TrailClear(TrailState &st)
  {
   st.active = false;
   st.side   = POSITION_TYPE_BUY;
   st.entry  = 0.0;
   st.peak   = 0.0;
  }

//+------------------------------------------------------------------+
//| Extend the peak to this bar best-case favourable price.           |
//|                                                                   |
//| The peak only ever moves further favourable - it never retreats    |
//| just because the bar own close came back in. A bar OHLC does not   |
//| say whether the high or the low printed first; this resolves that  |
//| the same direction every time (peak first, then test), which is    |
//| what a real trailing-stop order actually does.                     |
//+------------------------------------------------------------------+
void TrailAdvance(TrailState &st,const double high,const double low)
  {
   if(!st.active)
      return;
   if(st.side==POSITION_TYPE_BUY)
      st.peak = MathMax(st.peak, high);
   else
      st.peak = MathMin(st.peak, low);
  }

double TrailFavourableMovePct(const TrailState &st)
  {
   if(!st.active || st.entry<=0.0)
      return 0.0;
   const double move = (st.side==POSITION_TYPE_BUY)
                       ? (st.peak - st.entry)
                       : (st.entry - st.peak);
   return move / st.entry * 100.0;
  }

//+------------------------------------------------------------------+
//| Whether the peak has ever reached activationPct in the position    |
//| favour. Once true it stays true for this trail - the peak only     |
//| advances, so an activation that has fired cannot un-fire.          |
//+------------------------------------------------------------------+
bool TrailIsArmed(const TrailState &st,const double activationPct)
  {
   return st.active && TrailFavourableMovePct(st) >= activationPct;
  }

//+------------------------------------------------------------------+
//| Where the trailing stop currently sits: trailPct behind the peak,  |
//| CLAMPED so it can never sit worse than entry.                      |
//|                                                                    |
//| That clamp is the "cost to cost" invariant. Without it a large      |
//| enough trailPct relative to activationPct could give back more than |
//| the entire banked move, closing a genuine winner at a loss and      |
//| defeating the purpose of a *profit* trail. With it, the worst       |
//| outcome once a level is ever computed is a scratch at entry.        |
//|                                                                    |
//| Note it is a percentage of the CURRENT PEAK, not of entry - the way |
//| retail platforms present it - so the absolute distance widens as    |
//| the peak advances.                                                  |
//+------------------------------------------------------------------+
double TrailLevel(const TrailState &st,const double trailPct)
  {
   const double giveBack = st.peak * trailPct / 100.0;
   if(st.side==POSITION_TYPE_BUY)
      return MathMax(st.peak - giveBack, st.entry);
   return MathMin(st.peak + giveBack, st.entry);
  }

//+------------------------------------------------------------------+
//| Whether the trail is armed AND this bar range crossed it.          |
//| trailPct <= 0 means no trail is configured; always false, so a      |
//| caller need not branch on whether trailing is enabled.              |
//+------------------------------------------------------------------+
bool TrailTouched(const TrailState &st,const double high,const double low,
                  const double activationPct,const double trailPct)
  {
   if(trailPct<=0.0 || !TrailIsArmed(st,activationPct))
      return false;
   const double level = TrailLevel(st,trailPct);
   if(st.side==POSITION_TYPE_BUY)
      return low <= level;
   return high >= level;
  }

//+------------------------------------------------------------------+
//| The absolute price at which a stopPct adverse move from entry sits.|
//| A long stop sits below entry, a short one above it - the move is    |
//| against the position, not against the market own direction.         |
//+------------------------------------------------------------------+
double StopLossLevel(const double entry,const ENUM_POSITION_TYPE side,const double stopPct)
  {
   const double move = entry * stopPct / 100.0;
   return (side==POSITION_TYPE_BUY) ? entry-move : entry+move;
  }

//+------------------------------------------------------------------+
//| Whether the bar ACTUAL RANGE crossed the stop - not just close.    |
//|                                                                    |
//| Checking only the close would systematically understate how often   |
//| the stop fires: a bar that spikes through the level and closes back |
//| inside would never trigger a close-only check, even though a real   |
//| broker-side stop would have filled during that spike. Understating  |
//| a safety feature own frequency is the wrong direction to be         |
//| optimistic in.                                                     |
//+------------------------------------------------------------------+
bool StopTouched(const double high,const double low,const double entry,
                 const ENUM_POSITION_TYPE side,const double stopPct)
  {
   if(stopPct<=0.0)
      return false;
   const double level = StopLossLevel(entry,side,stopPct);
   if(side==POSITION_TYPE_BUY)
      return low <= level;
   return high >= level;
  }

//+------------------------------------------------------------------+
//| Advance the trail for this bar, then report the first exit that    |
//| fired. Mirrors ProtectiveExits.check().                            |
//|                                                                    |
//| Call every closed bar, BEFORE the expert entry logic and BEFORE its |
//| warmup gate - a held position must never go unprotected because the |
//| indicator that would eventually close it has not converged yet.     |
//|                                                                    |
//| Returns EXIT_NONE whenever flat, which also clears any trail so the |
//| next position starts one of its own.                               |
//+------------------------------------------------------------------+
ExitKind ProtectiveExitsCheck(TrailState &st,
                              const bool               hasPosition,
                              const ENUM_POSITION_TYPE side,
                              const double             entry,
                              const double             high,
                              const double             low,
                              const double             stopPct,
                              const double             activationPct,
                              const double             trailPct)
  {
   if(!hasPosition)
     {
      TrailClear(st);
      return EXIT_NONE;
     }

//--- Re-seed on a change of side *or* of entry price. Neither expert can
//--- reopen without a flat bar in between, so in practice only the side check
//--- ever fires; the entry check states that assumption instead of depending
//--- on it silently, since a stale peak would arm a trail against a price the
//--- current position never traded at.
   if(!st.active || st.side!=side || st.entry!=entry)
      TrailStart(st,entry,side);

   TrailAdvance(st,high,low);

   if(StopTouched(high,low,entry,side,stopPct))
      return EXIT_STOP;

   if(TrailTouched(st,high,low,activationPct,trailPct))
      return EXIT_TRAIL;

   return EXIT_NONE;
  }

//+------------------------------------------------------------------+
//| The single broker-side stop level that expresses both exits.       |
//|                                                                    |
//| The Python model checks the bar low/high precisely BECAUSE it is    |
//| modelling a real broker-side stop order firing intrabar. Live we    |
//| can simply place that order, so the two agree by construction       |
//| rather than by approximation.                                      |
//|                                                                    |
//| One SL slot, two levels: take whichever is nearer to price. For a   |
//| long the armed trail always sits at or above entry (the cost-to-    |
//| cost clamp) and the flat stop always below it, so an armed trail is |
//| always the nearer one - which is also the ordering                  |
//| ProtectiveExitsCheck enforces.                                     |
//|                                                                    |
//| Returns 0.0 when no protective exit is configured, meaning "no SL". |
//+------------------------------------------------------------------+
double ProtectiveStopPrice(const TrailState &st,
                           const ENUM_POSITION_TYPE side,
                           const double entry,
                           const double stopPct,
                           const double activationPct,
                           const double trailPct)
  {
   const bool haveStop  = (stopPct>0.0);
   const bool haveTrail = (trailPct>0.0 && TrailIsArmed(st,activationPct));

   if(!haveStop && !haveTrail)
      return 0.0;
   if(!haveTrail)
      return StopLossLevel(entry,side,stopPct);

   const double trail = TrailLevel(st,trailPct);
   if(!haveStop)
      return trail;

   const double flat = StopLossLevel(entry,side,stopPct);
   return (side==POSITION_TYPE_BUY) ? MathMax(flat,trail) : MathMin(flat,trail);
  }

//+------------------------------------------------------------------+
//| Human-readable, in the same words the Python signal reasons use.   |
//+------------------------------------------------------------------+
string ExitKindName(const ExitKind kind)
  {
   if(kind==EXIT_STOP)
      return "stop loss";
   if(kind==EXIT_TRAIL)
      return "trailing stop";
   return "none";
  }

#endif // ALGOGOLD_PROTECTIVE_EXITS_MQH
