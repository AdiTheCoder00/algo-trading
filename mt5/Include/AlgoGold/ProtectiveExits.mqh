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
//| the trails. A bar that crosses both is reported as the stop - the|
//| pessimistic reading algo/risk/exits.py already states.           |
//|                                                                  |
//| TWO PROFIT TRAILS, ORDERED BY LEVEL AND NOT BY PRECEDENCE        |
//| trailPct gives back a percentage of the PEAK PRICE. givebackFrac |
//| gives back a fraction of the BANKED MOVE: at 0.5 the level sits  |
//| exactly halfway between entry and peak, so the position closes   |
//| having handed back half of the best unrealised profit it ever    |
//| showed. Different rules, not one rule in two units - the first   |
//| scales with the instrument, the second with how well the trade   |
//| went - so both exist and a caller may run either or both.        |
//|                                                                  |
//| The flat stop wins ties by fiat, because a bar OHLC does not say |
//| whether its high or its low printed first. Between the two       |
//| trails there is no such ambiguity: both sit at or above entry    |
//| for a long once armed, and price reaches either only by          |
//| retreating from the peak - so it crosses the NEARER one first.   |
//| That is the one reported, and the one ProtectiveStopPrice places.|
//| Which is nearer depends on the inputs, so it is a comparison     |
//| rather than a fixed order.                                       |
//|                                                                  |
//| givebackFrac IS A FRACTION, NOT A PERCENT. 0.5 means half. It is |
//| dimensionless on purpose, so nobody reads it as "0.5% behind the |
//| peak" - which is what the same digits mean in trailPct, one      |
//| argument away.                                                   |
//|                                                                  |
//| Both new arguments default to 0.0, so the experts that took this |
//| module before the give-back trail existed compile and behave     |
//| exactly as they did.                                             |
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
   EXIT_NONE     = 0,
   EXIT_STOP     = 1,   // the flat, entry-anchored percentage stop
   EXIT_TRAIL    = 2,   // the armed trailing profit stop, % of the peak PRICE
   EXIT_GIVEBACK = 3    // the armed give-back trail, fraction of the BANKED MOVE
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
//| Where the give-back trail sits: givebackFrac of the BANKED MOVE    |
//| behind the peak, clamped so it never sits worse than entry.        |
//|                                                                    |
//| For a long: peak - frac*(peak - entry), which is the same number as |
//| entry + (1-frac)*(peak - entry). At frac = 0.5 those readings - give |
//| back half, keep half - coincide, and at every other value they do   |
//| not. This argument is the GIVE-BACK, so 0.25 surrenders a quarter   |
//| of the banked move and locks in three quarters of it.               |
//|                                                                    |
//| The clamp never actually binds for 0 <= frac <= 1; it is applied    |
//| anyway so the cost-to-cost invariant holds here for the same reason |
//| it holds in TrailLevel, and not because the caller passed a         |
//| fraction that happened to be in range.                              |
//+------------------------------------------------------------------+
double GivebackLevel(const TrailState &st,const double givebackFrac)
  {
   const double banked = (st.side==POSITION_TYPE_BUY)
                         ? (st.peak - st.entry)
                         : (st.entry - st.peak);
   const double giveBack = banked*givebackFrac;
   if(st.side==POSITION_TYPE_BUY)
      return MathMax(st.peak - giveBack, st.entry);
   return MathMin(st.peak + giveBack, st.entry);
  }

//+------------------------------------------------------------------+
//| Whether the give-back trail is armed AND this bar range crossed it.|
//|                                                                    |
//| Arms on the SAME activationPct gate as the percentage trail, and   |
//| that gate is not optional: without one, a position a dollar into   |
//| profit that gives back fifty cents has satisfied "handed back half |
//| its peak profit" and would close on its first bar of noise. The    |
//| fraction says how much of a banked move to surrender; activationPct |
//| says how much has to be banked before the question is worth asking. |
//|                                                                    |
//| givebackFrac <= 0 means no give-back trail is configured; always    |
//| false, mirroring TrailTouched so a caller need not branch first.    |
//+------------------------------------------------------------------+
bool GivebackTouched(const TrailState &st,const double high,const double low,
                     const double activationPct,const double givebackFrac)
  {
   if(givebackFrac<=0.0 || !TrailIsArmed(st,activationPct))
      return false;
   const double level = GivebackLevel(st,givebackFrac);
   if(st.side==POSITION_TYPE_BUY)
      return low <= level;
   return high >= level;
  }

