Analyse one classified ad and return the JSON described by the schema.

Read the `description` field as carefully as the `title`. Sellers put a short,
noisy line in the title and the real information in the description: the model
line, the wheel size, the frame material, the fork, the groupset, the defects.
A title like "Dámské horské kolo MERIDA (vel. 16") super stav" says almost
nothing, while its description names the model range and the wheel size. Never
conclude that a field is unknown before you have looked for it in the
description.

Work through it in this order:

1. Decide whether the ad sells a complete, rideable bicycle. A bare frame, a
   wheelset, a battery, spare parts, an accessory, a service or rental offer, or
   a "wanted to buy" ad is not a complete bicycle. Set `listing_type`
   accordingly and `classification.is_bicycle` to false for anything that is not
   a complete bicycle.
2. Identify brand, model, generation and model year, but only from what the ad
   states. Model names often appear in the description in forms such as
   "modelová řada X", "model X", "série X" or "typ X". `deterministic_hints`
   carries what the deterministic parser already found; treat it as a hint to
   verify, not as truth, and feel free to contradict it when the ad says
   otherwise.
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

Frame size and wheel size are different measurements and are easy to confuse.
A number attached to "vel.", "velikost rámu", "rám", "size" or "Rahmen" is the
frame size. A number attached to "kola", "kolech", "palců", "wheels" is the
wheel size, and so is a bare 26, 27.5, 28 or 29. Czech ads often state both:
"velikost rámu 16" ... jezdí na klasických 26" kolech" means frame 16 inches,
wheels 26 inches. When only a frame size is given, leave `wheel_size_inches`
as `null` rather than repeating the frame number.

Electric bicycles: set `identity.is_electric` to true only when the ad mentions
a motor, a battery, an e-bike term, or a known motor brand. Absence of these is
not proof of a non-electric bike; use `null` when you genuinely cannot tell.

DATA:
