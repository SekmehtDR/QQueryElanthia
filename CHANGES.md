# Changes

Latest version: **1.1.0** (2026-10-29). The version lives in `src/epedia/__init__.py`; add each release here,
newest first. Format: what a user would notice first, then fixes, then internals.

## 1.1.0: a guide who does not make things up (in progress, 2026-10-29)

Work on making Fennick a robust conversation agent (see the plan's analysis of the real conversation log: invented
creatures, narrated "let me check" actions, drift in long chats, fan fiction). Phase 0, the foundation:

### New

- **Every turn is recorded** in a new `turns` table: how long the planner, the search and the first word took, what the
  planner decided, and the error if it failed. It is written even when the model is down or the reader closes the page
  mid-answer (`aborted by the reader`), so the owner can see what went wrong. Messages now store the `[n]` they cite and
  their turn. Schema version 3 (`db._migrate_v3`, additive).
- **Visitors through the tunnel may ask 40 questions an hour** (`[web] visitor_turns_per_hour`, 0 = no limit); the 41st
  gets a friendly in-voice "try again in about N minutes". The owner at the computer is never limited.

### Fixes

- **A question is never lost.** It is saved before the planner runs; before, a failed or abandoned answer left no trace at
  all. A half-written answer that failed is kept in the transcript (mode `error`) but never fed back to the model.
- **A dropped connection to Ollama is handled.** Transport errors become `OllamaError` (the planner falls back to a plain
  search instead of crashing the turn), and an answer that fails before its first word is tried once more
  (`[ollama] retry_before_first_token`); never after words were shown. The CLI `chat` reports a model error and keeps
  the conversation instead of exiting.
- **The prompt always fits the model's context window.** Older turns are dropped first, then passages from the bottom of
  the list (`prompts.fit_messages`, `[ollama] answer_reserve_tokens`).
- **`/api/chat` now needs the page's `X-QQE` header** like every other state-changing call, so another site cannot make a
  visitor's browser spend the GPU. The Origin check also accepts a tunnel's `X-Forwarded-Host`.
- **The clock and the crafting tables skip the planner** on a first message (about 1.5 to 9 seconds saved), and a
  crafting table built while planning is reused by retrieval instead of being built twice.
- The page shows a refusal's own text (the hourly limit) instead of raw JSON.

### Internals

- `core/turns.py` (`Turn.start/mark/finish/fail`), `memory.add_message` returns the new id, `memory.note_plan`;
  `Assistant.answer_stream` is a recorder around `_answer`; `Done.turn_id`, SSE `done.turn_id`; `access.TurnLimiter`.
  14 new tests (`tests/test_turns.py`); 291 in all.

### Phase 1: a verifier, and an evaluation that measures truthfulness

- **`core/verify.py` checks an answer against its passages, in code** (a few milliseconds, no model):
  - every citation the model wrote must be borne out by the passage(s) it cites: enough of the line's words, and every
    number in it (an unsupported one becomes `[n?]`);
  - capitalised names that appear in no passage, title, the question or what the user said earlier ("Shadow Serpents",
    "Moonwraiths");
  - pretended lookups ("Let me check that for Binu", "Let me take a closer look at that").

  Calibrated by replaying it over the 204 answers in the conversation log: 15% name something no passage has, 3 pretend
  to look something up, 17% of cited lines are not borne out by the passage they cite, and in 48% most factual lines
  carry no citation at all. Phase 3 acts on these; for now they are measured.
- **`epedia eval --full`** also answers every question and scores the answers (citation validity, share of lines cited,
  answers with unknown names or pretended lookups, wrong numbers, false refusals, latency p50/p90), runs
  **`eval/refusals.yaml`** (25 questions Fennick must decline: not about DragonRealms, invented names checked absent
  from the index, fiction) and **`eval/scripts.yaml`** (7 conversations, 18 turns, end to end through the real answer
  path: follow-ups, the hunting clarify, "are you sure?", the edit-history bait, a long Binu thread, fiction).
- **Every eval run is saved** under `eval/runs/` (git-ignored) and printed with its change since the previous run of
  the same kind. New metric `hit_in_prompt`: the expected page is among the passages that actually fit the prompt
  (hit@k counted pages the model may never have been shown).

### Fixes

- The automatic `[n*]` tag now reaches numbered steps ("2. Smelt the ingot" used to count the 2 as a number the
  passage lacked), and numbers written differently still match ("130-230", "1,500", "2027-01-02" for "January 2, 2027").

### Internals

- `attribution.Evidence` (word bags, weights, number veto) and `attribution.unit_words` are shared by attribution and
  verification; `evalx/faithfulness.py` (`is_refusal`, `model_text`, `check`, `aggregate`); `evalx/runner.py`
  (`answer`, `run_refusals`, `run_scripts`, `save_run`, `deltas`). 17 new tests; 308 in all.
- Baseline `eval --full` before Phase 2: hit@5 0.94, hit@10 0.98, MRR 0.911, expected page in the prompt 0.96, cited
  lines borne out 0.798, answers naming something no passage has 9.3%, false refusals 0, **must-decline questions
  declined 0.48**, script turns passing 72% (3 of 7 conversations clean), answer time p50 10.8 s / p90 14.6 s.

### Phase 2: a grounding gate, and one list of question kinds

- **A question about something the wiki never mentions is declined in code, without the model** ("I have nothing in my
  notes on “Zarnak the Unbound”, and I'd rather not guess..."), so the model cannot fill the gap with an invented
  creature or spell (`core/grounding.py`). It declines when every capitalised name the question gives occurs nowhere in
  the wiki as a phrase and the question asks little else, or when a third or more of its content words occur in no page
  at all. Measured: all 11 invented names in the refusal set are declined, plus "World Cup" and an invented swamp; none
  of the 50 answerable eval questions or 93 of 94 real logged questions are (the one is a typo, "hollowseve", which is
  honestly answered "nothing on that; if it is spelled differently, try me again"). About 2 ms per question.
- **Changed from the plan, on the evidence:** the plan gated on vector distance. Measured, distance cannot tell an
  answerable question from an unanswerable one ("Moonwraith Veil", invented, is as close to its nearest chunk as "Who is
  Binu?"), so it is recorded on every turn (`best_cos`, title match, lexical overlap) and never used to decide. The
  "thin evidence" and "outside Elanthia" outcomes it was to drive are dropped; off-topic questions (sourdough, France)
  are left to the model and the verifier, since the wiki's forum posts mention those words too.
- **Every answer is verified against the passages it was written from**, and the result is recorded on the turn
  (`turns.verify_json`) and in `Done.verification`; `Done.by_code` marks answers code wrote. Nothing the reader sees
  changes yet (Phase 3 acts on it); the eval now scores what the assistant measured instead of re-reading passages,
  which it could not do for the edit-history passage or the code-written tables.

### Fixes

- Search with `k_final=0` returns nothing, as asked; it used to fall back to 8 results.
- The planner's list of question kinds is built from `kinds.KIND_DESCRIPTIONS` (the prompt is byte-for-byte the same),
  so the kinds are defined in one place.
- `epedia doctor` checks the stored vectors are unit length, which converting vec0's L2 distance to cosine relies on.
- **Kept on the evidence:** the exact-title boost (0.05) is larger than any rank-fusion score, so a title match always
  wins. Lowering it to one or two rank-1 units (0.0164, 0.0328) gained nothing (hit@5 0.94, hit@10 0.98 at all three;
  MRR 0.911, 0.911, 0.901), so it stays.

### Internals

- `search.vec_search_dist`, `Hit.vec_dist`; `Plan.signals`; `grounding.Gate/assess/decline/names_in/in_wiki`;
  `faithfulness.check_done`. 8 new tests (`tests/test_grounding.py`); 316 in all.

### Phase 3: acting on the checks, honest capabilities, stories declined, "are you sure?" re-checked

- **A draft that names things no passage has, or pretends to look something up, is withdrawn and written once more**,
  at temperature 0, with a code-written instruction naming exactly what to leave out. The page clears the draft
  ("Fennick is checking his notes again") and shows the second; the terminal prints a dim note. It happens at most once
  per answer; replayed on the 205 stored answers it would have fired on about one in seven (some 10 s each).
  `[chat] regenerate = "off"` turns it off.
- **Everything else is marked, not rewritten.** A citation its passage does not bear out becomes `[n?]`, drawn as a
  dotted badge whose tooltip says to check it (and a legend entry); a "let me check" line is removed from the text; and
  one closing note, written by code, covers the most important thing left: a name still in no passage ("One caution:
  I can't find “X” anywhere in the pages I had for this..."), a figure that does not match its page, or an answer
  almost wholly untied to any page. Measured on the stored answers, a note on every `[n?]` or every half-uncited answer
  would have sat under two answers in three, so those cases rely on the marks.
- **Stories, poems and roleplay are declined in voice** ("Spinning a tale of my own isn't something I do; my notes are
  copied, not composed..."), recognised by rule before the planner. When the request names pages the wiki has, the
  decline leads and the model answers what the wiki says about them; when it names none, the decline is the whole
  answer and no model is asked. "Tell me the story of the Lyras War" is still a lore question.
- **"Are you sure?" re-reads the evidence.** A short challenge after a searched answer skips the planner (which filed it
  as a question about Fennick), asks the same question again with the last answer's cited pages first plus fresh
  results, and tells the model to confirm what they support, correct what they contradict and not cave to please.
- **Fennick says truthfully what he can and cannot do** (`core/capabilities.py`): the "about this assistant" passage
  lists both, and both answer styles gain one rule: nothing can be looked up while writing, so never say you will check.

- **Measured** (`eval --full`, against the Phase 1 baseline): must-decline questions declined **0.88** (was 0.48; all six
  story requests and all eleven invented names now decline; three general-knowledge questions, sourdough, a leaky faucet
  and quantum entanglement, are still answered); false refusals 0; script turns passing 78% (was 72%), 5 of 7
  conversations clean (was 3), "are you sure?" re-checked instead of filed as small talk; answers naming something no
  passage has 0% (was 9.3%), cited lines borne out 0.927 (was 0.798), figures not in their page 0 (was 44); 1 eval answer
  in 54 rewritten; retrieval unchanged (hit@5 0.94, MRR 0.911); answer time p50 9.8 s, p90 16.1 s (was 10.8 / 14.6: the
  rewrites). Part of the citation and figure gains is the measurement: since Phase 2 the eval reads the very passages each
  answer was given, where the baseline re-read them from the index and missed the edit-history passage and code tables.
- **Still failing, for Phase 4:** in the long Binu conversations the model carries "Warrior Mage" and an invented
  "Warrior Necro-Mage" over from its own earlier answers when the new turn's passages lack them; the rewrite cannot
  remove what the conversation keeps feeding it, so the closing note names them. Phase 4 replaces raw history with a
  summary built from the database.

### Start screen and Past chats

- **"Others are asking:"** at this computer, the start screen shows the last 20 distinct questions from the chats in
  Past chats in place of the fixed starter questions, each in the planner's standalone form ("What tools do I need for
  forging?", not "...for it?"), so any can be asked again with one click. Small talk, the clock, declined questions,
  story requests and re-checks are left out. With none, it says "No questions have been asked yet. Ask one now!"
  Deleting a chat removes its questions at once. The list is everything asked on the page, visitors' questions
  included, and visitors through the tunnel see the same list; only the owner, at this computer, can open or delete
  the chats behind it.
- **Past chats shows every conversation on the page**, visitors' too (tagged "visitor"), so the owner can read what
  people asked and what Fennick answered, vote on those answers, and delete them. Asking a question while reading a
  visitor's chat starts a fresh chat of the owner's own.
- **Delete all chats**: a button at the foot of Past chats deletes every conversation on the page, visitors' included,
  after a confirmation that says so.
- **Thumbs up or down under every answer.** A thumbs-down then offers four reasons to tick (wrong or made up, didn't
  answer my question, lost track of the conversation, too long or hard to follow) and a line for anything else; a
  second click on the same thumb takes the vote back. The vote is kept on the answer's turn record, so it stays with
  its conversation, shows again when the chat is reopened, and goes when the chat is deleted. Visitors through the
  tunnel can vote on their own answers, never on anyone else's.
- **`qqueryelanthia feedback`** lists the thumbs-down answers, newest first: who asked (you or a visitor), the question,
  the reasons and note, and what the checks found (names no page has, unsupported lines, a rewritten draft). `feedback
  <turn id>` shows that answer in its conversation with its full record (the plan, retrieval signals, checks, timings);
  `--all` adds thumbs-up, `--since` limits by date and `--json` prints everything for processing elsewhere.
- **Visitors through the tunnel cannot manage chats**: like the Index panel, Past chats and Delete all are hidden from
  them, and the server refuses the conversation list, a past conversation's messages and deleting them all. (Their chats
  are still private; there was never a way to resume one across a page reload.)
- The Copy answer button also strips `[n?]` marks.
- **About Fennick**: an "About" button beside Desktop and Mobile (or a click on his portrait) opens a page from his
  notes over the chat, with a close button (Escape or a click outside also close it). It opens "Hi, I'm Fennick!" with
  a larger portrait (`fennick_large.png`, 512 px, drawn by `tools/make_fennick.py` with the others), tells his origin
  in character, then out of character: made by Sekmeht, grounded in Elanthipedia only, and how he can be wrong.
- Polish: the thumbs-down note gets its own line; header buttons no longer wrap onto two lines in the mobile view.

### Fixed in review (before release)

- "Delete all chats" now passes the same checks as the Index controls, the Host header included, so a page that
  re-resolves its own name to this computer cannot wipe the chats.
- "Are you sure?" is a re-check only when it is the whole message or is followed by punctuation: "Is that true for
  Paladins too?" and "Are you sure there's no other way?" ask something new. It is never a re-check after a declined
  story or an item table (the table is rebuilt by the planner, which no longer files a challenge as small talk).
- Story requests no longer catch hypotheticals about play ("Imagine I'm a Barbarian with 50 ranks, what should I
  hunt?", "Pretend I am a new player"), "who would win" questions, or questions that merely mention fan fiction.
- A re-check may say it checked the passages again (it did): that no longer forces a second draft or gets cut, and the
  re-check prompt asks for the result, not the process.
- Removing a pretended look-up keeps a fact in the same line ("Let me re-read the passages: the sword weighs 10 stones"
  keeps the weight), and a sentence ending in a `[n?]` mark is split there like any other.
- `qqueryelanthia feedback <turn>` shows the [up]/[down] marker (Rich was reading it as a style tag).

### Internals

- `memory.recent_user_questions`, `memory.delete_sessions`; `GET /api/recent-questions` (everything asked on the page,
  for everyone), `DELETE /api/sessions` (local only, with the Index panel's checks). At this computer, listing, opening,
  deleting and voting reach every page conversation (`may_see` in `app.py`); through the tunnel, only one's own.
- `core/feedback.py` (`record`, `votes`, `conversation`, `REASONS`); `POST /api/turns/{id}/feedback` (the `X-QQE` header
  and ownership of the conversation required); `memory.messages` returns each answer's `turn_id` and `feedback`;
  `/api/persona` carries the reasons. 5 new tests (`tests/test_feedback.py`).
- `assistant.Revise` event (SSE `revise`), `Done.revised`, `turns.regenerated`; `verify.strip_fake_actions`,
  `verify.honesty_note`; `prompts.strict_addendum`, `prompts.RECHECK`; `memory.last_question`; citations `[n?]` are
  understood everywhere `[n]`/`[n*]` are (history stripping, sources, the transcript). `eval --full` reports
  `answers_revised`. Tests: 381 in all (`tests/test_verify_policy.py`, `tests/test_feedback.py`, render, sharing and page
  tests).

## 1.0.0: Fennick, a storyteller's guide (2026-10-22)

The first full release: the whole of Elanthipedia, answered by a persona.

### New

- **The Index panel shows the whole picture.** New buttons **Rebuild knowledge** (facts and topic graph, local) and
  **Health check** (the `doctor` report, left visible in the panel); new rows for **Automatic updates** (schedule, last and
  next run), **Edit history** (edits stored, pages whose older versions were read, pages still waiting) and **Knowledge**
  (facts, materials, disciplines, topics, and a warning when an extractor read far fewer facts than before); an interval
  box that changes the automatic-update schedule without a restart (stored in the database, 0 = off). Tooltips describe
  what each button now does.
- **Fennick keeps the index current while the server runs.** A delta update starts two minutes after start-up and then
  hourly (`[web] auto_sync_minutes`, 0 = off), as the same job the Index panel shows; skipped when another job is running.
  No Windows scheduled task is involved.
- **Crafting disciplines answered from the Crafting page's own list.** "What are the crafting disciplines?" is written in
  code: five Lore skills, the disciplines each controls, and the guide the page names for each (so the model can no longer
  call Tinkering a skill). New extractor `index/extract/crafting.py`, tool `core/disciplines.py`.
- **16 new evaluation questions** (names, topic families, materials, edit history) and two new `doctor` checks
  (disciplines read; extractors still read what they used to).
- **Fennick reads a page's edit history before trusting it.** Every delta now keeps edit metadata (who, when, size,
  summary, revert tags), and `history` (the "Fetch history" button) backfills the rest and reads the guild, race, gender
  and status of each character page's old versions. Answers about a page carry its last N edits (Index panel setting,
  local only, 0 = off) with a reading computed in code: steady, mixed or contested (reverts, large rewrites, anonymous
  editors, bursts; an owner tidying their own page is not a dispute). For characters each changed fact is judged by who
  set it and how long it stood: credible (the page's owner set or restored it), accepted, unverified (recent, by someone
  else) or disputed (changed back and forth). Fennick states credible facts plainly, flags the rest, and never chooses
  between disputed versions (`wiki/history.py`, `wiki/timeline.py`). 236 tests.
- **"Best material for X?" is answered from the crafting tables, in code.** The index now has a typed `facts` layer: the
  `enrich` stage (runs after indexing in every update, `epedia enrich [--force]` by hand) reads every table of
  Category:Crafting materials (metals, woods, bones, stones, cloth, leather: 2,437 facts for 298 materials) through
  small extractors in `index/extract/`. For weapons, armor and bows `core/materials.py` ranks the materials with the
  wiki's own rules, each cited to the page that states it (hardness drives weapon damage, density counts for weapons
  only up to 7.5 and keeps helping armor, bows follow the wood's affinity for the bow type, nothing is craftable under
  3.0 density, a mix keeps a metal's properties at 67%). A name that is not in the tables ("damascus steel", only a
  decoration in item names) is said to be one instead of being ranked. `epedia doctor` is a repeatable integrity and
  coverage report (revision lag, full-text/vector parity, link graph coverage, facts, rules still on their pages).
  The chunker folds small sections into a neighbour, so an extractor reads the heading line above a table, not only
  the chunk's section path. 251 tests.
- **Names, topics and kind rules.** A name in a question now resolves through the title index (`retrieval/resolve.py`):
  one name reaching different kinds of page is narrowed by the question's own words (cast, wield, hunt... in the new
  declarative `kinds.INTENT_WORDS`/`KIND_LABEL`/`KIND_GROUP`), or asked about ("Mo could mean the character Mo or the spell
  Marshal Order. Which did you mean?"), and an acronym like SIS sets aside pages that merely contain the letters (the
  Svidaw sis weapon). The new topic graph (`index/topics.py`, built by `enrich` from categories, subpages and links: 1,129
  hubs, 77,729 memberships) lets "crafting" favour its whole family in search (`retrieval.topic_boost`). Materials now
  also cover leather, cloth, bone and stone. A code-written answer cautions when its source page was heavily rewritten
  lately, `doctor` compares the materials table with all 297 material pages (they agree), and the game name the
  planner appends ("in MUD DragonRealms") is stripped so it stops pulling the pages called DragonRealms and MUD.
- **Updates keep history current.** After the one-off history read, every delta and full refresh also reads the edits
  since the last read and re-reads the key facts of each character page it changed (from the real wiki, never the
  mirror; a failure only logs). Before that read has been done they make no history requests.
  Trust rule: the page owner's (creator's) own edits are authoritative; a value someone else changed away from the
  owner's is suspect and Fennick says so (a note written by code). Review fixes: `mw-reverted` is no revert, piped links
  in infoboxes, whole-word fact matching, updates only top up a completed history read (`history.complete`).
- **The Index panel shows a running job live.** A run is only logged when it ends, so the panel sat on the previous run
  until then. A "This run" row now shows pages fetched and indexed so far, the rate, and the latest page titles, read
  from the pipeline's own timestamps (`jobs.live_progress`). 218 tests.
- **"What should I hunt?" is answered from the hunting ladder, in code.** The question is matched on combat ranks
  (offense and defense; the lower one decides), whatever the guild, because a circle alone does not give them. Without
  ranks Fennick asks for them; with ranks (and any areas named) `core/hunting.py` lists the creatures whose ladder range
  takes them in, nearest the middle first, and writes the answer itself so no creature or stat can be invented. The
  creature answer hint no longer asks for a stat line the passages do not give, and says to name only creatures that
  appear in them. 217 tests.

- **Fennick**, a Halfling Bard who keeps a satchel of notes copied from Elanthipedia. He frames places, people,
  gods and creatures in his own voice, keeps game mechanics plain and marked out of character, relays the wiki's
  own hedges (outdated, obsolete, incomplete, era, GM post versus player guide), and never claims experience of
  his own. Switch him off with `[persona] enabled = false`.
- **Portrait in the corner of every answer.** He sways while thinking and his mouth moves in time with the words
  as they arrive (two hand-drawn frames, `tools/make_fennick.py`).
- **"Fennick is thinking..." with animated dots** instead of an empty box, changing to "is leafing through his
  notes" once the search is done.
- **Version badge** next to his name (`v1.0.0 · <commit>`), with a tooltip listing the models, page count and last
  update. Read live from the server; `qqueryelanthia version` prints the same.
- **Index panel with controls** behind an "Index" button in the header: **Delta update** (only what changed) and
  **Full refresh** (re-walk and compare every page; asks to confirm), **Stop**, a live status line with elapsed
  time and a log tail, plus last delta, last full refresh, newest wiki edit seen, source and pending work. The
  panel is opened on demand and polls only while open or while a job runs. Jobs run as a child
  `qqueryelanthia sync` process, one at a time, and are stopped when the server shuts down.
- **Pasted text is understood, not mistaken for a wiki page.** A question with a Discord post or in-game message
  pasted along with it (before or after the question) is split into the question and the pasted material: the
  planner uses the material to name the people, places and events being asked about, the answer is told it is
  not a source and must not be cited or confused with a wiki page, and a later "what I sent you" or "that
  message" brings the paste back. Closing offers the model writes itself are removed (the program adds one),
  and old offers are no longer fed back to the model, which made it repeat them.
- **Closing offers are about the conversation.** They used to favour the most-linked pages, so a Dragon Priests
  chat got "want to hear about the Crossing?". Candidates are now ranked by how close they are to the question
  (and dropped when nothing fits), so the offers are things like the Black Spire or Suulnir.
- **"What is coming up next?" is answered by code, not guesswork.** For upcoming/next/current event questions the
  retrieved pages are sorted into still-ahead, over, and undated; rows of an event list whose dates are over are removed
  before the model sees them, and if everything found is over the reply is written in code ("I can't find anything
  scheduled after today; the most recent is X, ... already over"). The date reader understands the wiki's styles
  ("October 26, 2018", "Aug 29, 2024", "March 17th, 2018", "2026-11-02", "20270102") and ignores in-game dates.
- **Fennick knows what day and time it is.** "What time is it?" / "what's today's date?" are answered straight from the
  computer's clock (date, time and zone, with a pointer to the in-game TIME command), without searching; every other
  answer is told the date and time too.
- **Fennick knows what day it is.** "What festival is coming up next?" used to be answered from whichever dated page
  ranked first (a 2025 festival, offered as upcoming). The planner now gets the current year and adds it to
  questions about what is upcoming, next, current or latest, and each passage that contains real-world dates is
  tagged in code as over, upcoming, or mixed (`core/dates.py`), so the model does not do date arithmetic. The answer
  step is told today's date, to name the most recent event when everything listed is over, and to treat in-game years
  ("Festival 453", "457 AV") as not real dates.
- **Share it through a tunnel, safely.** `[web] access_code` (git-ignored local config) puts a sign-in page in front of
  everyone who arrives through a tunnel (Cloudflare, Tailscale, ngrok: recognised by the forwarding headers they
  add); the owner at the computer needs no login. With no code set, tunnelled requests are refused outright. Visitors
  get private chats (owned per browser), no Index panel (status, jobs, clear and stop are refused for them, whatever
  Host header they send), a trimmed version record, five wrong codes per ten minutes, a 6,000-character message cap,
  one answer at a time each and `max_concurrent` (2) at a time overall. README has the steps for a Cloudflare quick tunnel.
- **Fill from the mirror; deltas fetch only what changed.** A delta update used to fetch every page waiting in the
  queue as well as the ones it found changed, all from Elanthipedia. It now fetches only the pages it found changed
  (`crawl(titles=...)`), and the backlog is the job of the new **Fill** (`qqueryelanthia fill`, and a button on the
  Index page), which fetches missing or out-of-date pages from the mirror (Elanthipedia as fallback), then classifies,
  extracts item stats and indexes them. Full refresh also reads from the mirror. Re-index is local (it re-reads stored
  pages and contacts no wiki).
- **Bulk from the mirror, deltas from Elanthipedia.** `[wiki] delta_source` names the source the delta update
  asks what changed (the real wiki, never behind), while crawls and full refreshes read the fast mirror. A mirror
  full refresh compares edit times against when each page was fetched (the same on every copy of a wiki, unlike
  revision ids, so mixing sources cannot cause re-fetch loops or overwrite a newer page with an older one), never
  deletes a page just because a lagging mirror lacks it, and winds the delta cursor back to just before the mirror's
  newest edit so the next delta picks up everything the mirror had not yet seen. A single-source setup still uses
  the revision-id guard.
- **Mirror first, Elanthipedia as fallback.** `[wiki] fallback_source` names a second source: the active one (a
  fast mirror) gets one quick retry, and when it is unreachable, overloaded, or does not have a page yet (a lagging
  mirror would otherwise make every new page look deleted) the request goes to Elanthipedia, which keeps its own
  rate limit. A mirror that fails three times in a row is skipped for five minutes. Set per machine in the
  git-ignored `epedia.local.toml`; links shown to people always point at elanthipedia.play.net.
- **Clear index** (`qqueryelanthia clear-index`, or the Index page: typed confirmation, refused while a job runs)
  empties the search index (chunks, vectors, keyword and title indexes) and marks every page unindexed, keeping the
  downloaded pages, item stats and conversations. Rebuild with Re-index (`index --reembed-all`): local work, no
  wiki traffic. The panel says so when the index is empty.
- **Full refresh explains itself.** When pages are read from a different source than the index was built from, Start
  full is disabled with the reason (it compares revision numbers, which only match within one wiki), the command
  fails with one readable line instead of a traceback, and the empty-index hint says Re-index (not Full refresh) is
  how a cleared index is rebuilt.
- **Re-index in the Index panel.** A whole-index re-chunk (`index --reembed-all`) now reports its progress, shown
  as a "Re-index" row (pages done, percent, when it started, or "stopped early" if it died), whether it was started
  from the command line or the new **Re-index** button. The start buttons are disabled while one runs, and the
  server refuses to start a second job on top of it. **Stop** also ends a re-index that was started from a terminal
  (found by its command line, then its whole process tree is stopped); it is shown as stopped at once.
- **Index quality fixes (a re-index applies them to existing pages):** "Incomplete Item" / "Outdated Item"
  banners (boilerplate in about 40,000 item chunks) are stripped and noted in one line like article banners;
  headerless two-column tables no longer get an invented "Col 1 | Col 2" header (about 5,000 chunks);
  `Post:` pages whose whole article sat in one table cell (one chunk was 6,000 tokens, more than the embedder
  reads) are split into normal-sized chunks; `[[category:...]]` typed into page text no longer leaks into chunks.
- **A more human voice.** Fennick no longer says "the chronicle" or "the passages": he is told to talk like a
  friendly, well-read person (contractions, varied openings), to name Elanthipedia or the page only when it
  matters, and the program-written lines (greeting, "couldn't find anything on that", closing offers) were
  reworded to match.
- **Conversations that stay on the rails.** Every page load starts a fresh chat (reloading no longer leaves
  Fennick remembering a conversation the screen has lost); earlier chats are listed under **Past chats**, where
  they can be resumed or deleted, and **New chat** closes the current one (it stays on disk). A **Talking about**
  chip shows what Fennick thinks the chat is about, and its x sets that topic aside without clearing the screen.
  Behind it: the last turns always roll into the next answer (as with a person, what was just said is shared
  context; a different subject is answered on its own), the planner gets a list of earlier questions as well as
  the last turns, pronoun-led messages ("what about them?") count as follow-ups, a question it had to rewrite counts as a follow-up, pages already cited or
  offered are not offered again, no re-greeting mid-chat, and a message with no subject at all ("how much does it
  cost?") gets a clarifying question (never twice in a row). New chat or opening a past chat cancels a running
  answer cleanly, each tab is its own conversation, and two requests on one chat are served in order.
  `qqueryelanthia eval --conversations` scores 17 scripted turns (17/17 now).
- **"View details" on each sync line** lists the pages that run touched (new, edited, deleted, re-rendered by a
  template edit) with links to the wiki. Kept per run in the new `sync_pages` table (up to 5,000 pages per run);
  runs from before this feature show only their counts.
- **Desktop and Mobile layouts.** A full-window desktop layout (default) and a phone-width one, switched from
  the header and remembered; phones choose mobile automatically.
- **Live formatting** of the answer while it streams (headings, lists, tables, bold, code) and **source badges**:
  solid `[n]` for the model's citations, dashed `[n*]` for lines matched to a passage automatically, with hover
  tooltips; "Sources used" is folded and lists only what the answer cites.
- **UI polish.** Auto-growing input, follow-the-stream scrolling with "Jump to latest", Copy answer, Try again on
  errors, one answer at a time, reduced-motion handling.
- **Kind-aware retrieval.** The planner labels each question (item, crafting, place, character, lore, creature,
  mechanics, event/news, meta) and search favours matching kinds of wiki page; role-play logs and obsolete pages
  are demoted. "How do I forge weapons" now reaches the Forging guide instead of stray item pages.
- **Page classification and a link graph** (`classify`): 28 page kinds from namespaces, categories and title
  patterns, and ~305k links used to suggest one related page after an answer.
- **Greetings and questions about the assistant** are answered in character without searching.
- **Structured item questions** ("heaviest heavy edged weapon") run a live Semantic MediaWiki query, with a local
  fallback when the wiki is unreachable.
- **Follow-up questions** are rewritten to name their subject and answered from the previous answer's pages.
- **Wiki sources.** The original wiki is the default and primary source; extra mirrors are configured in a
  git-ignored `epedia.local.toml` and chosen per command with `--source`.
- **Command renamed to `qqueryelanthia`** (`epedia` still works as an alias).
- **Evaluation set** of 38 questions with kinds; `eval` reports hit@k, MRR and planner accuracy; `eval --answers`
  prints a voice checklist for reviewing answers by hand.
- `ARCHITECTURE.md`, `CLAUDE.md` and this file.

### Fixed

- **The "that said" note covers every page and every question.** It goes with any page an answer cites (not only one the
  question names): a character's earlier guild/race/gender/status, and for every other page, when a person changed it in
  the last 90 days, when and what they said ("That said, my notes on Tinkering products were last revised on 14 Oct 2026
  (added bellows, in the Tinkered Toys and Accessories section)..."). Written by code from stored edit metadata, no extra
  wiki requests. Old values of other infobox fields (spells, creatures...) would need old page text: not fetched yet.
- **Fennick volunteers what a trusted fact used to be.** Asked "What guild is Binu?" he says Necromancer, then adds in code
  "That said, my notes first had Binu's guild as Warrior Mage (2017)…", without doubting the current value (the page's
  creator set it). Only for the page the question names; doubtful facts still get their caution, as before.
- A page whose facts are all trusted (and that is not being fought over) no longer shows the model its edit history at
  all: given the margin, the 8B model remarked on it ("not contested in recent edits"). Doubtful facts still carry it.
- A fact the page's own creator set (a trusted one) now reaches the model as "state it plainly", without the list of
  older values; the model used to repeat them ("updated to Necromancer, a detail set by himself in 2025").
- The topic graph no longer counts forum posts, news, characters, meta pages, redirect stubs or disambiguation pages as
  members of a topic just because they link to its hub (Crafting: 448 members down to 427, none of them chatter).
- The Index panel's live "indexed" count included pages other stages had merely touched; it now counts pages this job
  fetched and indexed.
- The name resolver now picks the most specific of several equally long names ("hunt rats" means the Rat, not the Hunt
  command), understands a plural ("rats"), and treats an all-caps message as shouting, not as an acronym. It runs once per
  question and the avoid-pages set and topic family share its answer.
- Enrich keeps the facts it already has when an extractor suddenly reads fewer than half of them (a layout change on the
  wiki), logs a warning and retries next run, instead of wiping them.
- "Best common/strongest/light metal for…" wrongly opened with "I don't have that as a craftable material"; describing
  words (common, rare, strongest, light...) are no longer taken for a missing material's name.
- The topic graph rebuild took 66 seconds (one scan of every title per hub) and ran after any delta that classified a
  page; subpages are now matched in one pass (1.2 seconds, identical result).
- A long section's short closing paragraph could be silently dropped by the chunker and never searched.
- `EPEDIA_WIKI__RPS` (and per-source overrides) were ignored.
- `pipeline` skipped classification, leaving a fresh build without page kinds.
- `watch` leaked a log handler and a database connection on every loop.
- `eval --answers` polluted real chat history and was affected by it on later runs.
- Ctrl-C during a crawl waited out a whole batch and left its handler installed.
- Title lookups scanned the whole title table several times per question (now 1 to 3 ms).
- Stale redirect targets could break title matching after a page was deleted and recreated.
- The items tool could hang for two minutes when the wiki was unreachable, and compared numeric filters sent as
  text lexicographically.
- The User-Agent default would have sent a literal `{version}`.
- A closing "offer" written by the model sometimes named pages that did not exist; it is now added by code from
  real related pages (and never maps, scripts, logs or obsolete pages).
- The model sometimes appended "the chronicle has nothing on that" to a real answer (now softened in code).
- Source tagging never puts a badge on a line whose numbers differ from the passage's, and keeps citations written
  after a period with their sentence.
- CLI output no longer crashes on characters a Windows console cannot print (`show` on a page with IPA text).
- A message only counts as leaning on earlier context ("what about them?") when it is short; advice that merely
  starts like an offer ("If you want to start forging, ...") is no longer stripped from the end of an answer.
- `qqueryelanthia version` no longer creates a database or leaks a connection on a fresh checkout; the version
  badge only reports a git commit for this project's own checkout.
- "This article is <b>incomplete</b>" banners (markup inside the phrase) were skipped by the stripper and left in
  about 1,400 chunks; "Who is <name>?" could be filed as small talk and answered with "nothing on that".
- The page handles failed requests, malformed stream events and dropped connections instead of freezing.
- Index controls are local-only: they need the `X-QQE` header, a Host of this machine (blocks DNS rebinding), a
  matching Origin, and a loopback client if the server is bound beyond loopback. Two clicks cannot start two jobs,
  Stop ends the whole process tree, and a job that cannot start is reported as failed.

### Internals

- Pages are identified by title, so the original wiki and a mirror can feed the same database; whole-wiki
  operations refuse to run against a non-primary source, and `adopt` keeps already-fetched pages whose stored
  copy is provably current.
- Change detection uses `allrevisions` and `logevents` rather than recent changes.
- Maintenance banners are stripped at index time and noted in one line; "Redirect to:" stubs are dropped;
  creature infoboxes and one-column tables come out as `Key: value` lines.
- 211 tests; lint clean.

## 0.1.0: first commit (2026-10-22)

Crawl, index, search and cited answers over the whole wiki, a CLI and a local web chat, sync, and the first
evaluation set. Pushed as the initial commit of the repository.

_Last updated: 1.0.0 (2026-10-22)_
