Analyse one classified ad and return the JSON described by the schema.

Work through it in this order:

1. Decide whether the ad sells a complete, rideable bicycle. A bare frame, a
   wheelset, a battery, spare parts, an accessory, a service or rental offer, or
   a "wanted to buy" ad is not a complete bicycle. Set `listing_type`
   accordingly and `classification.is_bicycle` to false for anything that is not
   a complete bicycle.
2. Identify brand, model, generation and model year, but only from what the ad
   states. `deterministic_hints` carries what the deterministic parser already
   found; treat it as a hint to verify, not as truth, and feel free to
   contradict it when the ad says otherwise.
3. Read specifications: frame size, wheel size in inches, frame material, fork,
   groupset, brakes. Normalise frame size to one of XS, S, M, L, XL, XXL when
   the ad gives a letter size or a centimetre size you can map confidently;
   otherwise keep `frame_size_normalized` as `null` and preserve the raw text.
4. Assess condition from what the seller claims, plus any defect, wear, damage,
   missing part or required service they mention.
5. Judge how urgent the seller sounds and how informative the ad is. Mark
   `hidden_opportunity` when the ad is poorly written or nearly empty yet
   describes what may be a valuable bicycle — those are worth a human look.
6. Flag risks: a suspiciously cheap price for the described bicycle, signs of a
   stolen bike (no documents, no history, seller avoids questions, unusual
   urgency combined with a very low price), or scam patterns (advance payment,
   shipping-only, seller abroad, refuses to meet).

Electric bicycles: set `identity.is_electric` to true only when the ad mentions
a motor, a battery, an e-bike term, or a known motor brand. Absence of these is
not proof of a non-electric bike; use `null` when you genuinely cannot tell.

DATA:
