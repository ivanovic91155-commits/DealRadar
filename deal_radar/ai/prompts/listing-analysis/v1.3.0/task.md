Analyse one classified ad and return the JSON described by the schema. The
request includes the ad's photos — usually the whole gallery, sometimes only the
cover. Read them together with the text.

Read the `description` field as carefully as the `title`. Sellers put a short,
noisy line in the title and the real information in the description: the model
line, the wheel size, the frame material, the fork, the groupset, the defects.
A title like "Dámské horské kolo MERIDA (vel. 16") super stav" says almost
nothing, while its description names the model range and the wheel size. Some
marketplaces send no description at all — then the photos are the only evidence
you have, and `image_count` tells you how many you were given. Never conclude
that a field is unknown before you have looked for it in the description and in
every photo.

Work through it in this order:

1. Decide whether the ad sells a complete, rideable bicycle. Use the photos
   here: a child seat, cycling shoes, a helmet, an indoor spinning trainer or
   exercise bike, a bare frame, a wheelset, a battery, spare parts, an
   accessory, a service or rental offer, or a "wanted to buy" ad is not a
   complete bicycle, even when the word "kolo" is in the title. Set
   `listing_type` accordingly and `classification.is_bicycle` to false for
   anything that is not a complete bicycle. Set `relevance_confidence` to how
   certain you are of that yes/no decision — high when you are sure, regardless
   of whether the answer is yes or no. A clear child seat or spinning trainer is
   a confident "no", so its `relevance_confidence` is high, not low.
2. Identify brand, model, generation and model year. Two sources count: what the
   ad states in words, and text you can actually read in a photo — a decal on
   the down tube, a name on the top tube, a model badge. Model names often
   appear in the description as "modelová řada X", "model X", "série X" or
   "typ X". `deterministic_hints` carries what the deterministic parser already
   found; treat it as a hint to verify, not as truth. A shape or a colour you
   recognise is not a model: if you cannot read the words, leave `null`.
3. Read the specifications from the text first, then from the photos: frame
   size, wheel size in inches, frame material, fork, groupset, brakes. A gallery
   usually shows the derailleur and the brakes clearly enough to name their
   class, and often the size printed on the seat tube. Normalise frame size to
   one of XS, S, M, L, XL, XXL when the ad gives a letter size or a centimetre
   size you can map confidently; otherwise keep `frame_size_normalized` as
   `null` and preserve the raw text.
4. Assess condition from what the seller claims, plus any defect, wear, damage,
   missing part or required service they mention or the photos plainly show:
   rust on the chain, a torn saddle, a bent rotor, a missing pedal. A tidy photo
   is not proof of good condition — keep `UNKNOWN` when unsaid and unseen.
5. Judge how urgent the seller sounds and how informative the ad is. Mark
   `hidden_opportunity` when the ad is poorly written or nearly empty yet the
   photos show what may be a valuable bicycle — a carbon frame, a suspension
   fork of a serious class, a high-end groupset. Those are worth a human look
   and are exactly what a bare marketplace listing hides.
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
a motor, a battery, an e-bike term, or a known motor brand, or a photo clearly
shows a hub or mid-drive motor or a frame/rack battery. Absence of these is not
proof of a non-electric bike; use `null` when you genuinely cannot tell.

DATA:
