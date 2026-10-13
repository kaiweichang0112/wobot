# The chat agent, v1 and v2

Phase B built the chat agent twice. v1 (`main` at `bef1701`) is one ReAct agent made
with `create_agent`: one prompt for every intent, four tools, a structured `Answer`, and
a guard that checks it. v2 (`phase-b-v2` at `972bdf2`) is a LangGraph workflow: a router
sends each message down one of four paths, and only lists and recommendations loop with
a model. Both were played on the same messages on 2026-10-11, against index version 7730
of the local database.

| | v1 | v2 |
| --- | --- | --- |
| Shape | one agent, six middleware | 11 nodes, 23 edges |
| Models | `gpt-5.6-luna`, reasoning medium, on every call | one per node, each chosen by evaluation: Jev routes, `gpt-5.6-luna` chats, `gpt-6-luna` elsewhere (DEC-059 to DEC-071) |
| Whether a product fits | judged by the model every turn, from any source | read once at ingestion as catalog facets, judged by code (DEC-069) |

Seconds are whole turns, router included. Cost counts OpenAI tokens at list prices;
v2's router adds about $0.03 per 1,000 turns. Both versions played each case after its
scripted history. v1's decisions map onto v2's: recommend stays, clarify and
explain_limitation are replies, and a turn its guard held back is unverified.

## Recommendations

recommendation-v2, dev, 14 cases × 3 runs, scored by v2's rules.

| | v1 | v2 |
| --- | --- | --- |
| Right | 9.7 / 14 (9–10) | 13.7 / 14 (13–14) |
| Never-shown products | 3 | 0 |
| Model calls per turn | 4.40 | 1.31 |
| Input / output tokens per turn | 26,461 / 801 | 4,039 / 98 |
| p50 / p95 | 14.94s / 21.11s | 2.07s / 4.56s, router excluded |
| $ per 1,000 turns | 6.25 | 0.45 |

Of v1's 13 misses, 6 come from rules set after v1:

- showing every fitting product when the user takes any (v1's guard showed one): NE-006 ×3
- judging from the catalog only (v1 read WhizPad's site, which states a computer view): REC-FX-04 ×2
- recommending rather than asking in one English case: CASE-025 ×1

The other 7 are v1's own:

- recommending again a product shown earlier: NE-008, NE-011 ×3
- answering an English message in Chinese: CASE-025 ×2
- asking instead of recommending three: NE-007
- a turn its guard held back: CASE-021

The preferred maker coming first is a v2 rule (DEC-065), so v1's 1 of 15 is not a miss.

## Every path

route-v1, dev, 24 messages × 3 runs. Paths are the labelled ones.

| Path (messages) | v1 p50 / p95 | v2 p50 / p95 | v1 / v2 model calls | v1 / v2 input tokens | v1 / v2 $ per 1,000 |
| --- | --- | --- | --- | --- | --- |
| Chat (5) | 2.44s / 3.23s | 2.21s / 5.06s | 1.00 / 1.20 | 2,909 / 1,348 | 0.67 / 0.21 |
| Knowledge (11) | 5.74s / 7.34s | 4.05s / 5.09s | 2.24 / 1.91 | 8,013 / 5,225 | 1.84 / 0.57 |
| List (5) | 4.64s / 7.50s | 4.29s / 5.05s | 2.00 / 2.00 | 8,355 / 4,159 | 1.92 / 0.46 |
| Recommendation (3) | 16.18s / 25.03s | 3.10s / 7.60s | 4.67 / 1.33 | 27,902 / 3,986 | 6.79 / 0.45 |
| All | 5.08s / 17.23s | 3.75s / 5.06s | | | |

v2's chat p95 comes from RT-028, which accepts chat or knowledge: v2 looks the price
up, v1 answers from the conversation. Without it, chat is 2.38s / 3.23s in v1 and
1.89s / 2.90s in v2. RT-026, labelled knowledge, goes to chat in v2 (DEC-059).

## Size

| | v1 | v2 |
| --- | --- | --- |
| Sent on every model call | 3,444 tokens: prompt 962, tools 1,452, answer schema 1,030, small talk included | per node: router 75, chat 191, answer 221, rewrite 516, lists 313 + 1,850 for tools, recommendations 1,821 + 1,199 |
| `agent/` lines of code | 1,558 | 2,860 |

v2 has more code: a prompt and schema per node, lists and cards rendered by code, and
facet judging. In return each prompt is small and evaluated alone, and checks that v1
asked of a guard over the model's output are plain code with tests.

## Limits

- One index version, and the comparison on dev cases only. V9 ran v2 alone once on each
  held-out split with its chosen models: route 12 of 12, recommendation 3 of 3 under
  the rules that came after this comparison, and two failures dev had not covered,
  fixed through new dev cases (reply language, a category narrowed by its own words).
- Answer quality is not compared between v1 and v2, since the reply formats differ. V8
  judged v2's own replies with gpt-6.1-sol, on what each model wrote, against a person's
  labels (DEC-074): the knowledge answer met the expected behaviour in 51 of 54 replies,
  each miss where the search found none of the labelled items, and all 54 were grounded;
  the list path's words in 47 of 48; the recommendation agent's own words in 16 of 16;
  the last turns of turns-v1 in 18 of 18. Chat is not judged.
- recommendation-v2 is labelled by v2's rules, which the misses above separate out.
  On 2026-10-12 recommendations changed to ask about differences before recommending
  (DEC-075), so the 13.7 of 14 above scores the rules of 2026-10-11; v1 was not played
  again under the new ones.
