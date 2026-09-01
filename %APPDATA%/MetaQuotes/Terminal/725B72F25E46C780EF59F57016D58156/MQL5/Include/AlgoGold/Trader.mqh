//+------------------------------------------------------------------+
//| Trader.mqh                                                       |
//|                                                                  |
//| The execution plumbing both experts share. Ports the parts of    |
//| algo/execution/mt5_broker.py that a terminal-side expert still   |
//| needs, and drops the parts it does not.                          |
//|                                                                  |
//| THE COMMENT FIELD IS NOT A TAG                                   |
//| MT5 overwrites an order comment: deals come back carrying         |
//| "[sl 4641.92]", "[tp 4635.00]" or "closePosition" in place of     |
//| whatever was sent. So the comment here is cosmetic only, and      |
//| MAGIC is the sole identity of this expert orders - the same       |
//| conclusion mt5_broker.py reached against the live account.        |
//|                                                                  |
//| A DISTINCT MAGIC PER EXPERT, AND NEITHER IS THE PYTHON ONE        |
//| algo/execution/mt5_broker.py claims magic 20260828. If an expert  |
//| used that number, the Python adapter reconciler would adopt the   |
//| expert positions as its own and try to manage them. Each expert   |
//| therefore ships its own magic and none of them is 20260828. Check |
//| this before running an expert alongside `algo mt5`.               |
//|                                                                  |
//| THE ACCOUNT MAY BE HEDGING, AND THE STRATEGY LOGIC NETS           |
//| positions_get() returns an independent ticket per trade, while    |
//| both strategies reason about one signed net position. Tickets are |
//| therefore aggregated here into a signed volume and a              |
//| volume-weighted entry - the same arithmetic Position.average_price|
//| does. Two opposing tickets netting to zero still pay financing and|
//| still hold spread, so that case is reported rather than hidden.   |
//|                                                                  |
//| SIZES ARE IN OUNCES THERE AND LOTS HERE                           |
//| One engine lot in the Python is one troy ounce; MT5 sizes XAUUSD  |
//| in 100-ounce lots with a 0.01 step. The Python default of 100     |
//| engine lots is 1.00 MT5 lots. Inputs here are in MT5 lots, which  |
//| is what the terminal shows, and both units are printed on init so |
//| the hundredfold error has to be looked at to be made.            |
//+------------------------------------------------------------------+
#ifndef ALGOGOLD_TRADER_MQH
#define ALGOGOLD_TRADER_MQH

#include <Trade\Trade.mqh>

//--- The magic claimed by algo/execution/mt5_broker.py. Reserved, never used
//--- by an expert; OnInit refuses to start if an input collides with it.
#define ALGOGOLD_PYTHON_MAGIC 20260828

//--- Ounces per MT5 lot on XAUUSD (SYMBOL_TRADE_CONTRACT_SIZE). Read from the
//--- symbol at init rather than assumed; this is only the fallback for the
//--- log line if the terminal reports nothing.
#define ALGOGOLD_FALLBACK_CONTRACT_SIZE 100.0

//+------------------------------------------------------------------+
//| One netted view of everything this expert holds on one symbol.    |
//+------------------------------------------------------------------+
struct GoldPosition
  {
   bool               exists;      // net volume is non-zero
   ENUM_POSITION_TYPE side;        // meaningless when !exists
   double             volume;      // absolute net volume, in MT5 lots
   double             entry;       // volume-weighted average entry price
   datetime           openTime;    // earliest of our tickets, for trail replay
   int                tickets;     // how many of our tickets are open
   bool               opposing;    // more than one, on both sides
  };

//+------------------------------------------------------------------+
//| CGoldTrader                                                       |
//+------------------------------------------------------------------+
class CGoldTrader
  {
private:
   CTrade            m_trade;
   string            m_symbol;
   long              m_magic;
   double            m_lots;
   string            m_tag;
   int               m_digits;
   double            m_point;

public:
                     CGoldTrader(void): m_symbol(""),m_magic(0),m_lots(0.0),m_tag(""),m_digits(2),m_point(0.01) {}

   bool              Init(const string symbol,const long magic,const double lots,
                          const ulong slippagePoints,const string tag);
   GoldPosition      Snapshot(void) const;
   bool              Open(const ENUM_POSITION_TYPE side,const string reason);
   bool              OpenBracket(const ENUM_POSITION_TYPE side,const double volume,
                                 const double stopDistance,const double takeDistance,
                                 const string reason);
   bool              CloseAll(const string reason);
   bool              ApplyStop(const double level);
   double            NormaliseVolume(const double lots) const;
   double            LotsForRisk(const double riskMoney,const double stopDistance) const;
   int               Digits(void) const { return m_digits; }
   double            Point(void) const { return m_point; }
   double            Lots(void) const { return m_lots; }
   string            Symbol(void) const { return m_symbol; }
   long              Magic(void) const { return m_magic; }
  };

//+------------------------------------------------------------------+
//| Wire up CTrade and read the symbol own terms. Filling mode is     |
//| READ, not assumed: symbol_info("XAUUSD").filling_mode reports IOC |
//| only on the Vantage account, and sending FOK there gets the order |
//| rejected outright.                                                |
//+------------------------------------------------------------------+
bool CGoldTrader::Init(const string symbol,const long magic,const double lots,
                       const ulong slippagePoints,const string tag)
  {
   m_symbol = symbol;
   m_magic  = magic;
   m_tag    = tag;

   if(!SymbolInfoInteger(m_symbol,SYMBOL_SELECT))
     {
      if(!SymbolSelect(m_symbol,true))
        {
         Print("FATAL: cannot select symbol ",m_symbol);
         return false;
        }
     }

   m_digits = (int)SymbolInfoInteger(m_symbol,SYMBOL_DIGITS);
   m_point  = SymbolInfoDouble(m_symbol,SYMBOL_POINT);

   m_lots = NormaliseVolume(lots);
   if(m_lots<=0.0)
     {
      Print("FATAL: requested volume ",DoubleToString(lots,2),
            " normalises to zero against the symbol volume step");
      return false;
     }
   if(MathAbs(m_lots-lots) > 1e-8)
      PrintFormat("volume %.2f adjusted to %.2f by the symbol volume step/min/max",
                  lots,m_lots);

   m_trade.SetExpertMagicNumber(m_magic);
   m_trade.SetDeviationInPoints(slippagePoints);
   m_trade.SetTypeFillingBySymbol(m_symbol);
   m_trade.LogLevel(LOG_LEVEL_ERRORS);

   const double contract = SymbolInfoDouble(m_symbol,SYMBOL_TRADE_CONTRACT_SIZE);
   const double ounces   = m_lots * (contract>0.0 ? contract : ALGOGOLD_FALLBACK_CONTRACT_SIZE);
   PrintFormat("size: %.2f MT5 lots = %.0f troy ounces = %.0f engine lots (Python default is 100)",
               m_lots,ounces,ounces);
   return true;
  }

//+------------------------------------------------------------------+
//| Clamp to volume_min/volume_max and round to volume_step.          |
//+------------------------------------------------------------------+
double CGoldTrader::NormaliseVolume(const double lots) const
  {
   const double vmin = SymbolInfoDouble(m_symbol,SYMBOL_VOLUME_MIN);
   const double vmax = SymbolInfoDouble(m_symbol,SYMBOL_VOLUME_MAX);
   const double step = SymbolInfoDouble(m_symbol,SYMBOL_VOLUME_STEP);

   double v = lots;
   if(step>0.0)
      v = MathRound(v/step)*step;
   if(vmin>0.0 && v<vmin)
      v = vmin;
   if(vmax>0.0 && v>vmax)
      v = vmax;
//--- volume_step is typically 0.01; two decimals is enough to kill the
//--- floating-point residue MathRound leaves behind.
   return NormalizeDouble(v,2);
  }

//+------------------------------------------------------------------+
//| Aggregate every ticket of ours on this symbol into one net view.  |
//+------------------------------------------------------------------+
GoldPosition CGoldTrader::Snapshot(void) const
  {
   GoldPosition snap;
   snap.exists   = false;
   snap.side     = POSITION_TYPE_BUY;
   snap.volume   = 0.0;
   snap.entry    = 0.0;
   snap.openTime = 0;
   snap.tickets  = 0;
   snap.opposing = false;

   double netVolume = 0.0;
   double costBasis = 0.0;
   int    longs = 0, shorts = 0;

   for(int i=PositionsTotal()-1; i>=0; i--)
     {
      const string sym = PositionGetSymbol(i);
      if(sym!=m_symbol)
         continue;
      if(PositionGetInteger(POSITION_MAGIC)!=m_magic)
         continue;

      const ENUM_POSITION_TYPE type = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
      const double volume = PositionGetDouble(POSITION_VOLUME);
      const double price  = PositionGetDouble(POSITION_PRICE_OPEN);
      const datetime opened = (datetime)PositionGetInteger(POSITION_TIME);

      const double signed_volume = (type==POSITION_TYPE_BUY) ? volume : -volume;
      netVolume += signed_volume;
      costBasis += signed_volume * price;

      if(type==POSITION_TYPE_BUY)
         longs++;
      else
         shorts++;

      snap.tickets++;
      if(snap.openTime==0 || opened<snap.openTime)
         snap.openTime = opened;
     }

   snap.opposing = (longs>0 && shorts>0);

//--- A netted zero is flat for the strategy but is NOT nothing: two opposing
//--- tickets both pay financing and both hold spread. Say so rather than let
//--- the netting hide it.
   if(MathAbs(netVolume) < 1e-8)
     {
      if(snap.tickets>0)
         PrintFormat("WARNING: %d ticket(s) of ours net to zero volume - both legs still "
                     "pay swap and spread. Strategy logic will treat this as flat.",
                     snap.tickets);
      return snap;
     }

   snap.exists = true;
   snap.side   = (netVolume>0.0) ? POSITION_TYPE_BUY : POSITION_TYPE_SELL;
   snap.volume = MathAbs(netVolume);
   snap.entry  = costBasis / netVolume;   // both signs cancel: a true average
   return snap;
  }

//+------------------------------------------------------------------+
//| Market order in `side`. Neither strategy ever reverses in one     |
//| step, so this is only ever called from flat.                      |
//+------------------------------------------------------------------+
bool CGoldTrader::Open(const ENUM_POSITION_TYPE side,const string reason)
  {
   bool ok = false;
   if(side==POSITION_TYPE_BUY)
      ok = m_trade.Buy(m_lots,m_symbol,0.0,0.0,0.0,m_tag);
   else
      ok = m_trade.Sell(m_lots,m_symbol,0.0,0.0,0.0,m_tag);

   if(!ok)
     {
      PrintFormat("OPEN %s REJECTED: retcode %d (%s) - %s",
                  (side==POSITION_TYPE_BUY?"BUY":"SELL"),
                  m_trade.ResultRetcode(),m_trade.ResultRetcodeDescription(),reason);
      return false;
     }

   PrintFormat("OPEN %s %.2f lots @ %s - %s",
               (side==POSITION_TYPE_BUY?"BUY":"SELL"),
               m_lots,DoubleToString(m_trade.ResultPrice(),m_digits),reason);
   return true;
  }

//+------------------------------------------------------------------+
//| Volume that risks `riskMoney` if `stopDistance` is hit.           |
//|                                                                   |
//| Converted through SYMBOL_TRADE_TICK_VALUE / SYMBOL_TRADE_TICK_SIZE|
//| rather than through the contract size, because tick value is      |
//| already denominated in the ACCOUNT currency. Doing it from the    |
//| contract size instead would be correct only while the quote       |
//| currency and the account currency happen to be the same - true    |
//| for a USD account on XAUUSD, and quietly wrong the moment either  |
//| changes.                                                          |
//|                                                                   |
//| Returns 0.0 when the symbol does not report enough to size        |
//| safely. The caller must treat that as "do not trade", never as    |
//| "use the minimum" - a fallback lot size is how a sizing bug turns |
//| into a position.                                                  |
//+------------------------------------------------------------------+
double CGoldTrader::LotsForRisk(const double riskMoney,const double stopDistance) const
  {
   if(riskMoney<=0.0 || stopDistance<=0.0)
      return 0.0;

   const double tickValue = SymbolInfoDouble(m_symbol,SYMBOL_TRADE_TICK_VALUE);
   const double tickSize  = SymbolInfoDouble(m_symbol,SYMBOL_TRADE_TICK_SIZE);
   if(tickValue<=0.0 || tickSize<=0.0)
     {
      PrintFormat("cannot size by risk: %s reports tick value %.5f, tick size %.5f",
                  m_symbol,tickValue,tickSize);
      return 0.0;
     }

//--- What one lot loses if the stop is hit, in account currency.
   const double lossPerLot = (stopDistance/tickSize)*tickValue;
   if(lossPerLot<=0.0)
      return 0.0;

   const double raw = riskMoney/lossPerLot;
   const double normalised = NormaliseVolume(raw);

//--- NormaliseVolume clamps UP to volume_min. That is the one direction a risk
//--- budget must not be silently exceeded in, so it is reported rather than
//--- swallowed: the trade still goes, but the log says what it actually risks.
   const double vmin = SymbolInfoDouble(m_symbol,SYMBOL_VOLUME_MIN);
   if(raw < vmin && vmin>0.0)
      PrintFormat("WARNING: risk %.2f over a %.*f stop wants %.4f lots, below the %.2f "
                  "minimum. Trading %.2f lots, which risks %.2f - MORE than asked.",
                  riskMoney,m_digits,stopDistance,raw,vmin,normalised,
                  normalised*lossPerLot);
   return normalised;
  }

//+------------------------------------------------------------------+
//| Market order with the stop and target attached to the SAME        |
//| request.                                                          |
//|                                                                   |
//| Not Open() followed by ApplyStop(). Between those two calls the   |
//| position exists with no stop on it, and the gap is not            |
//| theoretical - it spans a server round trip, which is exactly when |
//| the fast move that motivated the entry is still moving. A scalp   |
//| whose stop is a few ATR-tenths away cannot afford to be naked for |
//| a round trip, so CTrade is handed both levels up front and the    |
//| broker either accepts the whole bracket or rejects the whole      |
//| order.                                                            |
//|                                                                   |
//| Distances in, absolute prices out: the caller reasons in ATR      |
//| multiples and never has to know which side of the book its own    |
//| side is filled on.                                                |
//+------------------------------------------------------------------+
bool CGoldTrader::OpenBracket(const ENUM_POSITION_TYPE side,const double volume,
                              const double stopDistance,const double takeDistance,
                              const string reason)
  {
   if(volume<=0.0)
     {
      PrintFormat("OPEN REJECTED before sending: volume %.4f is not tradable - %s",
                  volume,reason);
      return false;
     }

   const double bid = SymbolInfoDouble(m_symbol,SYMBOL_BID);
   const double ask = SymbolInfoDouble(m_symbol,SYMBOL_ASK);
   if(bid<=0.0 || ask<=0.0)
     {
      Print("OPEN REJECTED before sending: no two-sided quote - ",reason);
      return false;
     }

//--- A long fills at the ask and is closed at the bid; the levels are anchored
//--- on the side the position will actually be measured against.
   double sl = 0.0, tp = 0.0;
   if(side==POSITION_TYPE_BUY)
     {
      if(stopDistance>0.0)
         sl = NormalizeDouble(bid-stopDistance,m_digits);
      if(takeDistance>0.0)
         tp = NormalizeDouble(bid+takeDistance,m_digits);
     }
   else
     {
      if(stopDistance>0.0)
         sl = NormalizeDouble(ask+stopDistance,m_digits);
      if(takeDistance>0.0)
         tp = NormalizeDouble(ask-takeDistance,m_digits);
     }

   bool ok = false;
   if(side==POSITION_TYPE_BUY)
      ok = m_trade.Buy(volume,m_symbol,0.0,sl,tp,m_tag);
   else
      ok = m_trade.Sell(volume,m_symbol,0.0,sl,tp,m_tag);

   if(!ok)
     {
      PrintFormat("OPEN %s %.2f lots REJECTED: retcode %d (%s) | sl %s tp %s - %s",
                  (side==POSITION_TYPE_BUY?"BUY":"SELL"),volume,
                  m_trade.ResultRetcode(),m_trade.ResultRetcodeDescription(),
                  DoubleToString(sl,m_digits),DoubleToString(tp,m_digits),reason);
      return false;
     }

   PrintFormat("OPEN %s %.2f lots @ %s | sl %s tp %s - %s",
               (side==POSITION_TYPE_BUY?"BUY":"SELL"),volume,
               DoubleToString(m_trade.ResultPrice(),m_digits),
               DoubleToString(sl,m_digits),DoubleToString(tp,m_digits),reason);
   return true;
  }

//+------------------------------------------------------------------+
//| Close every ticket of ours on this symbol, by ticket, which is    |
//| correct under both HEDGING and NETTING.                           |
//+------------------------------------------------------------------+
bool CGoldTrader::CloseAll(const string reason)
  {
   bool allClosed = true;
   for(int i=PositionsTotal()-1; i>=0; i--)
     {
      const string sym = PositionGetSymbol(i);
      if(sym!=m_symbol)
         continue;
      if(PositionGetInteger(POSITION_MAGIC)!=m_magic)
         continue;

      const ulong ticket = (ulong)PositionGetInteger(POSITION_TICKET);
      if(!m_trade.PositionClose(ticket))
        {
         PrintFormat("CLOSE #%I64u REJECTED: retcode %d (%s) - %s",
                     ticket,m_trade.ResultRetcode(),
                     m_trade.ResultRetcodeDescription(),reason);
         allClosed = false;
         continue;
        }
      PrintFormat("CLOSE #%I64u @ %s - %s",
                  ticket,DoubleToString(m_trade.ResultPrice(),m_digits),reason);
     }
   return allClosed;
  }

//+------------------------------------------------------------------+
//| Put `level` on every ticket of ours as a broker-side stop loss.   |
//|                                                                   |
//| Pass 0.0 to clear the stop (no protective exit configured).        |
//|                                                                   |
//| Two terminal-side facts this has to respect, neither of which the  |
//| Python model has to:                                               |
//|                                                                    |
//| SYMBOL_TRADE_STOPS_LEVEL - the broker refuses a stop closer to the |
//| market than this (20 points, i.e. $0.20, on the measured account). |
//| A level inside that band is CLAMPED to the edge of it and logged.  |
//| Clamping makes the stop slightly looser than asked; refusing to    |
//| place it at all would leave the position naked, which is worse.    |
//|                                                                    |
//| SYMBOL_TRADE_FREEZE_LEVEL - inside this band the position cannot   |
//| be modified at all. Reported, not fought; the bar-close backstop   |
//| in the expert covers the position until the band is left.          |
//+------------------------------------------------------------------+
bool CGoldTrader::ApplyStop(const double level)
  {
   const double stopsDistance  = (double)SymbolInfoInteger(m_symbol,SYMBOL_TRADE_STOPS_LEVEL) * m_point;
   const double freezeDistance = (double)SymbolInfoInteger(m_symbol,SYMBOL_TRADE_FREEZE_LEVEL) * m_point;
   const double bid = SymbolInfoDouble(m_symbol,SYMBOL_BID);
   const double ask = SymbolInfoDouble(m_symbol,SYMBOL_ASK);

   bool allApplied = true;
   for(int i=PositionsTotal()-1; i>=0; i--)
     {
      const string sym = PositionGetSymbol(i);
      if(sym!=m_symbol)
         continue;
      if(PositionGetInteger(POSITION_MAGIC)!=m_magic)
         continue;

      const ulong ticket = (ulong)PositionGetInteger(POSITION_TICKET);
      const ENUM_POSITION_TYPE type = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
      const double currentSl = PositionGetDouble(POSITION_SL);
      const double takeProfit = PositionGetDouble(POSITION_TP);

      double wanted = level;
      if(wanted>0.0)
        {
         //--- A long is closed at the bid, a short at the ask; the broker
         //--- measures the stops distance from that side of the book.
         if(type==POSITION_TYPE_BUY)
           {
            const double ceiling = bid - stopsDistance;
            if(wanted > ceiling)
              {
               PrintFormat("stop %s is inside the %s stops band; clamped to %s",
                           DoubleToString(wanted,m_digits),
                           DoubleToString(stopsDistance,m_digits),
                           DoubleToString(ceiling,m_digits));
               wanted = ceiling;
              }
            if(MathAbs(bid-wanted) < freezeDistance)
              {
               PrintFormat("stop %s is inside the freeze band; leaving the existing SL "
                           "and relying on the bar-close backstop this bar",
                           DoubleToString(wanted,m_digits));
               continue;
              }
           }
         else
           {
            const double floor_ = ask + stopsDistance;
            if(wanted < floor_)
              {
               PrintFormat("stop %s is inside the %s stops band; clamped to %s",
                           DoubleToString(wanted,m_digits),
                           DoubleToString(stopsDistance,m_digits),
                           DoubleToString(floor_,m_digits));
               wanted = floor_;
              }
            if(MathAbs(wanted-ask) < freezeDistance)
              {
               PrintFormat("stop %s is inside the freeze band; leaving the existing SL "
                           "and relying on the bar-close backstop this bar",
                           DoubleToString(wanted,m_digits));
               continue;
              }
           }
         wanted = NormalizeDouble(wanted,m_digits);
        }

      //--- Nothing to do. Saves a modify request per bar per ticket, which
      //--- some brokers count against a request-rate limit.
      if(MathAbs(currentSl-wanted) < m_point/2.0)
         continue;

      if(!m_trade.PositionModify(ticket,wanted,takeProfit))
        {
         PrintFormat("SL modify #%I64u to %s REJECTED: retcode %d (%s)",
                     ticket,DoubleToString(wanted,m_digits),m_trade.ResultRetcode(),
                     m_trade.ResultRetcodeDescription());
         allApplied = false;
         continue;
        }
      PrintFormat("SL #%I64u -> %s",ticket,DoubleToString(wanted,m_digits));
     }
   return allApplied;
  }

//+------------------------------------------------------------------+
//| Rebuild an armed trail purely from chart history.                 |
//|                                                                   |
//| The Python persists the trail peak through StateStore, because it  |
//| owns its process lifetime. An expert does not: it is reloaded on   |
//| recompile, on a terminal restart, on a timeframe change. So rather |
//| than persist, the peak is REPLAYED - advance_trail applied to      |
//| every closed bar from the one the position opened in through the   |
//| last closed one. That is the identical arithmetic and needs no     |
//| state file, so a reload cannot silently drop a trail that was      |
//| already armed mid-trade.                                          |
//|                                                                    |
//| Uses the bar the position opened IN (iBarShift of POSITION_TIME):  |
//| a market order sent after bar N closes fills at the open of bar    |
//| N+1, and bar N+1 is the first bar the Python sees the position on. |
//+------------------------------------------------------------------+
void RebuildTrail(TrailState &st,const string symbol,const ENUM_TIMEFRAMES tf,
                  const GoldPosition &pos)
  {
   if(!pos.exists)
     {
      TrailClear(st);
      return;
     }

   TrailStart(st,pos.entry,pos.side);

   const int firstBar = iBarShift(symbol,tf,pos.openTime,false);
   if(firstBar<1)
     {
      //--- Opened during the bar still forming: nothing closed to replay yet.
      return;
     }

   for(int shift=firstBar; shift>=1; shift--)
      TrailAdvance(st,iHigh(symbol,tf,shift),iLow(symbol,tf,shift));

   PrintFormat("trail rebuilt from %d closed bar(s) since entry: side %s, entry %.2f, peak %.2f",
               firstBar,(pos.side==POSITION_TYPE_BUY?"BUY":"SELL"),st.entry,st.peak);
  }

//+------------------------------------------------------------------+
//| Shared preflight. Returns false with a stated reason.             |
//+------------------------------------------------------------------+
bool GoldPreflight(const long magic,const double stopPct,
                   const double activationPct,const double trailPct)
  {
   if(magic==ALGOGOLD_PYTHON_MAGIC)
     {
      PrintFormat("FATAL: magic %d is claimed by algo/execution/mt5_broker.py. "
                  "Sharing it would let the Python reconciler adopt this expert positions. "
                  "Pick a different number.",ALGOGOLD_PYTHON_MAGIC);
      return false;
     }
   if(stopPct<0.0)
     {
      Print("FATAL: StopLossPct cannot be negative");
      return false;
     }
   if(activationPct<0.0)
     {
      Print("FATAL: TrailActivationPct cannot be negative");
      return false;
     }
   if(trailPct<0.0)
     {
      Print("FATAL: TrailPct cannot be negative");
      return false;
     }
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
      Print("WARNING: algo trading is disabled in the terminal - no order will be sent");
   if(!MQLInfoInteger(MQL_TRADE_ALLOWED))
      Print("WARNING: algo trading is disabled for this expert - no order will be sent");
   if(!AccountInfoInteger(ACCOUNT_TRADE_EXPERT))
      Print("WARNING: the account does not permit expert trading");
   return true;
  }

//+------------------------------------------------------------------+
//| The timeframe warning D-124 earned.                               |
//|                                                                   |
//| Measured on a common 2.11-year XAUUSD window, both strategies got  |
//| dramatically better as the bar interval slowed, for one reason:    |
//| trade count roughly halves per step while the $0.29 round-trip     |
//| spread is charged per round trip. MACD was -$230,052 on M15 and    |
//| +$190,186 on H1. That is not a tuning preference, it is the        |
//| dominant term, so running below M15 is worth a line in the log.    |
//+------------------------------------------------------------------+
void GoldTimeframeNote(const ENUM_TIMEFRAMES tf)
  {
   if(PeriodSeconds(tf) < PeriodSeconds(PERIOD_M15))
      Print("WARNING: below M15. Measured on real XAUUSD bars and real costs, both "
            "strategies were heavily net-negative at fast intervals - spread paid per "
            "round trip dominates gross P&L. M30/H1 is where the measurements were positive.");
  }

#endif // ALGOGOLD_TRADER_MQH
