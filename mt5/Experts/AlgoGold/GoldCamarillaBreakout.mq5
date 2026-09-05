//+------------------------------------------------------------------+
//| GoldCamarillaBreakout.mq5                                        |
//|                                                                  |
//| Forked from GoldTrendlineBreakout.mq5. That expert is a faithful |
//| port of algo/strategy/trendline_breakout.py and REMAINS ONE -    |
//| it was left untouched precisely so the Python correspondence it  |
//| documents stays true. This file is where the correspondence is   |
//| deliberately broken.                                             |
//|                                                                  |
//| ====================================================================
//| WHAT THIS IS, AND HOW IT DIFFERS FROM ITS PARENT
//| ====================================================================
//| The Donchian break is still the trigger, but it is no longer the |
//| strategy. It is now one of three conditions, and the way a trade |
//| ends has been replaced outright:                                  |
//|                                                                  |
//|   ENTRY needs all three                                           |
//|     1. a fresh `lookback`-bar high (long) or low (short)         |
//|     2. price beyond the Camarilla H1 (long) or L1 (short),       |
//|        built from the previous completed daily bar               |
//|     3. AMA(9, fast 4, slow 30) above DEMA(14) for a long,        |
//|        below it for a short                                       |
//|                                                                  |
//|   EXIT is two mechanisms with different jobs                      |
//|     - candle colour, AT THE BAR CLOSE: the first bar closing     |
//|       against the position closes it, and a later bar closing    |
//|       back in favour reopens the same side                        |
//|     - a STRUCTURAL STOP, INTRABAR ON THE CROSS: for a long, the  |
//|       low of the candle two before the last closed one; for a    |
//|       short, that candle's high                                   |
//|                                                                  |
//| The two exits are not redundant. The colour rule cannot look     |
//| until a bar has finished, so on its own it bounds a loss in TIME |
//| and not in price - one violent candle takes whatever it takes    |
//| before the rule gets a vote. The structural level sits on the    |
//| book and fires while that candle is still forming. That division |
//| is the whole point of the pair.                                   |
//|                                                                  |
//| ====================================================================
//| NOTHING HERE IS MEASURED, AND THAT IS THE FIRST THING TO KNOW
//| ====================================================================
//| scripts/measure_camarilla_filter.py scored Camarilla entry       |
//| filters against random-block controls and found exactly one arm  |
//| that beat its control: refusing entries INSIDE L3..H3. This      |
//| expert ships a different rule - DIRECTIONAL on L1/H1, plus a     |
//| trend gate, plus a candle exit, plus a structural stop - and no  |
//| measurement in this repository has scored that combination, or   |
//| any part of it beyond the first. The parent expert at least had  |
//| a Python backtest behind its signal. This one has arithmetic and |
//| reasoning, which is not the same thing.                           |
//|                                                                  |
//| Set InpCamMode=CAM_MODE_INNER and InpCamPair=CAM_PAIR_3 to run   |
//| the arm that was actually measured.                               |
//|                                                                  |
//| ====================================================================
//| IT MUST NOT SHARE A MAGIC WITH ITS PARENT
//| ====================================================================
//| Two experts on one symbol under one magic adopt each other's     |
//| positions: each Snapshot() would net the other's tickets into    |
//| its own view and manage them under rules they were never opened  |
//| under. The magic here is 20260905 against the parent's 20260902  |
//| and the Python adapter's 20260828, and the dashboard uses its    |
//| own object prefix so both panels can share a chart.               |
//|                                                                  |
//| ====================================================================
//| WHY A DONCHIAN CHANNEL IS "TREND LINE BREAKOUT"
//| ====================================================================
//| A hand-drawn trend line is not implementable without a human      |
//| judgement call about which two swing points to connect, and this  |
//| project does not invent indicators without a stated, checkable    |
//| definition. The Donchian channel - the highest high and lowest    |
//| low of the last `lookback` bars - IS that definition, formalised: |
//| it is what "price broke its recent trend line" means once you     |
//| have to write it down. It is also the literal core of the Turtle  |
//| system, so the shape has a long, checkable precedent.             |
//|                                                                  |
//| ====================================================================
//| THE CHANNEL EXCLUDES THE BAR BEING TESTED
//| ====================================================================
//| The channel is built from the `lookback` bars STRICTLY BEFORE the |
//| one being tested - here, chart shifts 2..lookback+1, never shift  |
//| 1. Including the current bar own high would compare today price   |
//| against a range that already contains it, which cannot be broken  |
//| by definition.                                                    |
//|                                                                  |
//| ====================================================================
//| NO INDICATOR STATE TO SEED
//| ====================================================================
//| An EMA is path-dependent, so the MACD expert has to replay        |
//| history on every init. A rolling max/min over the last `lookback` |
//| bars depends on ONLY those bars: a freshly loaded expert facing   |
//| the same recent history makes the same decision as one that has   |
//| been running for a month. The only path-dependent piece here is   |
//| the optional trailing stop, and that is replayed from the         |
//| position own open time rather than persisted (see RebuildTrail).  |
//|                                                                  |
//| ====================================================================
//| IT DOES NOT REVERSE IN ONE STEP
//| ====================================================================
//| A long taken on an upside breakout is closed the moment price     |
//| makes a fresh `lookback`-bar low - the same event that would open |
//| a short if the expert were flat. It closes on that bar and waits  |
//| for the next break to enter, mirroring the Python, which emits at |
//| most one signal per bar.                                          |
//+------------------------------------------------------------------+
#property copyright "algo trading - GOLDM/XAUUSD engine"
#property link      ""
#property version   "1.00"
#property description "Camarilla-gated Donchian breakout with an AMA/DEMA trend filter, "
#property description "a candle-colour exit and a structural stop. Forked from "
#property description "GoldTrendlineBreakout.mq5; no Python counterpart."
#property strict

#include <AlgoGold\ProtectiveExits.mqh>
#include <AlgoGold\Trader.mqh>
#include <AlgoGold\Dashboard.mqh>

//+------------------------------------------------------------------+
//| Inputs. Every default is the Python default for the same name.   |
//+------------------------------------------------------------------+
input group "--- Signal (algo/strategy/trendline_breakout.py) ---"
input int    InpLookback          = 20;      // Channel length, bars. Minimum 2

//+------------------------------------------------------------------+
//| THE STOP IS SIZED IN ATR, AND THAT IS THE POINT.                  |
//|                                                                  |
//| Every other unit this expert offers - percent of price, points,   |
//| money - is fixed. Volatility is not, and a stop that does not     |
//| move with it is either inside the noise or absurdly wide, never   |
//| the right distance for long.                                      |
//|                                                                  |
//| ====================================================================
//| MEASURED: WHY THE 0.5% STOP KEPT GETTING HIT
//| ====================================================================
//| A 0.5% stop, resolved against ATR(H1,14) on 2026-09-05:           |
//|                                                                  |
//|   symbol       0.5% is    = x ATR(H1)   = % of an average day    |
//|   FixedVol100   27.024        0.29 x            7.0%             |
//|   BTCUSD       398.10         1.13 x           13.9%             |
//|   XAUUSD        22.16         0.82 x           20.7%             |
//|                                                                  |
//| On FixedVol100 that stop was 29% of ONE HOUR'S typical range. It  |
//| was not being hunted; it was inside the instrument's own noise    |
//| and ordinary movement took it. FixedVol100 averages a 7.14%       |
//| DAILY range, so half a percent is a fourteenth of a normal day.   |
//|                                                                  |
//| Note also that the same 0.5% means three different things across  |
//| three symbols - 0.29x, 1.13x and 0.82x ATR. A percentage of price |
//| is not a volatility unit, which is the same flaw the Camarilla    |
//| buffer's own comment describes further down.                      |
//|                                                                  |
//| ====================================================================
//| WIDER STOP AND SMALLER SIZE, TOGETHER
//| ====================================================================
//| Widening the stop alone raises the loss per trade. The pair that  |
//| works is a wider stop AND a smaller lot, which leaves the MONEY   |
//| risked unchanged while moving the level out of the noise. The     |
//| init banner prints the resolved stop, what it costs at the        |
//| configured lot, and what share of equity that is, because that    |
//| last number is the one worth checking and it is not visible from  |
//| any single input.                                                 |
//|                                                                  |
//| PRECEDENCE: ATR > money > points > percent. The first non-zero    |
//| one wins, so the others can be left set as fallbacks.             |
//|                                                                  |
//| The ATR distance is converted back into a percentage of the entry |
//| for the same reason the point and money stops are: the bar-close  |
//| management (ProtectiveExitsCheck / ProtectiveStopPrice) is        |
//| percentage-based and would otherwise overwrite the level on the   |
//| next bar. The percentage is recomputed from the CURRENT ATR every |
//| time it is asked for, so the stop breathes with volatility for as |
//| long as the position is held.                                     |
//+------------------------------------------------------------------+
input group "--- Protective exits (percent of price, NOT points) ---"
input double InpStopLossAtrMult   = 0.0;     // Stop = this x ATR. HIGHEST precedence. 0 = off
input int    InpAtrPeriod         = 14;      // ATR period
//--- The ATR timeframe is pinned to H1 rather than following the chart, because
//--- the noise a stop has to clear is the instrument's, not the chart's. On M5
//--- a 1.5 x ATR(M5) stop is right back inside the same noise that was taking
//--- the 0.5% one. Set it to PERIOD_CURRENT if you want the stop to scale with
//--- the bars being traded instead.
input ENUM_TIMEFRAMES InpAtrTimeframe = PERIOD_H1; // Timeframe the ATR is measured on
input double InpStopLossPct       = 0.0;     // Flat stop, % of entry. 0 disables - fallback only
input double InpTrailActivationPct= 2.0;     // Profit % at which the trail arms
input double InpTrailPct          = 0.0;     // Trail distance, % behind peak. 0 disables
input double InpTakeProfitPct     = 0.0;     // Target, % of entry. 0 disables (no Python counterpart)
input bool   InpBracketAtEntry    = true;    // Attach SL/TP to the ENTRY order, not the next bar
//--- Point-based overrides. Non-zero WINS over the percentage above.
//---
//--- A percentage is the Python's unit and is symbol-independent, but it is a
//--- blunt instrument at these prices: 1% of FixedVol100 at 4,731 is 47 price
//--- units, which on M1 is an enormous target. Points let the distance be
//--- stated directly.
//---
//--- MIND THE SCALE. A point is SYMBOL_POINT, not a pip:
//---   XAUUSD      2 digits, point 0.01  -> 100 points = 1.00 price
//---   FixedVol100 3 digits, point 0.001 -> 100 points = 0.10 price
//---   BTCUSD      2 digits, point 0.01  -> 100 points = 1.00 price
//--- so the same number means a tenth as much on FixedVol100. The broker
//--- minimum there is 7,123 POINTS (7.123 price); anything under that is
//--- rejected, and init prints the resolved distance so it can be checked.
//---
//--- The stop is converted back into an equivalent percentage against the
//--- position's entry, because the bar-close management (ProtectiveExitsCheck /
//--- ProtectiveStopPrice) is percentage-based and would otherwise overwrite a
//--- point-based stop on the very next bar with a different level.
input int    InpStopLossPoints    = 0;       // Stop in POINTS. >0 overrides InpStopLossPct. 0 = use money below
input int    InpTakeProfitPoints  = 0;       // Target in POINTS. >0 overrides InpTakeProfitPct. 0 = use money below
//--- Target as a MONEY amount, in account currency. Highest precedence of all.
//---
//--- Precedence: money > points > percent. The first non-zero one wins, so the
//--- others can be left set as fallbacks without ambiguity.
//---
//--- The conversion depends on VOLUME, which the other two do not:
//---     money per 1.0 price move = (tick_value / tick_size) * lots
//---     distance                 = InpTakeProfitTargetMoney / that
//--- so changing InpLots silently changes the price distance this produces.
//--- That is inherent to asking for a money target, not a flaw - but it means
//--- the resolved distance has to be read from the init banner rather than
//--- assumed, and re-read after any lot change.
//---
//--- Worked example on FixedVol100 at 1.2 lots: tick_value 0.001 and tick_size
//--- 0.001 give 1.0 per price per lot, so 1.2 per price. A $20 target is a
//--- 16.67 price move. On XAUUSD at 0.05 lots it is (1.0/0.01)*0.05 = 5.0 per
//--- price, so the same $20 is a 4.00 move. Same input, very different trade.
input double InpStopLossMoney     = 0.0;     // Stop as LOSS in account currency. 0 = fall back to points/percent
input double InpTakeProfitMoney   = 0.0;     // Target as PROFIT in account currency. 0 = fall back to points/percent
//--- MONEY-BASED TRAILING STOP.
//---
//--- Arm the trail once open profit reaches InpTrailActivationMoney, then keep
//--- the stop InpTrailMoney behind the best price seen. Both are in account
//--- currency and both override the percentage equivalents above.
//---
//--- These convert to percentages against different references, which is not
//--- arbitrary: ProtectiveExits measures activation from the ENTRY (how far
//--- the position has travelled) and the give-back from the PEAK (how much of
//--- the best price is surrendered). Converting both against entry would make
//--- the give-back drift as the peak advanced.
//---
//--- The cost-to-cost clamp in TrailLevel still applies: an armed trail can
//--- never sit worse than entry, so its floor is a scratch, not a loss.
//---
//--- NO PYTHON COUNTERPART. trailing_profit_stop.py is percentage-based; a
//--- money trail is a live-only divergence the backtest has never scored.
input double InpTrailActivationMoney = 5.0;  // Arm the trail at this PROFIT in account currency. 0 = use percent
input double InpTrailMoney           = 1.0;  // Trail this many account-currency units behind the peak. 0 = use percent

//+------------------------------------------------------------------+
//| SALVAGE EXIT - take the first profit on a trade that started badly|
//|                                                                   |
//| If the position goes adverse within the first InpSalvageWindowMin |
//| minutes, it is MARKED. A marked position is then closed at market |
//| the moment its floating profit reaches InpSalvageExitProfit,      |
//| instead of waiting for the target.                                |
//|                                                                   |
//| ====================================================================
//| "IN LOSS" IS MEASURED AGAINST A THRESHOLD, NOT AGAINST ENTRY
//| ====================================================================
//| Every position is in loss the instant it opens: a buy fills at the|
//| ask and is marked against the bid, so it starts down by the spread|
//| before the market has done anything. Marking on "price went below |
//| entry" would therefore mark EVERY trade and the rule would just   |
//| mean "always exit at the first profit".                           |
//|                                                                   |
//| So the trigger is a real adverse excursion, measured in money and |
//| required to exceed InpSalvageLossMoney. Left at 0 it defaults to  |
//| one spread, which is the smallest move that is not just the cost  |
//| of having opened.                                                 |
//|                                                                   |
//| ====================================================================
//| MARKED IS REPLAYED, NOT REMEMBERED
//| ====================================================================
//| The adverse excursion is recomputed from the bars since entry, the|
//| same way RebuildTrail works, so a recompile or restart cannot lose|
//| the mark. It is cached per ticket so the replay runs once a bar    |
//| rather than once a tick.                                          |
//|                                                                   |
//| The PROFIT test runs every tick, because "close at the first      |
//| profit" is worth little if it only looks once a minute.           |
//|                                                                   |
//| NO PYTHON COUNTERPART - trendline_breakout.py has no such rule, so|
//| enabling this makes live behaviour something no backtest scored.  |
//+------------------------------------------------------------------+
input group "--- Salvage exit (no Python counterpart) ---"
input bool   InpSalvageEnabled    = true;    // Close a recovered trade at the first profit
input int    InpSalvageWindowMin  = 5;       // Minutes after entry in which the adverse move must happen
input double InpSalvageLossMoney  = 0.0;     // Must have been down at least this much. 0 = one spread
input double InpSalvageExitProfit = 0.0;     // Close once profit reaches this. 0 = any profit above zero

//+------------------------------------------------------------------+
//| SCALE-IN - add to a losing position, same side, up to a limit.    |
//|                                                                   |
//| When the netted position is InpScaleInLossMoney down, open another|
//| position on the SAME side. When it is twice that down, open a     |
//| third, and so on to InpScaleInMaxTrades in total.                 |
//|                                                                   |
//| ====================================================================
//| WHAT THIS IS, STATED PLAINLY
//| ====================================================================
//| This is averaging into a loser. It is the same family as the grid |
//| and martingale EAs D-141 disqualified - with one important        |
//| difference: the size does NOT escalate. Every add is InpLots, so  |
//| total exposure is bounded at InpScaleInMaxTrades * InpLots and the|
//| worst case is a known number, not an open-ended one.              |
//|                                                                   |
//| That bound came from the STOP, and the stop is now off by default |
//| (InpStopLossPct = 0). At ten rungs and a 1.25 multiplier the       |
//| ladder totals 33.25x the base lot - 36.58 lots on FixedVol100 at  |
//| 1.1 base, which is $36.58 for every 1.0 of adverse price. One      |
//| previous-day range of 828.460 against that is about $30,300 on a  |
//| ~$1,050 account, roughly 29x the account. On BTCUSD the same ten   |
//| rungs are 2.66 lots and a 3633.23 range is about $9,665, near 9x.  |
//|                                                                   |
//| Setting InpScaleInLotMult to 1.00 makes the ladder 10x the base    |
//| rather than 33.25x and cuts those to about $9,100 and $2,900.      |
//| Init prints the resolved ladder and the exposure, which is the     |
//| number to decide about rather than the multiplier.                 |
//|                                                                   |
//| ====================================================================
//| THE ADDS MOVE THE SHARED STOP
//| ====================================================================
//| Snapshot() nets every ticket into one volume-weighted position and|
//| ApplyStop puts ONE level on all of them. So each add pulls the    |
//| average entry toward price and the shared stop moves with it -    |
//| the first ticket's stop WIDENS as you add. That is coherent for an|
//| averaging strategy (you are betting on the mean), but it means the|
//| risk is not simply "five independent trades": they live and die   |
//| together on one level.                                            |
//|                                                                   |
//| ====================================================================
//| THE LADDER IS CUMULATIVE, WHICH IS WHAT STOPS IT SPAMMING
//| ====================================================================
//| Add number k fires at k * InpScaleInLossMoney of total floating   |
//| loss. So -5 opens the 2nd, -10 the 3rd, -15 the 4th, -20 the 5th. |
//| Because the count rises with each add, the next threshold is      |
//| immediately further away and one tick cannot trigger two adds.    |
//|                                                                   |
//| NO PYTHON COUNTERPART, and nothing here has ever backtested it.   |
//+------------------------------------------------------------------+
input group "--- Scale-in on loss (no Python counterpart) ---"
input bool   InpScaleInEnabled    = false;   // Add to a losing position, same side. OFF by default
input double InpScaleInLossMoney  = 5.0;     // Each add fires at another this-much of loss
input int    InpScaleInMaxTrades  = 10;      // Total positions including the first
//--- Size multiplier per add. 1.0 = flat averaging; anything above 1.0 is a
//--- MARTINGALE, and the distinction is not cosmetic.
//---
//--- With 1.25 and five positions the ladder is base x 1, 1.25, 1.5625, 1.9531,
//--- 2.4414 - a total of 8.21x the base lot rather than 5x. On FixedVol100 at
//--- 1.1 base that rounds to 1.1 / 1.4 / 1.7 / 2.1 / 2.7 = 9.0 lots, and a full
//--- stop-out costs about $83 instead of $51. Roughly 8% of a $1,050 account
//--- against 4.8%.
//---
//--- It is still BOUNDED - the ladder ends at InpScaleInMaxTrades and every
//--- position keeps its own bracket - so this is a martingale with a floor, not
//--- the open-ended kind that ends accounts. The floor is the max-trades cap;
//--- raising that is what would make it dangerous.
//---
//--- Each size is normalised to the symbol's volume step, so the actual ladder
//--- is printed on init rather than assumed from the multiplier.
input double InpScaleInLotMult    = 1.25;    // Multiply the lot by this on each add. 1.0 = flat
//--- WHETHER THE CAMARILLA FILTER ALSO GATES THE ADDS. Default false, and the
//--- default matters more than it looks.
//---
//--- A position only goes into loss by price moving AGAINST it, which for a
//--- breakout entry means price coming back toward the range it just left. So
//--- with a directional region selected, the add is asked for at exactly the
//--- moment the filter refuses that side - and gating adds would silently
//--- disable averaging altogether rather than merely restrict it.
//---
//--- Set it true only with an interior region (INNER_L3_H3 / INNER_L4_H4),
//--- where a pullback can still sit outside the refused zone and the gate
//--- means something.
input bool   InpCamFilterScaleIn  = false;   // true = the Camarilla filter also blocks adds
//--- BASKET TAKE PROFIT - close everything once the combined position is up.
//---
//--- This is the exit that makes scale-in coherent: the adds pull the average
//--- entry toward price, so the basket needs a smaller recovery to reach profit
//--- than the first ticket alone would. Without it the adds have no plan.
//---
//--- Measured on the COMBINED floating P&L of every ticket under our magic, not
//--- per trade. With five positions open, $2 means the basket as a whole is $2
//--- up - roughly $0.40 a trade - not $2 each. Set it to 10.00 if you want the
//--- latter.
//---
//--- ORDER MATTERS AGAINST SALVAGE. A marked position closes at the first
//--- profit above zero, which is a LOWER bar than this, so on a marked basket
//--- salvage fires first and this never gets the chance. That is deliberate -
//--- the tighter exit wins - but it means the two rules together behave like
//--- "exit at breakeven if it started badly, otherwise at InpBasketTakeMoney".
input double InpBasketTakeMoney   = 2.0;     // Close ALL positions at this combined profit. 0 = off

//+------------------------------------------------------------------+
//| CAMARILLA LEVEL FILTER - keep entries away from the pivots.       |
//|                                                                  |
//| Ported from "Camarilla Channel.mq5" (MetaQuotes, shipped under    |
//| MQL5\Indicators\Free Indicators). The arithmetic is the           |
//| indicator's, verbatim, off the previous COMPLETED daily bar:      |
//|                                                                  |
//|   range = prevHigh - prevLow                                     |
//|   H5 = (prevHigh/prevLow)*prevClose   L5 = prevClose-(H5-prevClose)
//|   H4 = prevClose + range*1.1/2        L4 = prevClose - range*1.1/2
//|   H3 = prevClose + range*1.1/4        L3 = prevClose - range*1.1/4
//|   H2 = prevClose + range*1.1/6        L2 = prevClose - range*1.1/6
//|   H1 = prevClose + range*1.1/12       L1 = prevClose - range*1.1/12
//|                                                                  |
//| ====================================================================
//| WHY THE FORMULAS ARE PORTED RATHER THAN CALLED WITH iCustom
//| ====================================================================
//| iCustom would bind this expert to a file at a fixed path inside   |
//| one terminal's Indicators folder. That indicator is not in this   |
//| repository, so a clean machine - and a Strategy Tester agent -    |
//| would fail to load it, and an iCustom handle that fails to build  |
//| leaves the expert running with NO filter rather than stopping.    |
//| The levels depend on one completed daily bar and nothing else, so |
//| recomputing them here is not an approximation of the indicator:   |
//| it is the same arithmetic on the same input.                      |
//|                                                                  |
//| ====================================================================
//| IT ONLY EVER SUPPRESSES BUYING
//| ====================================================================
//| The filter is consulted for a first entry and for a scale-in add. |
//| It is never consulted on the way out: protective stops, the       |
//| trail, salvage, the basket exit and the breakout flatten all run  |
//| untouched. Refusing to leave a position because price is near a   |
//| pivot is how a small loss becomes a large one.                    |
//|                                                                  |
//| NO PYTHON COUNTERPART - trendline_breakout.py has no such filter, |
//| so enabling this makes live behaviour something no backtest       |
//| scored.                                                           |
//+------------------------------------------------------------------+
//+------------------------------------------------------------------+
//| WHICH REGION IS REFUSED - and what measurement chose the default. |
//|                                                                  |
//| Camarilla's own reading of its levels: L3..H3 is the balance zone |
//| the day rotates inside, H4 and L4 are the breakout triggers, and  |
//| past those is trend territory. Three shapes follow from that, and |
//| `scripts/measure_camarilla_filter.py` ranked all of them against  |
//| the real Donchian entries rather than against the folklore.       |
//|                                                                  |
//| ====================================================================
//| A FILTER HAS TO BEAT BLOCKING THE SAME NUMBER OF TRADES AT RANDOM
//| ====================================================================
//| The first version of this filter shipped CAM_REGION_BANDS - a thin|
//| band around each of the ten lines - on nothing but the reasoning  |
//| that it was tidy. Measured, it blocks 7-9% of entries and moves   |
//| P&L by five figures, which is not evidence of an edge: it is      |
//| evidence that a handful of trades carry the sample. It has since  |
//| been REMOVED - the enum no longer offers it. So every arm         |
//| below was rerun twelve times blocking the SAME SHARE of entries   |
//| at random instants, and scored on how many of those twelve random |
//| runs matched or beat it. Twelve out of twelve means the filter is |
//| worse than chance; zero means it is doing something.              |
//|                                                                  |
//|   region        FV100 M15  FV100 H1  BTC M15  BTC H1  XAU M15  XAU H1
//|   INNER_L3_H3        0/12      0/12     3/12    0/12    11/12    7/12
//|   DIRECTIONAL_H4    12/12     12/12    12/12   12/12     7/12    0/12
//|                                                                  |
//| INNER_L3_H3 is the arm that beat its control on the symbols this  |
//| account trades: FixedVol100 H1 went -1365 to +3587 and BTCUSD H1  |
//| +3969 to +47026. It is kept, and selectable.                      |
//|                                                                  |
//| The shipped default is now DIRECTIONAL_H3 WITH A BUFFER, which is |
//| a rule none of those cells measured - the measured DIRECTIONAL_H4 |
//| had no buffer and required H4 rather than H3. Treat the table as  |
//| context for the neighbouring options, NOT as evidence for the     |
//| default. Nothing here has measured the buffered rule.             |
//|                                                                  |
//| ====================================================================
//| DIRECTIONAL_H4 IS THE TEXTBOOK RULE AND IT LOSES HERE
//| ====================================================================
//| Stated because it is the setting a reader will reach for. Taking  |
//| longs only above H4 and shorts only below L4 is what H4 and L4    |
//| are defined to mean, and on FixedVol100 and BTCUSD it was beaten  |
//| by random blocking twelve times out of twelve. The mechanism is   |
//| in the numbers: it refuses about 92% of entries, and refusing 92% |
//| AT RANDOM averaged +30955 on BTCUSD M15 where the rule itself     |
//| returned -5065. It is not failing to help, it is keeping the      |
//| losers - on these symbols a Donchian break beyond H4/L4 is        |
//| exhaustion rather than continuation, so the breaks worth taking   |
//| are the ones firing back INTO the range, which this discards.     |
//| It earned its 0/12 on XAUUSD H1 alone.                            |
//|                                                                  |
//| One window per cell, no walk-forward. Suggestive, not settled.    |
//+------------------------------------------------------------------+
//--- WHICH PAIR OF LEVELS the rule is built on, and WHAT it does with them,
//--- split into two inputs rather than one enum of every combination. The pair
//--- and the shape are independent choices and pretending otherwise is how an
//--- enum grows to sixteen members that mostly nobody selects.
//---
//--- The pairs sit at these distances either side of the previous close, in
//--- units of that day's range - so L1..H1 is the tightest and L4..H4 the
//--- widest:
//---
//---   pair 1   +/- 0.092 x range      pair 3   +/- 0.275 x range
//---   pair 2   +/- 0.183 x range      pair 4   +/- 0.550 x range
enum ENUM_CAM_PAIR
  {
   CAM_PAIR_1 = 0, // L1 / H1 - tightest, +/- 0.092 x range (default)
   CAM_PAIR_2,     // L2 / H2
   CAM_PAIR_3,     // L3 / H3 - the balance-zone edge
   CAM_PAIR_4      // L4 / H4 - the classical breakout trigger
  };

enum ENUM_CAM_MODE
  {
   CAM_MODE_DIRECTIONAL = 0, // BUY only above the upper level, SELL only below the lower (default)
   CAM_MODE_INNER            // No entry at all between the two levels
  };

input group "--- Camarilla level filter (no Python counterpart) ---"
input bool                InpCamEnabled   = true;                   // Refuse entries near a Camarilla level
input ENUM_CAM_PAIR       InpCamPair      = CAM_PAIR_1;             // WHICH pair of levels
input ENUM_CAM_MODE       InpCamMode      = CAM_MODE_DIRECTIONAL;   // WHAT the rule does with them
//--- HOW FAR PAST THE LEVEL THE BREAK HAS TO GO, as a percentage of the
//--- LEVEL'S OWN PRICE. A break has to clear H3 by this much before a BUY is
//--- allowed, and undercut L3 by this much before a SELL is.
//---
//--- MIND WHAT A PERCENTAGE OF PRICE MEANS HERE. It is not a volatility unit,
//--- so the same number buys a different amount of room on every symbol.
//--- Measured on the daily bars this account had on 2026-09-05, 0.5% resolves
//--- to:
//---
//---   FixedVol100  H3 5880.493 -> BUY above 5909.895, buffer  29.402
//---                = 4% of that day's 828.460 range
//---   BTCUSD       H3 80688.43 -> BUY above 81091.87, buffer 403.44
//---                = 11% of the 3633.23 range
//---   XAUUSD       H3 4508.93  -> BUY above 4531.48,  buffer  22.54
//---                = 17% of the 129.65 range
//---
//--- All three triggers land between H3 and H4, which is the intent: clear the
//--- balance zone's edge by a margin, without demanding a move to the next
//--- level. At 2% they did not - on XAUUSD 2% put the trigger just under H5,
//--- effectively "trade only beyond the day's extreme projection", which is a
//--- different instruction from the one the same number gives on FixedVol100.
//--- The spread between 4% and 17% of range is the residual of measuring in
//--- price rather than volatility, and it is why init prints the resolved
//--- trigger for the symbol in front of it rather than the percentage.
//--- Default 0: the rule is the literal one, "above H1" and "below L1". A
//--- buffer is available but is NOT free at this pair. H1 sits only 0.092 x
//--- range from the close, so on XAUUSD a 0.5% buffer is about 22 price units
//--- on top of an H1 that is 12 above the close - which lands the trigger at
//--- roughly H3 and quietly undoes the choice of pair 1. Check the resolved
//--- trigger the init banner prints before setting this above zero.
input double              InpCamBufferPct = 0.0;                    // Break must clear the level by this %% of price

//+------------------------------------------------------------------+
//| TREND CONFIRMATION - an adaptive MA against a double EMA.         |
//|                                                                  |
//| A second gate on top of the Camarilla one. A BUY needs DEMA above |
//| AMA, a SELL needs DEMA below it, so a breakout past H1 that the   |
//| two averages disagree with is refused. Both are read on the last  |
//| CLOSED bar for the same reason everything else here is: bar zero's|
//| values still move.                                                |
//|                                                                  |
//| ====================================================================
//| DEMA ABOVE AMA IS THE UPTREND. THIS WAS SHIPPED INVERTED ONCE.
//| ====================================================================
//| The first version of this gate required AMA above DEMA to buy, on |
//| the reasoning that Kaufman's AMA speeds up in a directional move  |
//| and should therefore lead. That reasoning is wrong, and it was    |
//| wrong in the direction that matters: the expert permitted longs   |
//| in downtrends and shorts in uptrends for as long as it stood.     |
//|                                                                  |
//| Measured on H1 closes, 8,246 bars of FixedVol100 and 20,000 each  |
//| of BTCUSD and XAUUSD, taking "uptrend" to mean simply that close  |
//| is above the close twenty bars earlier:                           |
//|                                                                  |
//|   symbol         UPTREND bars          DOWNTREND bars            |
//|                DEMA>AMA  AMA>DEMA    DEMA>AMA  AMA>DEMA          |
//|   FixedVol100     82.5%     17.5%       20.1%     79.9%          |
//|   BTCUSD          79.8%     20.2%       22.7%     77.3%          |
//|   XAUUSD          83.2%     16.8%       22.8%     77.2%          |
//|                                                                  |
//| Four times out of five, in every symbol and both directions.      |
//| Reproduce it with scripts/measure_ama_dema_orientation.py.        |
//|                                                                  |
//| The mechanism, stated so the error is not repeated: DEMA removes  |
//| most of a conventional EMA's lag, and at period 14 it sits very   |
//| close to price. AMA only reaches its fast constant when the       |
//| efficiency ratio approaches 1, which over a 9-bar window is rare; |
//| the rest of the time it decays toward the SLOW leg, which at 30   |
//| is far behind. So DEMA is the faster line here in almost every    |
//| regime, and the faster line is the one that sits above in a rise. |
//| "Adaptive" does not mean "fast".                                  |
//|                                                                  |
//| Note that forward-return tests were NOT what settled this, and    |
//| were nearly used to. Mean forward returns after each state differ |
//| by single-digit basis points and point in different directions on |
//| different symbols and horizons - noise. The claim being tested is |
//| about what the two lines MEAN concurrently, and the concurrent    |
//| measurement above is unambiguous.                                 |
//|                                                                  |
//| ====================================================================
//| IT FAILS CLOSED, UNLIKE THE CAMARILLA FILTER
//| ====================================================================
//| If the buffers cannot be read - handle still warming up after a   |
//| restart, not enough history - this refuses the entry rather than  |
//| letting it through. The two gates differ deliberately: the        |
//| Camarilla filter is a PROHIBITION ("not inside this zone") and an |
//| unreadable prohibition should not halt trading for a session,     |
//| while this is a REQUIREMENT ("DEMA above AMA") and a requirement  |
//| that cannot be evaluated has not been met. It only ever blocks    |
//| entries, so the failure mode is no new trades, never an unmanaged |
//| position - and it clears itself within a few bars.                |
//|                                                                  |
//| NO PYTHON COUNTERPART.                                            |
//+------------------------------------------------------------------+
input group "--- Trend confirmation: AMA vs DEMA (no Python counterpart) ---"
input bool   InpTrendEnabled    = true;    // Require DEMA above AMA to buy, below to sell
//--- MT5's own Adaptive Moving Average defaults are period 9, fast 2, slow 30.
//--- Only the fast leg differs here, at 4: a slower fast leg makes the adaptive
//--- step less jumpy in the efficient case, so the AMA crosses the DEMA a
//--- little later and a little less often. Everything else is left at the
//--- indicator's own default rather than picked.
input int    InpAmaPeriod       = 9;       // AMA period (MT5 default 9)
input int    InpAmaFast         = 4;       // AMA fast EMA period (MT5 default 2)
input int    InpAmaSlow         = 30;      // AMA slow EMA period (MT5 default 30)
input int    InpDemaPeriod      = 14;      // DEMA period (MT5 default 14)
//--- PERIOD_CURRENT means the chart's own timeframe, which is the right default
//--- for a trend filter: it should describe the bars being traded. Unlike the
//--- ATR stop above, which is pinned to H1 because the noise a STOP must clear
//--- belongs to the instrument rather than to the chart.
input ENUM_TIMEFRAMES     InpTrendTimeframe = PERIOD_CURRENT; // Timeframe both averages are measured on
input ENUM_APPLIED_PRICE  InpTrendPrice     = PRICE_CLOSE;    // Applied price for both

//+------------------------------------------------------------------+
//| CANDLE-COLOUR EXIT - leave on the first bar that closes against.  |
//|                                                                  |
//| A long is closed by the first bar whose close is below its open;  |
//| a short by the first bar that closes above its own open. A doji   |
//| (close exactly equal to open) is neither and does nothing.        |
//|                                                                  |
//| ====================================================================
//| THIS BOUNDS THE LOSS IN TIME, NOT IN PRICE
//| ====================================================================
//| With InpStopLossAtrMult, the point stop, the money stop and the    |
//| percent stop all at zero, this is the ONLY thing ending a losing  |
//| trade. It is not a stop and does not behave like one: it caps how |
//| LONG a position can be wrong - about one bar - and says nothing   |
//| about how FAR wrong it can go inside that bar. A single violent   |
//| bar, or a gap over a weekend, is unbounded. A stop level would    |
//| have been crossed intrabar; this rule cannot look until the bar   |
//| has closed.                                                       |
//|                                                                   |
//| ====================================================================
//| RE-ENTRY WAITS FOR THE CANDLE TO TURN BACK, NOT MERELY FOR A BAR
//| ====================================================================
//| Re-entering on the very next bar regardless of its colour would   |
//| put the position straight back into the move that just closed it: |
//| in a three-bar pullback that is three exits and three re-entries, |
//| each paying the spread twice, all of them wrong. So the re-entry  |
//| waits for a bar that closes back IN FAVOUR of the side that was   |
//| exited, and both gates - Camarilla and the trend averages - must  |
//| still permit that side when it comes.                             |
//|                                                                   |
//| It deliberately does NOT need a fresh Donchian break. The break   |
//| that started the trade already happened; the candle exit was a    |
//| pause in that move rather than a verdict on it, and requiring the |
//| channel to be broken a second time would mean almost never        |
//| getting back in. Set InpCandleReentry false to require one.       |
//|                                                                   |
//| The allowance expires after InpReentryMaxBars so a trade closed   |
//| this morning is not reopened this evening on an unrelated green   |
//| bar. Any other exit - the trail, or the opposite breakout -       |
//| clears it outright: those are verdicts, and re-entering against   |
//| one would be arguing with the signal that produced it.            |
//|                                                                   |
//| EXPECT SHORT HOLDS. Roughly half of all bars close against any    |
//| given side, so the average hold is near two bars and the trade    |
//| count rises sharply. Each round trip pays the spread; on          |
//| FixedVol100 that is about 0.97 in price, roughly 1% of ATR(H1).   |
//|                                                                   |
//| NO PYTHON COUNTERPART.                                            |
//+------------------------------------------------------------------+
input group "--- Candle-colour exit (no Python counterpart) ---"
input bool   InpCandleExitEnabled = true;  // Close on the first bar closing against the position
input bool   InpCandleReentry     = true;  // Re-enter on the next bar closing back in favour
input int    InpReentryMaxBars    = 10;    // Forget the re-entry after this many bars. 0 = never expire

//+------------------------------------------------------------------+
//| STRUCTURAL STOP - the low of a recent candle, not a distance.     |
//|                                                                  |
//| For a BUY the stop is the LOW of the candle InpStructStopBack     |
//| bars before the last closed one; for a SELL, that candle's HIGH.  |
//| At the default of 2, holding long, the level is the low of the    |
//| candle two before the one that just closed.                       |
//|                                                                  |
//| ====================================================================
//| WHY THIS EXISTS WHEN THE CANDLE EXIT ALREADY CLOSES ON RED
//| ====================================================================
//| It does not duplicate the candle-colour exit; it covers the hole  |
//| in it. That rule cannot look until a bar has CLOSED, so it bounds |
//| the loss in time and not in price - one violent bar takes         |
//| whatever it takes before the rule gets a vote. This is a real     |
//| broker-side level sitting on the book, so it fires INTRABAR.      |
//|                                                                  |
//| It also explains why the default mode ignores candle colour. The  |
//| rule as asked for was "a red candle breaks the low two candles    |
//| back", but a long is already closed by the FIRST red close, so    |
//| that combination can never be reached while still holding. What   |
//| is reachable, and what actually hurts, is price taking out that   |
//| low DURING a bar. STRUCT_STOP_BROKER protects against exactly     |
//| that; STRUCT_STOP_ON_CLOSE implements the literal reading instead |
//| and accepts that it can only act after the fact.                  |
//|                                                                  |
//| ====================================================================
//| IT RATCHETS
//| ====================================================================
//| The reference candle changes every bar, so the raw level moves    |
//| both ways. A stop that moves AWAY from price is not a stop - it   |
//| gives back protection already banked - so by default the level    |
//| only ever tightens: it may rise under a long and fall over a      |
//| short, never the reverse. Turn InpStructStopRatchet off to track  |
//| the raw candle wherever it goes.                                  |
//|                                                                  |
//| PRECEDENCE: this is combined with, not instead of, whatever the   |
//| percentage/ATR stop and the trail produce - whichever level is    |
//| TIGHTER wins. With all of those at zero it is simply the stop.    |
//|                                                                  |
//| NO PYTHON COUNTERPART.                                            |
//+------------------------------------------------------------------+
enum ENUM_STRUCT_STOP_MODE
  {
   STRUCT_STOP_BROKER = 0, // Real level on the book - fires intrabar, any colour (default)
   STRUCT_STOP_ON_CLOSE    // Only when a bar CLOSES against the position past the level
  };

input group "--- Structural stop: candle low/high (no Python counterpart) ---"
input bool InpStructStopEnabled = true;               // Stop at a recent candle's low (buy) / high (sell)
input int  InpStructStopBack    = 2;                  // Candles before the last CLOSED one. 2 = "two candles before"
input bool InpStructStopRatchet = true;               // Only ever tighten the level, never widen it
input ENUM_STRUCT_STOP_MODE InpStructStopMode = STRUCT_STOP_BROKER; // How it fires

input group "--- Dashboard ---"
input bool   InpShowDashboard    = true;    // On-chart status panel
input int    InpDashX            = 12;      // Panel X offset, pixels
input int    InpDashY            = 18;      // Panel Y offset, pixels

input group "--- Execution ---"
input double InpLots              = 1.00;    // Volume in MT5 LOTS (1.00 = 100 oz = Python default)
input long   InpMagic             = 20260905;// Differs from GoldTrendlineBreakout 20260902 and the Python adapter 20260828
input ulong  InpSlippagePoints    = 30;      // Max deviation, points
input int    InpMaxSpreadPoints   = 0;       // Block NEW entries above this spread. 0 = off
input bool   InpAllowNewEntries   = true;    // false = manage open positions only
input string InpComment           = "AlgoGold Camarilla"; // Cosmetic only - MT5 overwrites it

//+------------------------------------------------------------------+
//| State                                                            |
//+------------------------------------------------------------------+
CGoldTrader      g_trader;
TrailState       g_trail;
ENUM_TIMEFRAMES  g_tf          = PERIOD_CURRENT;
datetime         g_lastBarTime = 0;
//--- Salvage state, cached per position so the bar replay runs once a bar
//--- rather than once a tick. Keyed on entry+side, which is enough: this
//--- expert never holds two positions and never reverses without a flat bar.
double           g_salvageEntry  = 0.0;
int              g_salvageSide   = -1;
bool             g_salvageMarked = false;
CGoldDashboard   g_dash;
datetime         g_lastDashPaint = 0;
double           g_chanHigh      = 0.0;
double           g_chanLow       = 0.0;
//--- Camarilla levels for the current trading day, sorted ascending, with the
//--- resolved band half-width beside each one. Rebuilt when the day rolls over:
//--- the input is one completed daily bar, so nothing moves in between.
#define CAM_LEVELS 10
double           g_camLevel[CAM_LEVELS];
string           g_camName[CAM_LEVELS];
bool             g_camValid      = false;
datetime         g_camDay        = 0;
double           g_camRange      = 0.0;
double           g_camClose      = 0.0;
//--- Print throttles. Both of these sit on per-TICK paths, and a refusal that
//--- persists for an hour would otherwise write an hour of identical lines.
int              g_atrHandle     = INVALID_HANDLE;
int              g_amaHandle     = INVALID_HANDLE;
int              g_demaHandle    = INVALID_HANDLE;
datetime         g_trendWarnAt   = 0;
//--- Re-entry allowance left behind by a candle-colour exit: which side was
//--- closed, and how many more bars it may be reopened within.
//--- Ratcheted structural stop level for the position currently held. Zero
//--- when flat; reset the moment a position opens or closes so a level from
//--- the previous trade can never be applied to the next one.
double           g_structStop    = 0.0;
int              g_reentrySide   = -1;
int              g_reentryLeft   = 0;
datetime         g_camWarnAt     = 0;
datetime         g_camScaleLogAt = 0;

//+------------------------------------------------------------------+
//| trendline_breakout.warmup_bars(): lookback + 1. The channel needs |
//| `lookback` prior bars plus the one being tested against them.     |
//+------------------------------------------------------------------+
int WarmupBars()
  {
   return InpLookback + 1;
  }

//+------------------------------------------------------------------+
//| The stop as a PERCENTAGE, whichever way it was configured.        |
//|                                                                   |
//| One source of truth. Everything downstream - the bar-close exit    |
//| check, the broker-side stop level, the log line - is percentage-   |
//| based, so a point setting is resolved against the reference price  |
//| once and then behaves identically. Without this the entry would    |
//| use points and the next bar would overwrite it with the percent.   |
//+------------------------------------------------------------------+
//+------------------------------------------------------------------+
//| Money moved per 1.0 of PRICE, at the configured volume.           |
//|                                                                   |
//| Via tick_value/tick_size rather than contract size, because tick   |
//| value is already denominated in the ACCOUNT currency - the         |
//| contract-size route is right only while quote and account currency |
//| coincide. Returns 0 when the symbol reports nothing usable, and    |
//| every caller treats that as "fall through to the next unit"        |
//| rather than inventing a distance.                                  |
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
//| ATR of the last CLOSED bar on InpAtrTimeframe, or 0 if unusable. |
//|                                                                  |
//| Shift 1, never 0: bar zero's high and low are still moving, and a |
//| stop distance that changes within the bar it is measured on is    |
//| the same look-ahead the channel already refuses.                  |
//+------------------------------------------------------------------+
double CurrentAtr()
  {
   if(g_atrHandle==INVALID_HANDLE)
      return 0.0;
   double buf[];
   if(CopyBuffer(g_atrHandle,0,1,1,buf)!=1)
      return 0.0;
   if(buf[0]<=0.0 || !MathIsValidNumber(buf[0]))
      return 0.0;
   return buf[0];
  }

double EffectiveStopPct(const double refPrice)
  {
   if(InpStopLossAtrMult > 0.0 && refPrice > 0.0)
     {
      const double atr = CurrentAtr();
      if(atr > 0.0)
         return (InpStopLossAtrMult*atr)/refPrice*100.0;
      //--- Not fatal: the handle can be warming up on the first ticks after a
      //--- restart. Falling through to the next unit keeps the position
      //--- protected rather than leaving it naked while ATR fills.
     }
   if(InpStopLossMoney > 0.0 && refPrice > 0.0)
     {
      const double perPrice = MoneyPerPrice();
      if(perPrice > 0.0)
         return (InpStopLossMoney/perPrice)/refPrice*100.0;
      Print("WARNING: InpStopLossMoney set but the symbol reports no usable tick "
            "value - falling back to points/percent");
     }
   if(InpStopLossPoints > 0 && refPrice > 0.0)
      return InpStopLossPoints*_Point/refPrice*100.0;
   return InpStopLossPct;
  }

//+------------------------------------------------------------------+
//| Target distance in PRICE. Points win over percent when set.       |
//| The target is attached once at entry and never re-derived, so it   |
//| needs no percentage round trip.                                    |
//+------------------------------------------------------------------+
double TakeDistancePrice(const double refPrice)
  {
   if(InpTakeProfitMoney > 0.0)
     {
      const double perPrice = MoneyPerPrice();
      if(perPrice > 0.0)
         return InpTakeProfitMoney/perPrice;
      Print("WARNING: InpTakeProfitMoney set but the symbol reports no usable tick "
            "value - falling back to points/percent");
     }
   if(InpTakeProfitPoints > 0)
      return InpTakeProfitPoints*_Point;
   if(InpTakeProfitPct > 0.0 && refPrice > 0.0)
      return refPrice*InpTakeProfitPct/100.0;
   return 0.0;
  }

//+------------------------------------------------------------------+
//| SALVAGE: was this position adverse enough, early enough?          |
//|                                                                   |
//| Replayed from the bars covering the first InpSalvageWindowMin      |
//| minutes after entry. Returns the worst adverse excursion in MONEY,|
//| 0.0 when there was none.                                          |
//+------------------------------------------------------------------+
double EarlyAdverseMoney(const GoldPosition &pos)
  {
   if(!pos.exists || pos.entry<=0.0)
      return 0.0;
   const int firstBar = iBarShift(_Symbol,g_tf,pos.openTime,false);
   if(firstBar<0)
      return 0.0;
//--- how many bars cover the window; at least the entry bar itself
   const int perBar = (int)PeriodSeconds(g_tf);
   int windowBars = (perBar>0) ? (int)MathCeil(InpSalvageWindowMin*60.0/perBar) : 1;
   if(windowBars<1)
      windowBars = 1;
//--- shifts firstBar (entry bar) down to firstBar-windowBars+1, never past 1
   const int lastShift = MathMax(1, firstBar-windowBars+1);
   double worst = 0.0;
   for(int shift=firstBar; shift>=lastShift; shift--)
     {
      const double adverse = (pos.side==POSITION_TYPE_BUY)
                             ? (pos.entry - iLow(_Symbol,g_tf,shift))
                             : (iHigh(_Symbol,g_tf,shift) - pos.entry);
      if(adverse>worst)
         worst = adverse;
     }
   if(worst<=0.0)
      return 0.0;
   const double perPrice = MoneyPerPrice();
   return (perPrice>0.0) ? worst*perPrice : 0.0;
  }

//+------------------------------------------------------------------+
//| The adverse move that counts as "started badly".                  |
//| 0 means one spread - the smallest move that is not just the cost  |
//| of having opened the position.                                    |
//+------------------------------------------------------------------+
double SalvageLossTrigger()
  {
   if(InpSalvageLossMoney>0.0)
      return InpSalvageLossMoney;
   const double spread   = (double)SymbolInfoInteger(_Symbol,SYMBOL_SPREAD)*SymbolInfoDouble(_Symbol,SYMBOL_POINT);
   const double perPrice = MoneyPerPrice();
   return (spread>0.0 && perPrice>0.0) ? spread*perPrice : 0.0;
  }

//+------------------------------------------------------------------+
//| Trail activation as a PERCENTAGE, measured from ENTRY.            |
//|                                                                   |
//| TrailIsArmed compares against TrailFavourableMovePct, which is    |
//| (peak - entry) / entry, so the money amount is divided by entry.  |
//+------------------------------------------------------------------+
double EffectiveTrailActivationPct(const double entry)
  {
   if(InpTrailActivationMoney > 0.0 && entry > 0.0)
     {
      const double perPrice = MoneyPerPrice();
      if(perPrice > 0.0)
         return (InpTrailActivationMoney/perPrice)/entry*100.0;
     }
   return InpTrailActivationPct;
  }

//+------------------------------------------------------------------+
//| Trail distance as a PERCENTAGE, measured from the PEAK.           |
//|                                                                   |
//| TrailLevel computes give-back as peak * trailPct / 100, so to     |
//| surrender a fixed money amount the percentage must be taken       |
//| against the peak - not the entry. Using entry here would make the |
//| give-back grow as the peak advanced, which is the opposite of a   |
//| fixed-money trail.                                                |
//|                                                                   |
//| Falls back to entry when there is no peak yet (trail not started).|
//+------------------------------------------------------------------+
double EffectiveTrailPct(const double peakOrEntry)
  {
   if(InpTrailMoney > 0.0 && peakOrEntry > 0.0)
     {
      const double perPrice = MoneyPerPrice();
      if(perPrice > 0.0)
         return (InpTrailMoney/perPrice)/peakOrEntry*100.0;
     }
   return InpTrailPct;
  }

//+------------------------------------------------------------------+
//| The peak to measure the trail give-back against.                  |
//+------------------------------------------------------------------+
double TrailReference(const double entry)
  {
   return (g_trail.active && g_trail.peak > 0.0) ? g_trail.peak : entry;
  }

//+------------------------------------------------------------------+
//| Which unit is actually in force, for the init banner.             |
//+------------------------------------------------------------------+
string StopUnitName()
  {
   if(InpStopLossAtrMult > 0.0 && CurrentAtr() > 0.0)
      return StringFormat("%.2f x ATR(%s,%d)",InpStopLossAtrMult,
                          StringSubstr(EnumToString(InpAtrTimeframe),7),InpAtrPeriod);
   if(InpStopLossMoney  > 0.0) return StringFormat("%.2f money",InpStopLossMoney);
   if(InpStopLossPoints > 0)   return StringFormat("%d points",InpStopLossPoints);
   return StringFormat("%.2f%%",InpStopLossPct);
  }
string TakeUnitName()
  {
   if(InpTakeProfitMoney  > 0.0) return StringFormat("%.2f money",InpTakeProfitMoney);
   if(InpTakeProfitPoints > 0)   return StringFormat("%d points",InpTakeProfitPoints);
   return StringFormat("%.2f%%",InpTakeProfitPct);
  }

//+------------------------------------------------------------------+
//| Init                                                             |
//+------------------------------------------------------------------+
int OnInit()
  {
   g_tf = (ENUM_TIMEFRAMES)_Period;
   TrailClear(g_trail);

   if(InpLookback<2)
     {
      PrintFormat("FATAL: lookback must be at least 2, got %d",InpLookback);
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpTakeProfitPct<0.0)
     {
      Print("FATAL: InpTakeProfitPct cannot be negative");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpStopLossMoney<0.0 || InpTakeProfitMoney<0.0)
     {
      Print("FATAL: money stop/target cannot be negative");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpStopLossPoints<0 || InpTakeProfitPoints<0)
     {
      Print("FATAL: point stop/target cannot be negative");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpStopLossAtrMult<0.0)
     {
      Print("FATAL: InpStopLossAtrMult cannot be negative");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpCamBufferPct<0.0)
     {
      Print("FATAL: InpCamBufferPct cannot be negative");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(!GoldPreflight(InpMagic,InpStopLossPct,InpTrailActivationPct,InpTrailPct))
      return INIT_PARAMETERS_INCORRECT;

   if(!g_trader.Init(_Symbol,InpMagic,InpLots,InpSlippagePoints,InpComment))
      return INIT_FAILED;

   if(InpStopLossAtrMult > 0.0)
     {
      if(InpAtrPeriod < 1)
        {
         PrintFormat("FATAL: InpAtrPeriod must be at least 1, got %d",InpAtrPeriod);
         return INIT_PARAMETERS_INCORRECT;
        }
      g_atrHandle = iATR(_Symbol,InpAtrTimeframe,InpAtrPeriod);
      if(g_atrHandle==INVALID_HANDLE)
        {
         PrintFormat("FATAL: could not create ATR(%d) on %s - error %d",
                     InpAtrPeriod,EnumToString(InpAtrTimeframe),GetLastError());
         return INIT_FAILED;
        }
     }

   if(InpTrendEnabled)
     {
      if(InpAmaPeriod<1 || InpAmaFast<1 || InpAmaSlow<1 || InpDemaPeriod<1)
        {
         Print("FATAL: AMA/DEMA periods must all be at least 1");
         return INIT_PARAMETERS_INCORRECT;
        }
      if(InpAmaFast>=InpAmaSlow)
         PrintFormat("WARNING: AMA fast %d is not below slow %d - the adaptive step "
                     "cannot span the range it is meant to interpolate across",
                     InpAmaFast,InpAmaSlow);
      g_amaHandle = iAMA(_Symbol,InpTrendTimeframe,InpAmaPeriod,InpAmaFast,InpAmaSlow,
                         0,InpTrendPrice);
      g_demaHandle = iDEMA(_Symbol,InpTrendTimeframe,InpDemaPeriod,0,InpTrendPrice);
      if(g_amaHandle==INVALID_HANDLE || g_demaHandle==INVALID_HANDLE)
        {
         PrintFormat("FATAL: could not create AMA(%d,%d,%d) or DEMA(%d) on %s - error %d",
                     InpAmaPeriod,InpAmaFast,InpAmaSlow,InpDemaPeriod,
                     EnumToString(InpTrendTimeframe),GetLastError());
         return INIT_FAILED;
        }
     }

//--- A percentage stop resolves to a different PRICE distance on every symbol,
//--- and the broker refuses anything inside SYMBOL_TRADE_STOPS_LEVEL. Measured
//--- on this account: XAUUSD 0.20, BTCUSD 0.00, FixedVol100 7.123. A 0.5% stop
//--- is comfortable on all three, but a 0.1% one is illegal on FixedVol100 -
//--- and an expert configured that way sends orders that are ALL rejected while
//--- looking perfectly healthy (D-141). Warned rather than refused, because the
//--- distance moves with price and a level that is legal now may not be later.
     {
      const int    digits = (int)SymbolInfoInteger(_Symbol,SYMBOL_DIGITS);
      const double point  = SymbolInfoDouble(_Symbol,SYMBOL_POINT);
      const double stops  = (double)SymbolInfoInteger(_Symbol,SYMBOL_TRADE_STOPS_LEVEL)*point;
      const double price  = SymbolInfoDouble(_Symbol,SYMBOL_BID);
      const double slPct  = EffectiveStopPct(price);
      const double slNow  = (slPct>0.0) ? price*slPct/100.0 : 0.0;
      const double tpNow  = TakeDistancePrice(price);
      if(InpStopLossAtrMult>0.0)
        {
         const double atrNow = CurrentAtr();
         if(atrNow>0.0)
            PrintFormat("ATR(%s,%d) = %.*f, so the stop is %.2f x that = %.*f (%.3f%% of %.2f)",
                        StringSubstr(EnumToString(InpAtrTimeframe),7),InpAtrPeriod,
                        digits,atrNow,InpStopLossAtrMult,
                        digits,InpStopLossAtrMult*atrNow,slPct,price);
         else
            Print("WARNING: ATR is not available yet - the stop is falling back to "
                  "money/points/percent until it fills. Check this line again after a "
                  "few bars.");
        }
      PrintFormat("bracket at %.2f: stop %.*f (%s), target %.*f (%s), broker minimum %.*f (%d points)",
                  price,
                  digits,slNow,StopUnitName(),
                  digits,tpNow,TakeUnitName(),
                  digits,stops,(int)SymbolInfoInteger(_Symbol,SYMBOL_TRADE_STOPS_LEVEL));
      PrintFormat("  1 point = %.*f price here, so the broker minimum is %d points",
                  digits,point,(int)SymbolInfoInteger(_Symbol,SYMBOL_TRADE_STOPS_LEVEL));
//--- With a money target the price distance depends on VOLUME, so state what
//--- the configured lots actually buy - and what the levels are worth in money
//--- whichever unit was used, since that is the number being reasoned about.
      const double perPrice = MoneyPerPrice();
      if(perPrice > 0.0)
        {
         PrintFormat("  at %.2f lots, 1.0 of price = %.2f in account currency",
                     g_trader.Lots(),perPrice);
         PrintFormat("  so this bracket risks %.2f to make %.2f (%.2f:1)",
                     slNow*perPrice,tpNow*perPrice,
                     (slNow>0.0 ? tpNow/slNow : 0.0));
         //--- The number that decides whether the size is survivable. It is not
         //--- readable from InpLots or from the stop alone, only from both
         //--- against the account, so it is stated rather than left to be
         //--- worked out.
         const double equityNow = AccountInfoDouble(ACCOUNT_EQUITY);
         if(equityNow>0.0 && slNow>0.0)
           {
            const double riskPct = slNow*perPrice/equityNow*100.0;
            PrintFormat("  RISK PER TRADE: %.2f on %.2f equity = %.2f%%",
                        slNow*perPrice,equityNow,riskPct);
            if(riskPct>2.0)
               PrintFormat("  WARNING: %.2f%% of the account on one trade. The broker "
                           "minimum lot here is %.2f; at %.2f lots this is %.1fx that. "
                           "Cut InpLots, or trade a smaller instrument.",
                           riskPct,SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_MIN),
                           g_trader.Lots(),
                           g_trader.Lots()/MathMax(SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_MIN),0.01));
           }
        }
      else
         Print("  WARNING: symbol reports no usable tick value - money-based "
               "stop/target cannot be converted and will fall back to points/percent");

//--- The trail, in whatever unit it is configured in, with the resolved
//--- distances so the money case can be checked rather than assumed.
      if(InpTrailMoney > 0.0 || InpTrailPct > 0.0)
        {
         if(InpTrailMoney > 0.0 && perPrice > 0.0)
            PrintFormat("  trail: arms at %.2f profit, then keeps the stop %.2f behind the "
                        "peak (= %.*f and %.*f in price at %.2f lots)",
                        InpTrailActivationMoney,InpTrailMoney,
                        digits,InpTrailActivationMoney/perPrice,
                        digits,InpTrailMoney/perPrice,g_trader.Lots());
         else
            PrintFormat("  trail: arms at %.2f%% profit, then %.2f%% behind the peak",
                        InpTrailActivationPct,InpTrailPct);
         Print("  the trail can never sit worse than entry (cost-to-cost clamp), so its "
               "floor is a scratch rather than a loss");
        }
      else
         Print("  trail: OFF (both InpTrailMoney and InpTrailPct are 0)");

//--- Salvage, with the resolved trigger so "0 = one spread" is a number
//--- rather than a promise.
      if(InpSalvageEnabled)
        {
         const double trig = SalvageLossTrigger();
         PrintFormat("  salvage: ON - if the trade is %.2f down within its first %d minute(s), "
                     "close it at the first %s",
                     trig,InpSalvageWindowMin,
                     (InpSalvageExitProfit>0.0
                      ? StringFormat("%.2f of profit",InpSalvageExitProfit)
                      : "profit above zero"));
         if(trig<=0.0)
            Print("  WARNING: salvage trigger resolved to 0 - no position can ever be marked. "
                  "Set InpSalvageLossMoney explicitly.");
        }
      else
         Print("  salvage: OFF");

//--- Scale-in and the basket exit, with the worst case spelled out. The
//--- exposure question is the one worth answering before it runs, not after.
      if(InpScaleInEnabled && InpScaleInMaxTrades>1)
        {
         PrintFormat("  scale-in: ON - same side, at every %.2f of loss, up to %d position(s), "
                     "lot x%.2f each time",
                     InpScaleInLossMoney,InpScaleInMaxTrades,InpScaleInLotMult);
//--- Walk the actual ladder rather than describing it: each rung is normalised
//--- to the volume step, so the multiplier alone does not tell you the sizes.
         string ladder = "";
         double totalLots = 0.0;
         for(int k=0; k<InpScaleInMaxTrades; k++)
           {
            const double rung = g_trader.NormaliseVolume(
                                   g_trader.Lots()*MathPow(MathMax(InpScaleInLotMult,1.0),k));
            totalLots += rung;
            ladder += StringFormat("%s%.2f",(k>0?" + ":""),rung);
           }
         PrintFormat("  ladder: %s = %.2f lots total",ladder,totalLots);
         if(perPrice>0.0 && g_trader.Lots()>0.0)
           {
            //--- perPrice is money-per-price at the BASE lot, so scale it by the
            //--- ladder's total volume to get the whole book's exposure.
            const double perPriceAll = perPrice*(totalLots/g_trader.Lots());
            if(slNow>0.0)
               PrintFormat("  WORST CASE: all %d stopped at %.*f = about %.2f, before slippage",
                           InpScaleInMaxTrades,digits,slNow,slNow*perPriceAll);
            else
              {
               //--- With no stop there is no worst case to quote, so the
               //--- yardstick is a move the instrument actually makes: one
               //--- previous-day range. Silence here would be the wrong
               //--- answer - this is exactly when the number matters.
               PrintFormat("  NO STOP: the full ladder is %.2f lots = %.2f per 1.0 of "
                           "adverse price. There is no worst case to quote.",
                           totalLots,perPriceAll);
               if(CamarillaRefresh(TimeCurrent()) && g_camRange>0.0)
                 {
                  const double bleed = g_camRange*perPriceAll;
                  const double equity = AccountInfoDouble(ACCOUNT_EQUITY);
                  PrintFormat("  NO STOP: one previous-day range (%.*f) against the full "
                              "ladder is about %.2f%s",
                              digits,g_camRange,bleed,
                              (equity>0.0
                               ? StringFormat(" - %.0fx the %.2f equity", bleed/equity, equity)
                               : ""));
                  if(equity>0.0 && bleed>equity)
                     Print("  WARNING: that is more than the account. A single ordinary "
                           "day going the wrong way closes it out. Lower "
                           "InpScaleInLotMult, InpScaleInMaxTrades or InpLots, or put "
                           "the stop back.");
                 }
              }
           }
        }
      else
         Print("  scale-in: OFF");

      if(InpBasketTakeMoney>0.0)
         PrintFormat("  basket exit: close ALL tickets at %.2f COMBINED profit "
                     "(with %d positions that is %.2f each)",
                     InpBasketTakeMoney,InpScaleInMaxTrades,
                     InpBasketTakeMoney/MathMax(InpScaleInMaxTrades,1));
      else
         Print("  basket exit: OFF");
      if(InpBasketTakeMoney>0.0 && InpSalvageEnabled && InpSalvageExitProfit<InpBasketTakeMoney)
         Print("  NOTE: salvage exits at a LOWER profit than the basket target, so on a "
               "marked position salvage fires first and the basket target is never reached");
      const int minPoints = (int)SymbolInfoInteger(_Symbol,SYMBOL_TRADE_STOPS_LEVEL);
      if(stops>0.0 && slNow>0.0 && slNow<stops)
         PrintFormat("WARNING: the stop resolves to %.*f here, INSIDE the broker's %.*f "
                     "minimum. Entries will be REJECTED. Raise it to at least %d points "
                     "(or %.3f%%).",digits,slNow,digits,stops,minPoints,stops/price*100.0);
      if(stops>0.0 && tpNow>0.0 && tpNow<stops)
         PrintFormat("WARNING: the target resolves to %.*f here, INSIDE the broker's %.*f "
                     "minimum. Entries will be REJECTED. Raise it to at least %d points "
                     "(or %.3f%%).",digits,tpNow,digits,stops,minPoints,stops/price*100.0);
      if(!InpBracketAtEntry)
         Print("WARNING: InpBracketAtEntry is false - positions are opened with no stop and "
               "stay naked until the next closed bar applies one.");
     }

//--- CAMARILLA. The resolved TRIGGER is printed, not the percentage, because
//--- InpCamBufferPct is a share of price rather than of volatility: the same
//--- 2% is a seventh of a daily range on FixedVol100 and seven tenths of one on
//--- XAUUSD. The number worth checking is where the trigger actually lands
//--- relative to H4 and H5, which only the symbol in front of it can say.
   if(!InpCamEnabled)
      Print("camarilla filter: OFF");
   else if(!CamarillaRefresh(TimeCurrent()))
      Print("camarilla filter ON, but the previous daily bar is not available yet - open "
            "the D1 chart for this symbol once to download the series. Until it builds the "
            "filter passes EVERYTHING.");
   else
     {
      const int    camDigits = (int)SymbolInfoInteger(_Symbol,SYMBOL_DIGITS);
      const double camBid    = SymbolInfoDouble(_Symbol,SYMBOL_BID);
      const bool   camDir    = (InpCamMode==CAM_MODE_DIRECTIONAL);
      const string camLoName = CamPairName(false);
      const string camHiName = CamPairName(true);
      const double camRawLo  = CamLevel(camLoName);
      const double camRawHi  = CamLevel(camHiName);
      const double camLo     = camRawLo*(1.0-InpCamBufferPct/100.0);
      const double camHi     = camRawHi*(1.0+InpCamBufferPct/100.0);

      PrintFormat("camarilla filter ON: %s on %s/%s, buffer %.2f%% of price, "
                  "previous day close %.*f, range %.*f",
                  EnumToString(InpCamMode),camLoName,camHiName,InpCamBufferPct,
                  camDigits,g_camClose,camDigits,g_camRange);

      if(camDir)
         PrintFormat("  BUY only above %.*f (%s %.*f + %.*f), "
                     "SELL only below %.*f (%s %.*f - %.*f)",
                     camDigits,camHi,camHiName,camDigits,camRawHi,camDigits,camHi-camRawHi,
                     camDigits,camLo,camLoName,camDigits,camRawLo,camDigits,camRawLo-camLo);
      else
         PrintFormat("  no entry between %.*f and %.*f (%s..%s widened by %.2f%%), "
                     "%.*f wide",
                     camDigits,camLo,camDigits,camHi,camLoName,camHiName,InpCamBufferPct,
                     camDigits,camHi-camLo);

      //--- Where the trigger sits against the outer levels is the honest read
      //--- on how strict this is. A BUY trigger past H5 means the rule asks for
      //--- a move beyond the day's extreme projection, which is a very
      //--- different instruction from "clear H3".
      if(camDir && g_camRange>0.0)
        {
         PrintFormat("  buffer is %.0f%% of the previous day's %.*f range",
                     (camHi-camRawHi)/g_camRange*100.0,camDigits,g_camRange);
         const double camH4 = CamLevel("H4"), camH5 = CamLevel("H5");
         const double camL4 = CamLevel("L4"), camL5 = CamLevel("L5");
         PrintFormat("  BUY trigger %.*f sits %s H4 %.*f and %s H5 %.*f",
                     camDigits,camHi,(camHi>camH4?"ABOVE":"below"),camDigits,camH4,
                     (camHi>camH5?"ABOVE":"below"),camDigits,camH5);
         PrintFormat("  SELL trigger %.*f sits %s L4 %.*f and %s L5 %.*f",
                     camDigits,camLo,(camLo<camL4?"BELOW":"above"),camDigits,camL4,
                     (camLo<camL5?"BELOW":"above"),camDigits,camL5);
         if(camHi>camH5 || camLo<camL5)
            Print("  NOTE: a trigger beyond H5/L5 asks for a break past the day's own "
                  "extreme projection. Expect very few entries.");
        }

      for(int i=CAM_LEVELS-1;i>=0;i--)
         PrintFormat("  %s %.*f",g_camName[i],camDigits,g_camLevel[i]);

      string camNowWhy="";
      if(CamarillaBlocks(camBid,POSITION_TYPE_BUY,camNowWhy))
         PrintFormat("  a BUY is refused right now: %s",camNowWhy);
      else
         Print("  a BUY is allowed right now");
      if(CamarillaBlocks(camBid,POSITION_TYPE_SELL,camNowWhy))
         PrintFormat("  a SELL is refused right now: %s",camNowWhy);
      else
         Print("  a SELL is allowed right now");

      Print("  NOTE: the buffered rule is NOT one of the arms "
            "scripts/measure_camarilla_filter.py measured. INNER_L3_H3 is the arm that "
            "beat its random-block control; this default has no such backing.");

      //--- The source indicator refuses to load above D1 outright. Here it is
      //--- a warning rather than a refusal: the levels still compute, they
      //--- just stop being a meaningful intraday filter once one chart bar
      //--- spans a whole day or more, and a stale filter is a reason to say
      //--- so rather than to stop managing an open position.
      if(PeriodSeconds(g_tf)>=PeriodSeconds(PERIOD_D1))
         Print("  WARNING: the chart timeframe is D1 or higher. Camarilla levels are "
               "built from the PREVIOUS DAY, so on this timeframe they barely move "
               "within a bar and the filter is close to meaningless.");
     }

//--- TREND. The two averages, and which side they permit right now.
   if(!InpTrendEnabled)
      Print("trend filter (AMA vs DEMA): OFF");
   else
     {
      const int tDigits = (int)SymbolInfoInteger(_Symbol,SYMBOL_DIGITS);
      double ama=0.0, dema=0.0;
      //--- DEMA named first throughout, because DEMA-above-AMA is the buy
      //--- condition and reading it in that order is how the sign stays
      //--- straight. MQL5's PrintFormat has no positional arguments, so the
      //--- argument list is reordered rather than the format string.
      PrintFormat("trend filter ON: DEMA(%d) vs AMA(%d, fast %d, slow %d) on %s",
                  InpDemaPeriod,InpAmaPeriod,InpAmaFast,InpAmaSlow,
                  StringSubstr(EnumToString(
                     InpTrendTimeframe==PERIOD_CURRENT ? g_tf : InpTrendTimeframe),7));
      if(TrendValues(ama,dema))
         PrintFormat("  DEMA %.*f, AMA %.*f -> %s permitted right now",
                     tDigits,dema,tDigits,ama,
                     (dema>ama ? "BUY only" : "SELL only"));
      else
         Print("  not readable yet - entries are REFUSED until the buffers fill. "
               "This is deliberate: an unmet requirement is not a passed one.");
     }

//--- STRUCTURAL STOP.
   if(!InpStructStopEnabled)
      Print("structural stop: OFF");
   else
     {
      const int sDigits = (int)SymbolInfoInteger(_Symbol,SYMBOL_DIGITS);
      PrintFormat("structural stop ON: %s, %d candle(s) before the last closed one, "
                  "ratchet %s",
                  (InpStructStopMode==STRUCT_STOP_BROKER
                   ? "broker-side level, fires intrabar"
                   : "checked on the bar close only"),
                  InpStructStopBack,(InpStructStopRatchet?"on":"off"));
      const double buyLevel  = StructStopRaw(POSITION_TYPE_BUY);
      const double sellLevel = StructStopRaw(POSITION_TYPE_SELL);
      if(buyLevel>0.0 && sellLevel>0.0)
        {
         const double bidNow = SymbolInfoDouble(_Symbol,SYMBOL_BID);
         PrintFormat("  a BUY opened now would stop at %.*f (%.*f away), "
                     "a SELL at %.*f (%.*f away)",
                     sDigits,buyLevel,sDigits,bidNow-buyLevel,
                     sDigits,sellLevel,sDigits,sellLevel-bidNow);
        }
      else
         Print("  not enough closed bars yet to read the reference candle");
      if(InpStructStopMode==STRUCT_STOP_ON_CLOSE)
         Print("  NOTE: on-close mode cannot act until a bar has finished, so it "
               "bounds the loss in time rather than price - the same limitation the "
               "candle-colour exit has. STRUCT_STOP_BROKER is the one that fires "
               "intrabar.");
     }

//--- NO STOP. Said loudly, once, at init, because an expert running without one
//--- looks exactly like an expert running with one until the day it does not.
   if(EffectiveStopPct(SymbolInfoDouble(_Symbol,SYMBOL_BID))<=0.0)
     {
      Print("WARNING: NO STOP LOSS. InpStopLossPct, InpStopLossPoints and "
            "InpStopLossMoney are all zero, so positions are opened with sl=0 and "
            "nothing bounds a loser except the trail, salvage, the basket target and "
            "the opposite breakout.");
      if(InpScaleInEnabled)
        {
         PrintFormat("WARNING: SCALE-IN IS ALSO ON (%d adds, x%.2f lot multiplier). The "
                     "ladder's loss bound came from every ticket carrying a bracket; with "
                     "no stop there is no such bound, and an adverse run adds size to a "
                     "position that has no floor under it.",
                     InpScaleInMaxTrades,InpScaleInLotMult);
         if(InpTrailPct<=0.0 && InpTrailMoney<=0.0)
            Print("WARNING: no stop, no trail, and scale-in on. The only exits left are "
                  "salvage, the basket target and the opposite breakout - all of which "
                  "require price to come back.");
        }
     }

   const int available = Bars(_Symbol,g_tf);
   if(available < WarmupBars()+1)
      PrintFormat("WARNING: %d bar(s) of history, below the %d needed for a %d-bar channel - "
                  "no entry will be taken until enough bars have closed",
                  available,WarmupBars()+1,InpLookback);

   GoldTimeframeNote(g_tf);