//+------------------------------------------------------------------+
//| Whichever of two protective levels sits NEARER to price for `side`.|
//| For a long that is the higher one - price falling from the peak    |
//| reaches it first.                                                  |
//+------------------------------------------------------------------+
double NearerLevel(const ENUM_POSITION_TYPE side,const double a,const double b)
  {
   return (side==POSITION_TYPE_BUY) ? MathMax(a,b) : MathMin(a,b);
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
                              const double             trailPct,
                              const double             givebackFrac=0.0)
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

   bool hitTrail    = TrailTouched(st,high,low,activationPct,trailPct);
   bool hitGiveback = GivebackTouched(st,high,low,activationPct,givebackFrac);

//--- Both armed trails sit at or above entry for a long, and price only
//--- reaches either by retreating from the peak - so the NEARER level filled
//--- first. Report that one. See the header on why this is a comparison and
//--- not a fixed precedence.
   if(hitTrail && hitGiveback)
     {
      const double pctLevel  = TrailLevel(st,trailPct);
      const double giveLevel = GivebackLevel(st,givebackFrac);
      hitTrail    = (NearerLevel(side,pctLevel,giveLevel) == pctLevel);
      hitGiveback = !hitTrail;
     }

   if(hitTrail)
      return EXIT_TRAIL;
   if(hitGiveback)
      return EXIT_GIVEBACK;

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
//| One SL slot, up to three levels: take whichever is nearest to      |
//| price. For a long both armed trails sit at or above entry (the      |
//| cost-to-cost clamp) and the flat stop always below it, so an armed  |
//| trail is always nearer than the flat stop - which is also the       |
//| ordering ProtectiveExitsCheck enforces. Between the two trails the  |
//| nearer one is likewise the one that fills first, and the same       |
//| comparison picks it in both functions.                              |
//|                                                                    |
//| Returns 0.0 when no protective exit is configured, meaning "no SL". |
//+------------------------------------------------------------------+
double ProtectiveStopPrice(const TrailState &st,
                           const ENUM_POSITION_TYPE side,
                           const double entry,
                           const double stopPct,
                           const double activationPct,
                           const double trailPct,
                           const double givebackFrac=0.0)
  {
   const bool armed        = TrailIsArmed(st,activationPct);
   const bool haveStop     = (stopPct>0.0);
   const bool haveTrail    = (trailPct>0.0 && armed);
   const bool haveGiveback = (givebackFrac>0.0 && armed);

   if(!haveStop && !haveTrail && !haveGiveback)
      return 0.0;

//--- Fold the configured levels together, keeping the nearest. Written as a
//--- fold rather than as a branch per combination so adding a fourth level
//--- later cannot reintroduce a case nobody covered.
   double level = 0.0;
   bool   have  = false;
   if(haveStop)
     {
      level = StopLossLevel(entry,side,stopPct);
      have  = true;
     }
   if(haveTrail)
     {
      const double t = TrailLevel(st,trailPct);
      level = have ? NearerLevel(side,level,t) : t;
      have  = true;
     }
   if(haveGiveback)
     {
      const double g = GivebackLevel(st,givebackFrac);
      level = have ? NearerLevel(side,level,g) : g;
      have  = true;
     }
   return level;
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
   if(kind==EXIT_GIVEBACK)
      return "give-back stop";
   return "none";
  }

#endif // ALGOGOLD_PROTECTIVE_EXITS_MQH
