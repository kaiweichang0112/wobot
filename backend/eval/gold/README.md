# Gold labels

Reference answers for the evaluation dataset, written by a person reading the original
sources: the GRC and G-Tech websites, the WhizToys documentation and the product catalog
workbook. Nothing here may come from
what the system extracted; a label copied from parser output measures nothing.

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
