Estimate what this bicycle would realistically sell for, second-hand, between
private people in Czechia, in the condition described.

Work through it in this order:

1. Establish what the bicycle actually is: brand, model, model year, category,
   wheel size, whether it is electric. Use `identity` and `specifications`
   first, then the raw title and description to fill gaps or correct them.
2. Decide how well you know this exact bicycle and set `basis` accordingly:
   - SAME_MODEL — you recognise this brand and model and roughly its market
     position across model years.
   - SIMILAR_MODEL — you do not know this exact model, but you know the brand
     and comparable models in the same range.
   - COMPONENT_CLASS — you price it from its parts and category: frame
     material, fork, groupset tier, brakes, wheel size.
   - GENERIC — the ad is too vague for anything beyond "a used bicycle of this
     type and age".
3. Anchor the price. If `partial_market_data` contains a new-bike price or any
   comparable listings, start from those and adjust. Otherwise start from what
   this class of bicycle was worth new, then depreciate for age.
4. Adjust for condition: wear, defects, required service, missing parts,
   documented service history, original papers. For electric bikes, weigh
   battery age heavily — a battery near end of life removes a large part of
   the value.
5. Produce the range. `price_low_czk` is what it would fetch from an impatient
   seller to a picky buyer; `price_high_czk` is a patient sale to the right
   buyer. `market_price_czk` is the realistic middle. Widen the range whenever
   you are unsure; a wide honest range is far more useful than a narrow guess.
6. Set `confidence`:
   - high — you know the model, the ad is detailed, the year is known.
   - medium — you know the model family or the ad is reasonably detailed.
   - low — vague ad, unknown model, or a segment whose prices you are unsure
     about.
7. Write `reasoning_summary` as one short sentence naming what actually drove
   the number: the model, its age, and the condition. No hedging boilerplate,
   no restating these instructions.

Sanity check before you answer: would a person selling this bicycle on a Czech
classifieds site plausibly get your `market_price_czk` for it within a month?
If the number is far from the asking price, that is allowed and useful — but
make sure it is a considered conclusion and not a slip of a decimal place.

DATA:
