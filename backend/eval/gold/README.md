# Gold labels

Reference answers for the evaluation dataset, written by a person reading the original
sources: the GRC and G-Tech websites, the WhizToys documentation, the PDFs the G-Tech
website links to and the product catalog workbook. Nothing here may come from what the
system extracted; a label copied from parser or model output measures nothing.

## Rules

- Copy from the source exactly: same characters, spelling and punctuation. Do not fix
  typos, translate or shorten.
- `.txt` files: one item per line, pasted from the page or the workbook. Lines starting
  with `#` are comments. If pasting splits one item over two lines, join them again.
- `.yaml` files: fill the quoted values. Inside `'...'`, write an apostrophe twice
  (`Women''s`). Copy a block to add another item.
- Leave out nothing that belongs: a list label is complete or it is wrong.
- In `retrieval.yaml` and `single-item-questions.yaml`, name the items that hold the
  answer, not the answer itself: the patent entries, not the countries they name.
- A talk, paper or profile passage may be named by a piece of its text, such as its
  title, when no other item has that piece. `check-gold` says when a piece fits several.
- A section of the GRC home page, the G-Tech pages or the WhizToys docs is named by its
  whole heading, by `page › heading` when another page has the same heading, or by a piece
  of one paragraph under it. A heading is not text: `2003` does not name `Since 2003`.
- When several items each answer a question alone, such as every page that gives the
  year GRC was founded, list them under one `any_of`: finding one of them finds the item.
  Items that answer different parts, such as each of G-Tech's products, stay separate,
  and every one must be found. Since 2026-10-07 retrieval scores count an `any_of` as one
  item; earlier reports counted each of its items and do not compare.

Check the files with:

```bash
uv run wobot-eval check-gold [--index-version <version>] [--show CASE-012,CASE-013]
```

It reports syntax errors and items it cannot find in the active version, or in another
one such as a dry run's. An item it cannot find is either a typo in the label or a defect
in ingestion; look at the source before changing the label. A piece of text that one item
holds is taken as naming it, even when that item is on the wrong site: `--show` prints
what each label of the given cases matched.

## Speech fields

`speech-fields.yaml` labels three parts of each talk. Each value must be one continuous
piece of the entry, copied as written.

- `title`: the talk's title, without the quotation marks around it.
- `event`: the conference, course, forum or meeting, with its organizer when the entry
  writes them together, as in `長庚大學「高齡科技與創新服務」課程`. It stops before the talk
  type (`keynote speech`, `plenary speech`, `invited speech`, `專題演講`, `主題演講`, `演講`,
  `講座`) and before a place that follows it: `Gerontechnology Conference – Lingnan
  University`, without `, Hong Kong`.
- `location`: the city, town or country where the talk was given, as the entry writes it,
  with the country when it follows the city (`Seoul, Korea`). One inside a name counts:
  `Hong Kong` in `the Hong Kong Polytechnic University`, `合肥` in
  `第三屆兩岸（合肥）健康養老產業合作論壇`, `新竹` in `國軍新竹醫院`. Only when the entry
  states none, the institution or venue where the talk was given, such as `長庚大學`. Empty
  when the entry names no place.
- The date and the `PDF` label belong to no field.
- Leave out the commas that separate the parts, also when one sits inside the closing
  quotation mark: `“Smart care in practice,” keynote speech, …` has the title
  `Smart care in practice`.

## Product facets

`product-facets.yaml` labels what each product's catalog text states, in the fixed
vocabulary of `src/wobot/knowledge/facets.py` (DEC-069). Recommendations filter products
by these facets, so a value the text does not state, or one it states but the label
lacks, decides what is recommended.

- Read the product's main features, usage and summary as the workbook writes them.
- List every facet. Each value takes the span that states it: one continuous piece of the
  text, copied exactly. `[]` says the text states none of that facet; leaving a facet out
  is an error.
- `functions`: what the product itself does, named by the vocabulary, not by the text's
  words: `離床預警` in the text is `離床偵測`. A nearby function is not the same: preventing
  falls or rating their risk is `跌倒預防`, not `跌倒偵測`; sleep is not bed exit. A
  measurement (`血壓量測`, `心率監測` and the like) is labelled only when the product itself
  measures it; a product that records or shows what other devices measure is
  `遠距健康管理`, and uploading data to a cloud alone is neither; a carer seeing a
  product's own readings from afar is `viewing`, not `遠距健康管理`, which is a product
  made to keep and follow health records over time. `緊急求助` is the person
  asking for help, not an alert the product sends by itself. `活動監測` is a person's
  activity in a room or in daily life, not body movement in bed (`體動`) or posture.
- `form`: `worn` is on the person cared for (a wristband, pendant, patch, insole or
  exoskeleton), not on staff, such as smart glasses a nurse wears. `bed` is the mattress, or
  a pad on or under it; a device at the bedside or on the wall above the bed is `room`.
  `device` is used only to measure, such as a blood-pressure monitor or a kiosk.
  `software` is a product that is software alone; a device's app or platform is how it is
  viewed, not its form. `service` is a service people provide or a platform that matches
  people to one; a powered exoskeleton is both `worn` and `robot`.
- `places`: home, institution (long-term care, nursing homes, day care, community
  centres), hospital (hospitals, clinics, wards, and `醫療` in a list of places such as
  `居家、醫療與長照場域`) or public (schools, companies, gyms, public spaces). A ward bed
  (`病床`), staff (nurses, carers, `醫療人員`) or residents alone name no place, nor does a
  system the product only connects or sends data to; a product said to help an
  institution run its care or manage its beds is used there.
- `sensing`: `camera` includes images and 3D depth sensing. A sensor, pad or mat whose kind
  the text does not name is no value.
- `viewing`: where a carer sees the data or the alert. `app` is a phone, tablet or other
  mobile device, or its app: `手機` is `app`, and `phone` is a voice call to a number.
  `computer` includes a PC, a desktop, web pages and central monitoring software; a
  platform or remote monitoring alone is no value. `device` is the unit itself, a nurse
  station or a call system.
- `power`: `mains` when the text says it is plugged in, or has a battery only to bridge a
  power cut; `battery` when it charges, runs on a battery or warns of a low battery.
- `users`: for activities only: `group` when several take part at once or compete.
- `split`: the dev products choose the extraction model; the held-out ones are scored
  only on what ingestion keeps.

`check-gold` reports a product that matches no record, and a span its record's text does
not hold.

## Transcriptions

`transcriptions.yaml` holds what a person reads in one picture: an image a page shows, or
one page of a PDF. It scores what the vision model reads in the same picture: the share of
lines it read (text recall) and the numbers it read right (value precision and recall).

- Name the picture by `image:`, its address, or by `document:` (`whizpad_catalog`,
  `whiztoys_catalog`, `whiztoys_manual`) and `page:`. An image's address may be the one a
  browser copies: the resized copy Wix serves is traced back to its file.
- `text`: each line, label or table cell the picture prints, as printed, one per item.
  Order does not count. Text in a photo, such as a brand on a mattress, counts when it is
  legible; text you cannot read with certainty is left out.
- `values`: each number with the unit printed beside it. The value is the number alone,
  as printed, with its separators and range signs (`1,200`, `5~10`); the unit is the unit
  or measure word printed beside it (`%`, `cm`, `mmHg`, `年`, `項`), or `''` when there is
  none. Words that qualify the number, such as `約`, `近`, `超過`, `餘`, `多`, `以上` or `+`,
  go in neither: `100餘位` is value `100`, unit `位`. A unit printed once for a row, a scale
  or an axis belongs to each of its numbers. A year counts as a number. Each number once
  per place it is printed: a scale printing 42 and 36 is two values.
- Numbers standing alone, such as a scale's, go in `values` only; `text` holds words.
- A line holding a number goes in `text` as printed, and its number in `values` too. A
  number that names rather than measures, such as a phone number, an address, a postal
  code or a model number, goes in `text` only.
- Text in a logo is left out: a wordmark's letters have no reading order and state no fact.
- `values: []` says the picture prints no number to list; left as the template's blank
  row, values are not scored for that picture, as when only some of a page's text is
  transcribed.
- What no eye tells apart in print counts as the same: full-width and half-width forms
  (`２０％` is `20%`), hyphens and dashes, middle dots, and spaces beside a Chinese
  character (`公司 SEDA` is `公司SEDA`).

The dev cases choose the vision model:

```bash
DB_USER=wobot_ingest_user uv run wobot-eval vision --model <model> --model <model> \
  [--document-file whizpad_catalog=<path.pdf>]
```

It makes paid calls, so ask before running it, and keeps the answers where ingestion
does, which takes the ingestion role. The heldout cases are never read by it; `wobot-eval
run` scores them on the version ingestion built.

## Judge labels

`judge-labels.yaml` holds a person's verdicts on replies a `wobot-eval answer` run kept,
which the judge is measured against. `wobot-eval judge-sample <judge report>` writes it
blank: every reply the judge failed or the text check disagreed with, filled up with
replies it passed, shuffled, without the judge's verdicts. It refuses to write over a file
that holds a label.

- Read the earlier turns, the message, what the case expects and the reply, and write
  `pass` or `fail` under `expected`: the reply does what the case expects. Judge the
  meaning, not the wording or the order, and do not require what the case marks optional
  ("may").
- Where an entry has `supported`, also read its case's passages and records under
  `evidence`: it passes when every fact the reply states is in them. A greeting, advice,
  encouragement and wording the user asked to be written are not facts, and neither is
  saying the sources do not state something. True is not enough: a fact the passages do
  not hold fails.
- Leave a verdict blank to come back to it; a blank is not a label. Add a `note`, in
  English, when the call was close.
- `reply_sha` ties a label to the reply's text: a label is never compared with another
  reply. Set `labelled_on` when the labels are finished.

`wobot-eval judge-agreement <judge report>` measures a judge run against the labels,
rewriting its report without a call; `wobot-eval judge` does so for each new run.