//--- A reload mid-trade must not leave the position naked for a whole bar.
   const GoldPosition pos = g_trader.Snapshot();
   if(pos.exists)
     {
      RebuildTrail(g_trail,_Symbol,g_tf,pos);
      const double sl = ProtectiveStopPrice(g_trail,pos.side,pos.entry,
                                            EffectiveStopPct(pos.entry),
                                            EffectiveTrailActivationPct(pos.entry),
                                            EffectiveTrailPct(TrailReference(pos.entry)));
      g_trader.ApplyStop(sl);
      PrintFormat("adopted an existing %s position of %.2f lots at %.2f (magic %d)",
                  (pos.side==POSITION_TYPE_BUY?"BUY":"SELL"),pos.volume,pos.entry,(int)InpMagic);
     }

   if(InpShowDashboard)
      g_dash.Create("AGCam_",StringFormat("ALGOGOLD CAMARILLA  (%d)",(int)InpMagic),
                    InpDashX,InpDashY);

   g_lastBarTime = iTime(_Symbol,g_tf,0);
   PrintFormat("Donchian(%d) on %s %s | stop %.2f%% | trail %.2f%% from %.2f%% | magic %d",
               InpLookback,_Symbol,EnumToString(g_tf),
               EffectiveStopPct(SymbolInfoDouble(_Symbol,SYMBOL_BID)),
               EffectiveTrailPct(SymbolInfoDouble(_Symbol,SYMBOL_BID)),
               EffectiveTrailActivationPct(SymbolInfoDouble(_Symbol,SYMBOL_BID)),
               (int)InpMagic);
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   if(g_atrHandle!=INVALID_HANDLE)
     {
      IndicatorRelease(g_atrHandle);
      g_atrHandle = INVALID_HANDLE;
     }
   if(g_amaHandle!=INVALID_HANDLE)
     {
      IndicatorRelease(g_amaHandle);
      g_amaHandle = INVALID_HANDLE;
     }
   if(g_demaHandle!=INVALID_HANDLE)
     {
      IndicatorRelease(g_demaHandle);
      g_demaHandle = INVALID_HANDLE;
     }
   g_dash.Destroy();
   PrintFormat("stopped (reason %d). Open positions are LEFT AS THEY ARE - removing an "
               "expert is not a flatten instruction.",reason);
  }

//+------------------------------------------------------------------+
//| Tick: nothing happens except on a bar close.                     |
//+------------------------------------------------------------------+
void OnTick()
  {
//--- Salvage runs on the TICK, not the bar. "Close at the first profit" is
//--- worth little if it only looks once a minute; the whole point is to take
//--- the recovery when it appears. The expensive part (replaying the bars to
//--- decide whether the position was marked) is cached and refreshed on the
//--- bar boundary below, so this path is only a P&L read.
//--- Basket take profit FIRST: it closes everything, so there is no point
//--- adding to a position on the same tick that the basket is done.
   if(InpBasketTakeMoney>0.0 && CheckBasketTakeProfit())
      return;
   if(InpSalvageEnabled)
      CheckSalvageExit();
//--- Scale-in also runs on the tick: the trigger is a floating-loss level, and
//--- waiting for a bar close would add at a worse price than the level asked for.
   if(InpScaleInEnabled)
      CheckScaleIn();

   PaintDashboard();

   const datetime current = iTime(_Symbol,g_tf,0);
   if(current==g_lastBarTime || current==0)
      return;
   g_lastBarTime = current;
   OnClosedBar();
  }

//+------------------------------------------------------------------+
//| Realised P&L for our magic since a given time.                    |
//+------------------------------------------------------------------+
double RealisedSince(const datetime from)
  {
   if(!HistorySelect(from,TimeCurrent()+86400))
      return 0.0;
   double sum = 0.0;
   const int total = HistoryDealsTotal();
   for(int i=0; i<total; i++)
     {
      const ulong t = HistoryDealGetTicket(i);
      if(t==0)
         continue;
      if(HistoryDealGetString(t,DEAL_SYMBOL)!=_Symbol)
         continue;
      if(HistoryDealGetInteger(t,DEAL_MAGIC)!=InpMagic)
         continue;
      sum += HistoryDealGetDouble(t,DEAL_PROFIT)
             + HistoryDealGetDouble(t,DEAL_SWAP)
             + HistoryDealGetDouble(t,DEAL_COMMISSION);
     }
   return sum;
  }

//+------------------------------------------------------------------+
//| Repaint the panel. Throttled to once a second - chart objects are |
//| not free and nothing here changes faster than a human can read.   |
//+------------------------------------------------------------------+
void PaintDashboard(void)
  {
   if(!g_dash.Active())
      return;
   const datetime now = TimeCurrent();
   if(now==g_lastDashPaint)
      return;
   g_lastDashPaint = now;

//--- Rebuild anything deleted by hand. Deleting a panel object is easy to do by
//--- accident (Object List, or Ctrl+A on the chart), and a panel that stays half
//--- gone until the expert is re-attached reads as a bug.
   g_dash.Refresh(StringFormat("ALGOGOLD CAMARILLA  (%d)",(int)InpMagic));

   const int    digits = (int)SymbolInfoInteger(_Symbol,SYMBOL_DIGITS);
   const double bid    = SymbolInfoDouble(_Symbol,SYMBOL_BID);
   const GoldPosition pos = g_trader.Snapshot();

   const color cOk = C'120,220,140', cBad = C'240,110,110', cDim = C'150,160,180', cHot = C'255,200,90';

   int r = 0;
   g_dash.Set(r++,"SYMBOL / TF",
              StringFormat("%s  %s",_Symbol,StringSubstr(EnumToString(g_tf),7)),clrWhite);

   const bool canTrade = (bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)
                         && (bool)MQLInfoInteger(MQL_TRADE_ALLOWED);
   g_dash.Set(r++,"STATUS",
              (canTrade ? (InpAllowNewEntries ? "TRADING" : "MANAGE ONLY") : "ALGO OFF"),
              (canTrade && InpAllowNewEntries) ? cOk : cHot);

//--- The channel is the signal. Nothing else in MT5 shows it.
   if(g_chanHigh>0.0)
     {
      g_dash.Set(r++,"CHANNEL HI",StringFormat("%.*f  (%+.*f)",digits,g_chanHigh,
                                               digits,g_chanHigh-bid),cDim);
      g_dash.Set(r++,"CHANNEL LO",StringFormat("%.*f  (%+.*f)",digits,g_chanLow,
                                               digits,g_chanLow-bid),cDim);
     }
   else
      g_dash.Set(r++,"CHANNEL","warming up",cDim);

   g_dash.Set(r++,"SPREAD",StringFormat("%d pts",(int)SymbolInfoInteger(_Symbol,SYMBOL_SPREAD)),cDim);

//--- Camarilla proximity. The bands are invisible on the chart unless the
//--- indicator happens to be attached too, and "why did it not enter?" is
//--- exactly the question this panel exists to answer.
   if(!InpCamEnabled)
      g_dash.Set(r++,"CAMARILLA","off",cDim);
   else if(!CamarillaRefresh(TimeCurrent()))
      g_dash.Set(r++,"CAMARILLA","no daily bar",cHot);
   else
     {
      const int n = CamarillaNearest(bid);
      string buyWhy="", sellWhy="";
      //--- Both sides, because a directional region refuses one and allows the
      //--- other, and a panel that showed only "BLOCKED" would hide which.
      const bool noBuy  = CamarillaBlocks(bid,POSITION_TYPE_BUY,buyWhy);
      const bool noSell = CamarillaBlocks(bid,POSITION_TYPE_SELL,sellWhy);
      g_dash.Set(r++,"CAMARILLA",
                 StringFormat("%s %.*f  (%+.*f)",g_camName[n],
                              digits,g_camLevel[n],digits,g_camLevel[n]-bid),cDim);
      string zone = "clear";
      if(noBuy && noSell)
         zone = "BLOCKED";
      else if(noBuy)
         zone = "no LONG";
      else if(noSell)
         zone = "no SHORT";
      g_dash.Set(r++,"CAM ZONE",zone,((noBuy||noSell)?cHot:cOk));
     }

   if(!InpStructStopEnabled)
      g_dash.Set(r++,"STRUCT SL","off",cDim);
   else if(g_structStop>0.0)
      g_dash.Set(r++,"STRUCT SL",
                 StringFormat("%.*f  (%+.*f)",digits,g_structStop,
                              digits,g_structStop-bid),cHot);
   else
      g_dash.Set(r++,"STRUCT SL","-",cDim);

   if(!InpCandleExitEnabled)
      g_dash.Set(r++,"CANDLE EXIT","off",cDim);
   else if(g_reentrySide>=0)
      g_dash.Set(r++,"RE-ENTRY",
                 StringFormat("%s armed, %d bar(s) left",
                              (g_reentrySide==(int)POSITION_TYPE_BUY?"BUY":"SELL"),
                              g_reentryLeft),cHot);
   else
      g_dash.Set(r++,"RE-ENTRY","-",cDim);

//--- The two averages and, more usefully, which side they currently permit.
   if(!InpTrendEnabled)
      g_dash.Set(r++,"AMA/DEMA","off",cDim);
   else
     {
      double ama=0.0, dema=0.0;
      if(!TrendValues(ama,dema))
         g_dash.Set(r++,"AMA/DEMA","warming up",cHot);
      else
        {
         //--- DEMA first, because DEMA-minus-AMA is the sign that decides.
         g_dash.Set(r++,"DEMA/AMA",
                    StringFormat("%.*f / %.*f  (%+.*f)",digits,dema,digits,ama,
                                 digits,dema-ama),cDim);
         g_dash.Set(r++,"TREND",(dema>ama?"UP - longs only":"DOWN - shorts only"),
                    (dema>ama?cOk:cBad));
        }
     }

   if(pos.exists)
     {
      double floating = 0.0;
      for(int i=PositionsTotal()-1; i>=0; i--)
        {
         if(PositionGetSymbol(i)!=_Symbol) continue;
         if(PositionGetInteger(POSITION_MAGIC)!=InpMagic) continue;
         floating += PositionGetDouble(POSITION_PROFIT)+PositionGetDouble(POSITION_SWAP);
        }
      g_dash.Set(r++,"POSITION",
                 StringFormat("%s %.2f @ %.*f",(pos.side==POSITION_TYPE_BUY?"BUY":"SELL"),
                              pos.volume,digits,pos.entry),
                 (pos.side==POSITION_TYPE_BUY?cOk:cBad));
      g_dash.Set(r++,"TICKETS",StringFormat("%d",pos.tickets),cDim);
      g_dash.Set(r++,"FLOATING",StringFormat("%+.2f",floating),(floating>=0?cOk:cBad));
      //--- Trail and salvage state exist nowhere else in the terminal.
      const bool armed = TrailIsArmed(g_trail,EffectiveTrailActivationPct(pos.entry));
      g_dash.Set(r++,"TRAIL",(armed?"ARMED":"not armed"),(armed?cOk:cDim));
      g_dash.Set(r++,"SALVAGE",(g_salvageMarked?"MARKED":"clean"),(g_salvageMarked?cHot:cDim));
     }
   else
     {
      g_dash.Set(r++,"POSITION","flat",cDim);
      g_dash.Set(r++,"TICKETS","0",cDim);
      g_dash.Set(r++,"FLOATING","0.00",cDim);
      g_dash.Set(r++,"TRAIL","-",cDim);
      g_dash.Set(r++,"SALVAGE","-",cDim);
     }

   MqlDateTime t; TimeToStruct(TimeCurrent(),t);
   t.hour=0; t.min=0; t.sec=0;
   const datetime dayStart = StructToTime(t);
   const double today = RealisedSince(dayStart);
   const double week  = RealisedSince(dayStart-6*86400);
   g_dash.Set(r++,"TODAY",StringFormat("%+.2f",today),(today>=0?cOk:cBad));
   g_dash.Set(r++,"7 DAYS",StringFormat("%+.2f",week),(week>=0?cOk:cBad));
   g_dash.Set(r++,"EQUITY",StringFormat("%.2f",AccountInfoDouble(ACCOUNT_EQUITY)),clrWhite);

   ChartRedraw(0);
  }

//+------------------------------------------------------------------+
//| Close every ticket once the COMBINED floating profit reaches the  |
//| basket target. Returns true if it closed.                         |
//+------------------------------------------------------------------+
bool CheckBasketTakeProfit()
  {
   double profit  = 0.0;
   int    tickets = 0;
   for(int i=PositionsTotal()-1; i>=0; i--)
     {
      if(PositionGetSymbol(i)!=_Symbol)
         continue;
      if(PositionGetInteger(POSITION_MAGIC)!=InpMagic)
         continue;
      profit += PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
      tickets++;
     }
   if(tickets==0 || profit < InpBasketTakeMoney)
      return false;

   PrintFormat("basket take profit: %d ticket(s) combined at %.2f, target %.2f - closing all",
               tickets,profit,InpBasketTakeMoney);
   g_trader.CloseAll(StringFormat("basket take profit at %.2f across %d ticket(s)",
                                  profit,tickets));
   TrailClear(g_trail);
   g_salvageEntry  = 0.0;
   g_salvageSide   = -1;
   g_salvageMarked = false;
   return true;
  }

//+------------------------------------------------------------------+
//| Add to a losing position, same side, on a cumulative loss ladder. |
//+------------------------------------------------------------------+
void CheckScaleIn()
  {
   if(!InpAllowNewEntries)
      return;
   if(InpScaleInLossMoney<=0.0 || InpScaleInMaxTrades<2)
      return;

   const GoldPosition pos = g_trader.Snapshot();
   if(!pos.exists)
      return;
//--- Opposing tickets mean the netted view is not what is actually held, and
//--- adding to a book we are misreading is exactly how a small mess becomes a
//--- large one. Refuse rather than guess.
   if(pos.opposing)
     {
      Print("scale-in suppressed: opposing tickets under our magic - the netted view "
            "does not describe what is actually held");
      return;
     }
   if(pos.tickets >= InpScaleInMaxTrades)
      return;

//--- Live floating P&L across our tickets, the number the account would realise.
   double profit = 0.0;
   for(int i=PositionsTotal()-1; i>=0; i--)
     {
      if(PositionGetSymbol(i)!=_Symbol)
         continue;
      if(PositionGetInteger(POSITION_MAGIC)!=InpMagic)
         continue;
      profit += PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
     }
   if(profit >= 0.0)
      return;

//--- Add number `tickets` fires at `tickets * InpScaleInLossMoney` of loss, so
//--- the threshold moves away the instant an add lands and one tick cannot
//--- trigger two.
   const double needed = pos.tickets * InpScaleInLossMoney;
   if(-profit < needed)
      return;

   if(!SpreadIsAcceptable())
      return;

//--- The add carries its own bracket, priced from the CURRENT market rather
//--- than the original entry - it is a new position, not an extension.
   const double ref = (pos.side==POSITION_TYPE_BUY)
                      ? SymbolInfoDouble(_Symbol,SYMBOL_ASK)
                      : SymbolInfoDouble(_Symbol,SYMBOL_BID);
   if(ref<=0.0)
      return;

//--- The Camarilla gate, only if it was asked for. See InpCamFilterScaleIn:
//--- with a directional region this test is true almost whenever an add is
//--- wanted, because a losing breakout is by definition price coming back
//--- inside. The refusal is throttled to once a minute rather than once a tick,
//--- since the ladder threshold stays met for as long as price sits there.
   if(InpCamFilterScaleIn)
     {
      string camWhy="";
      if(CamarillaBlocks(ref,pos.side,camWhy))
        {
         if(TimeCurrent()>=g_camScaleLogAt)
           {
            g_camScaleLogAt = TimeCurrent()+60;
            PrintFormat("scale-in suppressed: camarilla - %s",camWhy);
           }
         return;
        }
     }

   const double slPct      = EffectiveStopPct(ref);
   const double slDistance = (slPct>0.0) ? ref*slPct/100.0 : 0.0;
   const double tpDistance = TakeDistancePrice(ref);

//--- Ladder size: base * mult^(adds so far). Normalised to the symbol's volume
//--- step, so "1.25x" becomes whatever the broker will actually accept - on a
//--- 0.1-step symbol 1.375 is dealt as 1.4, and the log states the real number.
   const double wanted = g_trader.Lots()*MathPow(MathMax(InpScaleInLotMult,1.0),pos.tickets);
   const double volume = g_trader.NormaliseVolume(wanted);
   if(volume<=0.0)
     {
      PrintFormat("scale-in suppressed: %.4f lots does not normalise to a tradable size",wanted);
      return;
     }

   const string reason = StringFormat("scale-in %d of %d: position is %.2f down, threshold %.2f",
                                      pos.tickets+1,InpScaleInMaxTrades,-profit,needed);
   PrintFormat("%s - adding %.2f lots %s at %s (ladder wanted %.4f, %.2f total on the book)",
               reason,volume,
               (pos.side==POSITION_TYPE_BUY?"BUY":"SELL"),
               DoubleToString(ref,g_trader.Digits()),
               wanted,pos.volume+volume);
   g_trader.OpenBracket(pos.side,volume,slDistance,tpDistance,reason);
  }

//+------------------------------------------------------------------+
//| Close a marked position the moment it turns profitable enough.    |
//+------------------------------------------------------------------+
void CheckSalvageExit()
  {
   const GoldPosition pos = g_trader.Snapshot();
   if(!pos.exists)
     {
      g_salvageEntry  = 0.0;
      g_salvageSide   = -1;
      g_salvageMarked = false;
      return;
     }

//--- Re-decide only when the position changed, not every tick.
   if(pos.entry!=g_salvageEntry || (int)pos.side!=g_salvageSide)
     {
      g_salvageEntry  = pos.entry;
      g_salvageSide   = (int)pos.side;
      const double adverse = EarlyAdverseMoney(pos);
      const double trigger = SalvageLossTrigger();
      g_salvageMarked = (trigger>0.0 && adverse>=trigger);
      if(g_salvageMarked)
         PrintFormat("salvage: MARKED - went %.2f against entry within the first %d minute(s), "
                     "trigger %.2f. Will close at the first %.2f of profit.",
                     adverse,InpSalvageWindowMin,trigger,InpSalvageExitProfit);
     }

   if(!g_salvageMarked)
      return;

//--- Live floating P&L, including swap and commission - the number the
//--- account actually realises, not a price-derived approximation.
   double profit = 0.0;
   bool   found  = false;
   for(int i=PositionsTotal()-1; i>=0; i--)
     {
      if(PositionGetSymbol(i)!=_Symbol)
         continue;
      if(PositionGetInteger(POSITION_MAGIC)!=InpMagic)
         continue;
      profit += PositionGetDouble(POSITION_PROFIT)
                + PositionGetDouble(POSITION_SWAP);
      found = true;
     }
   if(!found)
      return;

//--- 0 means "any profit above zero", so use a strict test there.
   const bool hit = (InpSalvageExitProfit>0.0) ? (profit>=InpSalvageExitProfit) : (profit>0.0);
   if(!hit)
      return;

   PrintFormat("salvage: closing at %.2f profit - this trade was adverse inside its first "
               "%d minute(s), so it is taken on recovery rather than held for the target",
               profit,InpSalvageWindowMin);
   g_trader.CloseAll(StringFormat("salvage exit at %.2f profit",profit));
   TrailClear(g_trail);
   g_salvageEntry  = 0.0;
   g_salvageSide   = -1;
   g_salvageMarked = false;
  }

//+------------------------------------------------------------------+
//| Rebuild the ten levels from the last COMPLETED daily bar.        |
//|                                                                  |
//| Keyed off the day containing `ref` exactly as the indicator is:   |
//| it takes the D1 bar covering one second before that day's open,   |
//| which is the previous day whatever the broker's server offset is. |
//| Returns false when the daily series is not downloaded yet.        |
//+------------------------------------------------------------------+
bool CamarillaRefresh(const datetime ref)
  {
   if(!InpCamEnabled)
      return false;

   const datetime dayOpen = ref - (ref % PeriodSeconds(PERIOD_D1));
   if(g_camValid && dayOpen==g_camDay)
      return true;

   MqlRates prev[];
   if(CopyRates(_Symbol,PERIOD_D1,dayOpen-1,1,prev)!=1)
     {
      g_camValid = false;
      return false;
     }
//--- A zero low would divide by zero in H5, and a non-positive range collapses
//--- every level onto the close. Either means the bar is not usable.
   if(prev[0].low<=0.0 || prev[0].high<=prev[0].low)
     {
      g_camValid = false;
      return false;
     }

   const double range = prev[0].high - prev[0].low;
   const double c     = prev[0].close;
   const double h5    = (prev[0].high/prev[0].low)*c;

   double v[CAM_LEVELS];
   string n[CAM_LEVELS];
   v[0]=c-(h5-c);         n[0]="L5";
   v[1]=c-range*1.1/2.0;  n[1]="L4";
   v[2]=c-range*1.1/4.0;  n[2]="L3";
   v[3]=c-range*1.1/6.0;  n[3]="L2";
   v[4]=c-range*1.1/12.0; n[4]="L1";
   v[5]=c+range*1.1/12.0; n[5]="H1";
   v[6]=c+range*1.1/6.0;  n[6]="H2";
   v[7]=c+range*1.1/4.0;  n[7]="H3";
   v[8]=c+range*1.1/2.0;  n[8]="H4";
   v[9]=h5;               n[9]="H5";

//--- Sort ascending. L5 sits below L4 and H5 above H4 for any sane daily bar,
//--- but the neighbour-gap basis reads ADJACENT entries and must not depend on
//--- that holding for every symbol and every session.
   for(int i=1;i<CAM_LEVELS;i++)
     {
      const double vk=v[i];
      const string nk=n[i];
      int j=i-1;
      while(j>=0 && v[j]>vk)
        {
         v[j+1]=v[j];
         n[j+1]=n[j];
         j--;
        }
      v[j+1]=vk;
      n[j+1]=nk;
     }

   for(int i=0;i<CAM_LEVELS;i++)
     {
      g_camLevel[i]=v[i];
      g_camName[i]=n[i];
     }


   g_camDay   = dayOpen;
   g_camRange = range;
   g_camClose = c;
   g_camValid = true;
   return true;
  }

//+------------------------------------------------------------------+
//| The raw structural level for `side`, before any ratchet.         |
//| 0 when the bar is not available.                                  |
//+------------------------------------------------------------------+
double StructStopRaw(const ENUM_POSITION_TYPE side)
  {
   if(!InpStructStopEnabled || InpStructStopBack<0)
      return 0.0;
   //--- Shift 1 is the last closed bar, so "two candles before" it is shift 3.
   const int shift = 1 + InpStructStopBack;
   const double level = (side==POSITION_TYPE_BUY) ? iLow(_Symbol,g_tf,shift)
                                                  : iHigh(_Symbol,g_tf,shift);
   if(level<=0.0)
      return 0.0;
   return level;
  }

//+------------------------------------------------------------------+
//| The level actually in force, ratcheted. Advances the ratchet, so  |
//| call it once per closed bar and read g_structStop elsewhere.      |
//+------------------------------------------------------------------+
double StructStopAdvance(const ENUM_POSITION_TYPE side)
  {
   const double raw = StructStopRaw(side);
   if(raw<=0.0)
      return g_structStop;

   if(!InpStructStopRatchet || g_structStop<=0.0)
     {
      g_structStop = raw;
      return g_structStop;
     }
   g_structStop = (side==POSITION_TYPE_BUY) ? MathMax(g_structStop,raw)
                                            : MathMin(g_structStop,raw);
   return g_structStop;
  }

//+------------------------------------------------------------------+
//| +1 for a bar that closed up, -1 down, 0 for an exact doji.        |
//+------------------------------------------------------------------+
int BarColour(const double barOpen,const double barClose)
  {
   if(barClose>barOpen)
      return 1;
   if(barClose<barOpen)
      return -1;
   return 0;
  }

//+------------------------------------------------------------------+
//| Forget any re-entry allowance. Called by every exit that is a     |
//| verdict on the direction rather than a pause in it.               |
//+------------------------------------------------------------------+
void ReentryClear()
  {
   g_reentrySide = -1;
   g_reentryLeft = 0;
  }

//+------------------------------------------------------------------+
//| AMA and DEMA on the last CLOSED bar. False if either is unusable. |
//+------------------------------------------------------------------+
bool TrendValues(double &ama,double &dema)
  {
   ama  = 0.0;
   dema = 0.0;
   if(g_amaHandle==INVALID_HANDLE || g_demaHandle==INVALID_HANDLE)
      return false;

   double a[], d[];
   if(CopyBuffer(g_amaHandle,0,1,1,a)!=1)
      return false;
   if(CopyBuffer(g_demaHandle,0,1,1,d)!=1)
      return false;
   if(!MathIsValidNumber(a[0]) || !MathIsValidNumber(d[0]) || a[0]<=0.0 || d[0]<=0.0)
      return false;

   ama  = a[0];
   dema = d[0];
   return true;
  }

//+------------------------------------------------------------------+
//| True when the averages refuse `side`. `why` explains.            |
//|                                                                  |
//| Fails CLOSED - see the input group's comment for why this differs |
//| from the Camarilla filter's fail-open.                            |
//+------------------------------------------------------------------+
bool TrendBlocks(const ENUM_POSITION_TYPE side,string &why)
  {
   why="";
   if(!InpTrendEnabled)
      return false;

   double ama=0.0, dema=0.0;
   if(!TrendValues(ama,dema))
     {
      why="AMA/DEMA are not readable yet (indicator warming up or short history)";
      if(TimeCurrent()>=g_trendWarnAt)
        {
         g_trendWarnAt = TimeCurrent()+60;
         Print("trend filter: AMA/DEMA unavailable - entries are REFUSED until they "
               "fill. This clears itself once enough bars have closed.");
        }
      return true;
     }

   const int digits=(int)SymbolInfoInteger(_Symbol,SYMBOL_DIGITS);
   if(side==POSITION_TYPE_BUY)
     {
      if(dema>ama)
         return false;
      why=StringFormat("a BUY needs DEMA above AMA, but DEMA %.*f <= AMA %.*f",
                       digits,dema,digits,ama);
      return true;
     }
   if(dema<ama)
      return false;
   why=StringFormat("a SELL needs DEMA below AMA, but DEMA %.*f >= AMA %.*f",
                    digits,dema,digits,ama);
   return true;
  }

//+------------------------------------------------------------------+
//| The selected pair's level names. One place, so the blocking rule, |
//| the init banner and the dashboard can never disagree about which  |
//| pair is in force.                                                 |
//+------------------------------------------------------------------+
string CamPairName(const bool upper)
  {
   switch(InpCamPair)
     {
      case CAM_PAIR_1: return upper ? "H1" : "L1";
      case CAM_PAIR_2: return upper ? "H2" : "L2";
      case CAM_PAIR_3: return upper ? "H3" : "L3";
      default:         break;
     }
   return upper ? "H4" : "L4";
  }

//+------------------------------------------------------------------+
//| One named level. Caller must have refreshed. Returns 0 if absent,|
//| which cannot happen for the five names this expert asks for.     |
//+------------------------------------------------------------------+
double CamLevel(const string name)
  {
   for(int i=0;i<CAM_LEVELS;i++)
      if(g_camName[i]==name)
         return g_camLevel[i];
   return 0.0;
  }

//+------------------------------------------------------------------+
//| Index of the level nearest `price`. Caller must have refreshed.  |
//+------------------------------------------------------------------+
int CamarillaNearest(const double price)
  {
   int    best     = 0;
   double bestDist = DBL_MAX;
   for(int i=0;i<CAM_LEVELS;i++)
     {
      const double d = MathAbs(price-g_camLevel[i]);
      if(d<bestDist)
        {
         bestDist = d;
         best     = i;
        }
     }
   return best;
  }

//+------------------------------------------------------------------+
//| True when `price` sits inside any level's band; `why` explains.  |
//|                                                                  |
//| A filter that cannot BUILD its levels does not block. The daily   |
//| series being absent is a data problem, and refusing every entry   |
//| for a whole session over a missing history download would be a    |
//| larger failure than the one this guards against. It says so in    |
//| the log - throttled, because this runs on the tick - so an        |
//| inactive filter is visible rather than silent.                    |
//+------------------------------------------------------------------+
bool CamarillaBlocks(const double price,const ENUM_POSITION_TYPE side,string &why)
  {
   why="";
   if(!InpCamEnabled || price<=0.0)
      return false;

   if(!CamarillaRefresh(TimeCurrent()))
     {
      if(TimeCurrent()>=g_camWarnAt)
        {
         g_camWarnAt = TimeCurrent()+60;
         Print("camarilla filter: no completed daily bar for this symbol - the filter is "
               "INACTIVE and entries are passing unchecked. Open the D1 chart for this "
               "symbol once to download the series.");
        }
      return false;
     }

   const int digits=(int)SymbolInfoInteger(_Symbol,SYMBOL_DIGITS);

//--- Every remaining region is bounded by two named levels. `lo`/`hi` are the
//--- pair, and whether a side is refused inside or outside them is the only
//--- thing that separates the interior rules from the directional ones.
   const string loName = CamPairName(false);
   const string hiName = CamPairName(true);
   const double rawLo = CamLevel(loName);
   const double rawHi = CamLevel(hiName);
   if(rawLo<=0.0 || rawHi<=0.0)
      return false;

//--- The buffer pushes each threshold further from the middle: a BUY needs the
//--- break to clear H3 by InpCamBufferPct of H3's own price, a SELL needs it to
//--- undercut L3 by the same share of L3's. On the interior regions the same
//--- arithmetic simply widens the refused zone, so one input means the same
//--- thing whichever region is selected.
   const double lo = rawLo*(1.0-InpCamBufferPct/100.0);
   const double hi = rawHi*(1.0+InpCamBufferPct/100.0);

   if(InpCamMode==CAM_MODE_INNER)
     {
      if(price<lo || price>hi)
         return false;
      why=StringFormat("%.*f is inside the %s..%s range-bound zone [%.*f, %.*f] "
                       "(levels %.*f/%.*f widened by %.2f%%)",
                       digits,price,loName,hiName,digits,lo,digits,hi,
                       digits,rawLo,digits,rawHi,InpCamBufferPct);
      return true;
     }

//--- Directional: a long has to be above the upper level, a short below the
//--- lower one. Anything else is an entry taken back into the day's balance.
   if(side==POSITION_TYPE_BUY)
     {
      if(price>hi)
         return false;
      why=StringFormat("a BUY at %.*f has not cleared %s %.*f by %.2f%% (needs %.*f)",
                       digits,price,hiName,digits,rawHi,InpCamBufferPct,digits,hi);
      return true;
     }
   if(price<lo)
      return false;
   why=StringFormat("a SELL at %.*f has not undercut %s %.*f by %.2f%% (needs %.*f)",
                    digits,price,loName,digits,rawLo,InpCamBufferPct,digits,lo);
   return true;
  }

//+------------------------------------------------------------------+
//| The channel over the `lookback` bars STRICTLY BEFORE bar 1.      |
//+------------------------------------------------------------------+
bool ChannelFromPriorBars(double &channelHigh,double &channelLow)
  {
   double highs[], lows[];
   const int gotHighs = CopyHigh(_Symbol,g_tf,2,InpLookback,highs);
   const int gotLows  = CopyLow(_Symbol,g_tf,2,InpLookback,lows);
   if(gotHighs<InpLookback || gotLows<InpLookback)
      return false;

   channelHigh = highs[ArrayMaximum(highs)];
   channelLow  = lows[ArrayMinimum(lows)];
   return true;
  }

//+------------------------------------------------------------------+
//| The whole strategy, in the order trendline_breakout.on_bar runs.  |
//+------------------------------------------------------------------+
void OnClosedBar()
  {
   const double close   = iClose(_Symbol,g_tf,1);
   const double high    = iHigh(_Symbol,g_tf,1);
   const double low     = iLow(_Symbol,g_tf,1);
   const double barOpen = iOpen(_Symbol,g_tf,1);
   if(close<=0.0 || barOpen<=0.0)
      return;

   const GoldPosition pos = g_trader.Snapshot();
   if(pos.opposing)
      Print("WARNING: opposing tickets on this symbol under our magic - both legs pay "
            "swap and spread while netting to a smaller exposure.");

   if(pos.exists && (!g_trail.active || g_trail.side!=pos.side || g_trail.entry!=pos.entry))
      RebuildTrail(g_trail,_Symbol,g_tf,pos);

//--- 1. Protective exits, BEFORE the warmup gate - a held position must never
//---    go unprotected because the channel has not been recomputed yet.
   const ExitKind fired = ProtectiveExitsCheck(g_trail,pos.exists,pos.side,pos.entry,
                                               high,low,EffectiveStopPct(pos.entry),
                                               EffectiveTrailActivationPct(pos.entry),
                                               EffectiveTrailPct(TrailReference(pos.entry)));
   if(fired!=EXIT_NONE)
     {
      //--- Normally the broker-side SL placed last bar has already fired
      //--- intrabar and this branch finds nothing to do. It covers the bar
      //--- where the stop could not be placed.
      const string reason = StringFormat("%s: %.2f%% level against a %s position, entry %.2f",
                                         ExitKindName(fired),
                                         (fired==EXIT_STOP?EffectiveStopPct(pos.entry)
                                                          :EffectiveTrailPct(TrailReference(pos.entry))),
                                         (pos.side==POSITION_TYPE_BUY?"BUY":"SELL"),pos.entry);
      g_trader.CloseAll(reason);
      TrailClear(g_trail);
      ReentryClear();
      g_structStop = 0.0;
      return;
     }

//--- 2. Move the broker-side stop to wherever this bar left it, combining the
//---    percentage/ATR/trail level with the structural one. Whichever is
//---    TIGHTER wins; with the others at zero the structural level is the stop.
   if(pos.exists)
     {
      double sl = ProtectiveStopPrice(g_trail,pos.side,pos.entry,
                                      EffectiveStopPct(pos.entry),
                                      EffectiveTrailActivationPct(pos.entry),
                                      EffectiveTrailPct(TrailReference(pos.entry)));

      const double structural = StructStopAdvance(pos.side);
      if(InpStructStopEnabled && structural>0.0)
        {
         //--- A structural level on the wrong side of the market is not a stop,
         //--- it is an instant close. That happens when the reference candle's
         //--- low already sits above price on a long; skip it and let the bar
         //--- close check below decide instead of sending a doomed order.
         const double market = (pos.side==POSITION_TYPE_BUY)
                               ? SymbolInfoDouble(_Symbol,SYMBOL_BID)
                               : SymbolInfoDouble(_Symbol,SYMBOL_ASK);
         const bool usable = (market<=0.0) ||
                             (pos.side==POSITION_TYPE_BUY ? structural<market
                                                          : structural>market);
         if(usable && InpStructStopMode==STRUCT_STOP_BROKER)
            sl = (sl<=0.0) ? structural
                           : ((pos.side==POSITION_TYPE_BUY) ? MathMax(sl,structural)
                                                            : MathMin(sl,structural));
        }
      g_trader.ApplyStop(sl);
     }

//--- 2a. Structural stop, checked on the close.
//---
//---     In STRUCT_STOP_ON_CLOSE this IS the rule: a bar that closes against the
//---     position having broken the level. In STRUCT_STOP_BROKER it is only the
//---     backstop for the bar where the level could not be placed - normally the
//---     broker-side order has already fired intrabar and this finds nothing.
   if(InpStructStopEnabled && pos.exists && g_structStop>0.0)
     {
      const int colour = BarColour(barOpen,close);
      const bool broke = (pos.side==POSITION_TYPE_BUY) ? (low<=g_structStop)
                                                       : (high>=g_structStop);
      const bool against = (pos.side==POSITION_TYPE_BUY && colour<0) ||
                           (pos.side==POSITION_TYPE_SELL && colour>0);
      const bool fires = (InpStructStopMode==STRUCT_STOP_ON_CLOSE)
                         ? (broke && against)
                         : broke;
      if(fires)
        {
         const string reason = StringFormat("structural stop: %s bar broke the %s of the "
                                            "candle %d before it (%.*f), %s position",
                                            (colour>0?"up":(colour<0?"down":"doji")),
                                            (pos.side==POSITION_TYPE_BUY?"low":"high"),
                                            InpStructStopBack,
                                            (int)SymbolInfoInteger(_Symbol,SYMBOL_DIGITS),
                                            g_structStop,
                                            (pos.side==POSITION_TYPE_BUY?"BUY":"SELL"));
         PrintFormat("%s",reason);
         g_trader.CloseAll(reason);
         TrailClear(g_trail);
         //--- A stop is a verdict, not a pause. Nothing to rejoin.
         ReentryClear();
         g_structStop = 0.0;
         return;
        }
     }

//--- 2b. Candle-colour exit. After the protective exits, so a stop that
//---     fired keeps precedence, and before the channel is rebuilt, because
//---     this rule does not need it.
   if(InpCandleExitEnabled && pos.exists)
     {
      const int colour = BarColour(barOpen,close);
      const bool against = (pos.side==POSITION_TYPE_BUY  && colour<0) ||
                           (pos.side==POSITION_TYPE_SELL && colour>0);
      if(against)
        {
         const string reason = StringFormat("candle exit: bar closed %s (%.2f -> %.2f) "
                                            "against a %s position of %.2f lots",
                                            (colour>0?"up":"down"),barOpen,close,
                                            (pos.side==POSITION_TYPE_BUY?"BUY":"SELL"),
                                            pos.volume);
         PrintFormat("%s",reason);
         g_trader.CloseAll(reason);
         TrailClear(g_trail);
         //--- A pause, not a verdict: remember the side so a bar that closes
         //--- back in favour can rejoin it.
         g_reentrySide = (int)pos.side;
         g_reentryLeft = (InpReentryMaxBars>0 ? InpReentryMaxBars : 2147483647);
         g_structStop  = 0.0;
         return;
        }
     }

//--- 3. Warmup.
   double channelHigh = 0.0, channelLow = 0.0;
   if(!ChannelFromPriorBars(channelHigh,channelLow))
     {
      PrintFormat("no entry: not enough closed bars for a %d-bar channel",InpLookback);
      return;
     }

   g_chanHigh = channelHigh;
   g_chanLow  = channelLow;

   const bool brokeUp   = (close>channelHigh);
   const bool brokeDown = (close<channelLow);

//--- 4. Held: a breakout is its own exit signal for the opposite side.
   if(pos.exists)
     {
      const bool wantsClose = (pos.side==POSITION_TYPE_BUY  && brokeDown) ||
                              (pos.side==POSITION_TYPE_SELL && brokeUp);
      if(!wantsClose)
         return;

      //--- Named from the break that actually fired, not derived from the
      //--- closing side: a short is closed by a fresh HIGH, and keying the
      //--- label off the side inverted it on both directions in the Python
      //--- until that was fixed.
      const string reason = StringFormat("trendline breakout: fresh %d-bar %s, flattening a %s "
                                         "position of %.2f lots (close %.2f vs channel [%.2f, %.2f])",
                                         InpLookback,(brokeUp?"high":"low"),
                                         (pos.side==POSITION_TYPE_BUY?"BUY":"SELL"),
                                         pos.volume,close,channelLow,channelHigh);
      g_trader.CloseAll(reason);
      TrailClear(g_trail);
      //--- A verdict, not a pause: the channel broke the other way, so the
      //--- side that was just closed is the wrong one to be waiting to rejoin.
      ReentryClear();
      g_structStop = 0.0;
      return;   // no reversal on the same bar
     }

//--- 5. Flat. Age the re-entry allowance first: it is measured in CLOSED
//---    bars spent flat, so it has to tick here rather than on the tick loop.
   if(g_reentrySide>=0 && g_reentryLeft<2147483647)
     {
      g_reentryLeft--;
      if(g_reentryLeft<=0)
        {
         PrintFormat("re-entry allowance for the %s side expired unused",
                     (g_reentrySide==(int)POSITION_TYPE_BUY?"BUY":"SELL"));
         ReentryClear();
        }
     }

//--- Either a fresh break, or a re-entry into the side a candle exit paused.
   int    wantSide   = -1;
   string wantReason = "";

   if(brokeUp)
     {
      wantSide   = (int)POSITION_TYPE_BUY;
      wantReason = StringFormat("trendline breakout: close %.2f above the %d-bar high %.2f",
                                close,InpLookback,channelHigh);
     }
   else if(brokeDown)
     {
      wantSide   = (int)POSITION_TYPE_SELL;
      wantReason = StringFormat("trendline breakout: close %.2f below the %d-bar low %.2f",
                                close,InpLookback,channelLow);
     }
   else if(InpCandleExitEnabled && InpCandleReentry && g_reentrySide>=0)
     {
      const int colour = BarColour(barOpen,close);
      const bool agrees = (g_reentrySide==(int)POSITION_TYPE_BUY  && colour>0) ||
                          (g_reentrySide==(int)POSITION_TYPE_SELL && colour<0);
      if(agrees)
        {
         wantSide   = g_reentrySide;
         wantReason = StringFormat("re-entry after a candle exit: this bar closed %s "
                                   "again (%.2f -> %.2f), %d bar(s) of allowance left",
                                   (colour>0?"up":"down"),barOpen,close,g_reentryLeft);
        }
     }

   if(wantSide<0)
      return;
   if(!InpAllowNewEntries)
     {
      Print("entry suppressed: InpAllowNewEntries is false");
      return;
     }
   if(!SpreadIsAcceptable())
      return;

   const ENUM_POSITION_TYPE side = (ENUM_POSITION_TYPE)wantSide;

//--- 5b. Camarilla proximity, tested against the price this entry would
//---     actually FILL at rather than the bar close. The rule is about where
//---     the position ends up, and on a breakout bar those two differ by a
//---     spread plus whatever has moved since the close. The bar close is the
//---     fallback only when there is no two-sided quote to read.
     {
      const double fill = (side==POSITION_TYPE_BUY)
                          ? SymbolInfoDouble(_Symbol,SYMBOL_ASK)
                          : SymbolInfoDouble(_Symbol,SYMBOL_BID);
      string camWhy="";
      if(CamarillaBlocks(fill>0.0 ? fill : close,side,camWhy))
        {
         PrintFormat("entry suppressed: camarilla - %s",camWhy);
         return;
        }
      //--- 5c. Trend confirmation. Second gate, and both must pass: the level
      //---     says price has left the balance area, the averages say the move
      //---     has a direction behind it.
      string trendWhy="";
      if(TrendBlocks(side,trendWhy))
        {
         PrintFormat("entry suppressed: trend - %s",trendWhy);
         return;
        }
     }

   const string reason = wantReason;
//--- Committed to the entry: the allowance has done its job either way.
   ReentryClear();

//--- 6. Send it, bracketed.
//---
//---    Open() sends sl=0 tp=0 and the stop is only applied on the NEXT closed
//---    bar by ApplyStop. That leaves every position naked for a full bar -
//---    an hour on H1 - and it is not theoretical: a live FixedVol100 position
//---    under this magic was observed sitting with sl=0.0.
//---
//---    Attaching the stop to the entry order is also MORE faithful to the
//---    Python, not less. price_stop.py checks the bar's low/high rather than
//---    its close precisely because it is standing in for a broker-side stop
//---    firing intrabar; a stop that does not exist until the next bar cannot
//---    do that. The bar-close check in step 1 stays as the backstop for the
//---    bar where the broker refuses the level.
   if(!InpBracketAtEntry)
     {
      g_trader.Open(side,reason);
      return;
     }

   const double ref = (side==POSITION_TYPE_BUY)
                      ? SymbolInfoDouble(_Symbol,SYMBOL_ASK)
                      : SymbolInfoDouble(_Symbol,SYMBOL_BID);
   if(ref<=0.0)
     {
      Print("entry suppressed: no two-sided quote to price the bracket against");
      return;
     }
   const double slPct   = EffectiveStopPct(ref);
   double slDistance    = (slPct>0.0) ? ref*slPct/100.0 : 0.0;
   const double tpDistance = TakeDistancePrice(ref);

//--- The structural level, measured from the price this order will fill at.
//--- The ratchet starts fresh here: a level carried over from the last trade
//--- would be anchored to a move this position was not part of.
   g_structStop = 0.0;
   if(InpStructStopEnabled && InpStructStopMode==STRUCT_STOP_BROKER)
     {
      const double structural = StructStopAdvance(side);
      if(structural>0.0)
        {
         const double distance = (side==POSITION_TYPE_BUY) ? ref-structural
                                                           : structural-ref;
         if(distance>0.0)
            slDistance = (slDistance>0.0) ? MathMin(slDistance,distance) : distance;
         else
            PrintFormat("structural stop not attached at entry: the candle %d bars back "
                        "is on the wrong side of %.*f - the bar-close check covers this "
                        "position until the level moves",
                        InpStructStopBack,
                        (int)SymbolInfoInteger(_Symbol,SYMBOL_DIGITS),ref);
        }
     }

   g_trader.OpenBracket(side,g_trader.Lots(),slDistance,tpDistance,reason);
  }

//+------------------------------------------------------------------+
//| A live-only guard with no counterpart in the backtest.           |
//| Blocks NEW entries when the book is abnormally wide; never blocks |
//| an exit, because refusing to leave a position because leaving is  |
//| expensive is how a small loss becomes a large one.                |
//+------------------------------------------------------------------+
bool SpreadIsAcceptable()
  {
   if(InpMaxSpreadPoints<=0)
      return true;
   const long spread = SymbolInfoInteger(_Symbol,SYMBOL_SPREAD);
   if(spread<=InpMaxSpreadPoints)
      return true;
   PrintFormat("entry suppressed: spread %d points is above the %d-point limit",
               (int)spread,InpMaxSpreadPoints);
   return false;
  }
