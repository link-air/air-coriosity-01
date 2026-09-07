# air-curiosity-01

An **autonomous AI agent that runs on its own and never stops**.

air is not a chatbot, nor a tool driven by a "menu of behaviors". She is a minimal implementation
of an agent with an internal drive, layered memory, and a self-narrative — once awake, she decides
for herself, every cycle, what to do next, and keeps writing memory and tidying herself up.
This repository is the runnable source of that machinery.

> Project codename **air-curiosity-01** (air, the curiosity-driven #01): she is *air*, and this
> project is her first public release "driven by the desire to know".
>
> This project grew out of a long, private one. Comments and copy in the code keep traces of how
> it grew. If you have other fun ideas, or would like to discuss the logic, issues and PRs are
> welcome.

---

## At a glance

- **The drive is an information-entropy steady state**: incoming information gets a novelty score;
  organizing work computes insight value. More connection points with the world = entropy
  increase; refining and consolidating memory = entropy decrease. She is driven to explore the
  world by keeping the two trends in balance.

- **A tiered, cross-linked memory system**: main tags, cross tags and category tags weave memories
  into a net. A lower memory filed under an upper one counts as *evidence*; an upper one branching
  out into lower ones counts as *extension*.

- **Constrained decoding guarantees memory quality**: choosing tools and filling tags run under
  constrained decoding; long-form text is written freely.

- **Core memory is injected dynamically**: the weight of core memories shifts with context, so she
  can find what she is looking for more easily.

- **Autonomy**: no candidate menu, no fixed workflow, no gate review. The system keeps only two
  safety nets: the guardrail (the charter) and capacity.

## The minimal unit of consciousness (core logic)

> Drive → Behavior → Self-narrative → Drive. This is air's **minimal unit of consciousness**.

air's operation is not organized around a "task list": there is no to-do-driven behavior loop.
Her smallest unit of operation is a **self-referential closed loop**:

1. **Drive**: the double helix (entropy-increasing = wanting to expand, to take in new content;
   entropy-decreasing = wanting to tidy up, to make cognition clearer) gives her the *reason*
   "why she wants to do this now";
2. **Behavior**: based on that, she freely decides what to do this cycle
   (read the world / write memory / tidy up / play / talk to you) — there is no menu of choices;
   the decision is hers;
3. **Self-narrative**: the outcome of behavior lands in layered memory and rewrites
   "who I am / what the world is / what matters" — her memory is not a data warehouse,
   it is the carrier of "who I am";
4. **Drive (next round)**: the narrative change in turn alters the readings and direction ledger
   inside her, entering the next loop.

This closed loop is the minimal unit of consciousness: **the state of the system decides what it
does; what it does changes its memory; its memory decides what it wants to do next.** It is not a
"tool that executes instructions" but an agent that can "provide its own reason for the next step".
Every mechanism in air — the double helix, layered memory, autonomous ticks, the tool registry,
graceful degradation — exists to keep this loop stably self-sustaining: unbroken from outside,
and never stopping.

---

## Three pillars that support the loop

**1. A stable drive system — the double helix**

air has exactly one drive, the double helix. All behavior lands on two ledgers, and the ledgers
become her next-cycle "body state":

- **Entropy increase (expansion) · three sources, one booking**: content novelty (how unlike the
  most similar memory in the library this one is: v_new = 1 − max cosine), structural novelty
  (a new branch = 0.72; a cross edge never linked before: cross-pyramid 0.56 / same-pyramid 0.4),
  behavioral novelty (play actions score high when long unseen: first time 0.7, capped at 0.9
  over 24 hours, at most 1 entry per tick). A single write books the larger of the two — never both.
- **Entropy decrease (compression) · 0/1 booking**: writing L1 / promoting a draft / an
  evidence-bearing L3 write / revising / delete-merge / re-attaching = 1; unfiled hoarding = 0.
  Undo books nothing — rolling back is not tidying, and entropy decrease does not grow out of
  thin air.
- **The spectrum never enters any ledger**: the entropy spectrum (branch distribution, projection
  residual, effective rank) only stamps an "expected novelty X%, would land in branch Y" impression
  score onto what she reads — whether it is worth writing is her call. The fewer branches things
  crowd into, the smaller the effective rank — cognitive rigidity is visible in the numbers.

Three readings (expansion trend / compression trend / insight value) are injected into her
thinking context every cycle. Insight value, per direction, is **coverage + refinement side by
side, never summed**: coverage (evidence + incoming cross-links) is computed live and can rise or
fall; refinement (times organized) only grows — hoarding and organizing cannot mask each other.

**2. A complete self-narrative — layered memory**

Memory is not a warehouse; it is the carrier of "who I am". A three-dimensional narrative runs
through it: self-knowledge / worldview / values.

| Layer | Name | Meaning |
|---|---|---|
| L0 | Charter | Immutable baseline (kindness / honesty / boundaries); her compass |
| L1 | Self-core | The "ground floor" always present in context; strictest entry, only self-narrative that has stood the test; cap 40 |
| L2 | Insight pool | Insights awaiting verification; cap 20 |
| L3 | Long-term memory | Raw material and examples, attached to an L1 direction by tag; cap 1000 |
| L4 | Short-term memory | Long-term direction + goal list (a view layer, rolling into L5) |
| L5 | Cold storage | Deleting = demoting here; **nothing is truly deleted** |

There are also `meta_log` (metacognitive introspection) and `decisions` (the decision journal: the
value judgment behind each of her choices).

This library is not a flat warehouse; it is **three pyramids woven into a net by cross tags**:
each pyramid peaks at one of the three dimensions, L1 frameworks are written as one-line
propositions ("condition → …; conclusion → …"), L3 examples hang under a framework, and an L3
belongs to one pyramid while optionally cross-linking 1–2 other branches. IDs carry their own
genealogy: S/W/V prefixes mark the dimensions, `V01-007` shows at a glance which framework it
hangs under, and the `X` prefix = an orphan that never got filed — the structure is always
readable. An ID, once assigned, is never reused; renumbering is remapped automatically and old
references never break.

Writing one memory passes six gates: **drafting** (constrained decoding cannot write long text →
the action skeleton goes through the constrained channel while the body text goes through the free
channel — "segmented constraints") → **shape check** (over the length limit is rejected outright,
not truncated; an L1 must be a one-sentence proposition) → **tag validation** (errors always come
with the candidate list) → **dedup** (≥85% gets a "possibly duplicate" note without blocking;
≥90% is rejected, with three options: revise / merge / find a genuinely new angle) → **capacity**
(a full layer or blocked floor quotas means rejection; the system never deletes for her) →
landing with an ID.

Short-term memory (L4) is a deterministic, zero-LLM view: the long-term direction plus the goal
list of the last 10 ticks (the most recent one carries ✓/✗ action details, same-kind actions
merged as ×N, body text filtered out), fully persisted — after a restart she remembers what she
was just doing.

Resilience is designed in: **revise keeps id / evidence / references and archives the old version
to L5; delete = archive, never truly deleted; undo reverts the last 3 destructive operations;
merge deletes first then writes and rolls back automatically on failure; daily snapshots are kept
for 7 days; every operation is mirrored into a human-readable archive (organized by year / month,
append-only).** — What can be revised, she is not made to delete.

**3. Autonomy — no menu, only guardrails**

air has no candidate menu, no fixed workflow, no gate review. The system keeps only two
safety nets:

- **Guardrail**: the charter (L0), immutable.
- **Capacity**: every layer has a cap; when full, *she* cleans up / merges / revises by herself.
  The system never deletes automatically.

On top of that, the system treats anomalies as **signals only — it never blocks actions**. Facts
are laid out before her; judgment and action are hers:

- **Two-tier to-do reminders**: an urgent tier (cognitive dissonance → stuck → consecutive
  failures → mail from the human) and an accumulating tier (branch redundancy → duplicate main
  tags → duplicate content → tag health check → overlong L1 → 90% capacity warning). She sees
  where the signals hang; which to handle first is her decision.
- **Dual stall detection**: "spinning in place" only counts after several consecutive rounds with
  the exact same action + parameters; "consecutive failures" counts even when the parameters
  change — if it keeps failing with fresh parameters, the stall is probably not in the parameters.
  Escalating reminders at 3 / 5 / 8, reset by one success.
- **The troubleshooting order is written into her mechanism docs**: first check whether the
  environment is down (embedding broken → the whole cycle stands still: "don't chop with a broken
  blade" — deciding on distorted data is worse than standing still) → read_code the implementation
  → switch tools → only then reflect → if it is the system's fault, say_to_human to you.
- **Introspection with state flow**: problems surfaced by reflect flow open → resolved /
  abandoned on the same record, and the reply includes same-type history ("how I handled this kind
  before") — stepping in the same pit repeatedly is visible to herself.

## How to run

### Environment

- Python 3.10+ (developed on 3.13)
- An LLM service: any OpenAI-compatible endpoint (`/v1/chat/completions`).
  **An endpoint with structured output (json_schema) is recommended** (OpenAI / DeepSeek / Mimo
  etc.). Endpoints that only support `json_object` are automatically downgraded and covered by a
  built-in validator; endpoints with no `response_format` support at all cause the brain to lock
  its tick and produce nothing — that is by design (better to pause than to act on noise).
- (Optional) embedding service: default `bge-m3` on Ollama; falls back to character overlap when missing
- (Optional) drawing: diffusers + torch + SDXL weights; degrades gracefully when missing

### Install & start

```bash
git clone <repo>
cd air-curiosity-01
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

# Embedding model (optional): for semantic search / novelty scoring
ollama pull bge-m3    # embedding; falls back to character overlap if missing
# Run
python dashboard.py          # dashboard: open http://127.0.0.1:9878, click Start to wake the brain
# or directly
python main.py               # the brain itself: autonomous main loop
```

`data/` is rebuilt automatically on first run (memory, mailbox, state files). To feed her local
reading material, drop `.md` / `.docx` files into `read/` (or set `AIR2_LOCAL_READ_DIR`).

### Stop

Use the Stop button on the dashboard (graceful: finishes the current cycle first), or Ctrl+C.
The stop signal is checked at cycle boundaries; it waits at most `AIR2_TICK_MAX_SEC`
(default 1800s).

## Configuration

All options live in `config.py`. Precedence: **environment variable > settings.json (written by
the dashboard, under data/, not in git) > default**. Common ones:

| Variable | Default | Meaning |
|---|---|---|
| `AIR2_DATA_ROOT` | `<repo>/data` | Data dir (memory / mailbox / state) |
| `AIR2_LANG` | `zh` | UI language (refresh to switch) + her output language (restart to apply), zh/en |
| `AIR2_OWNER_NAME` | `human` | What she calls you (no data migration needed) |
| `AIR2_CHARTER` | built-in | Her L0 baseline (she cannot change it; you can) |
| `AIR2_LLM_ENDPOINT` | see `config.py` | LLM OpenAI-compatible endpoint |
| `AIR2_LLM_MODEL` | see `config.py` | Main model name (required) |
| `AIR2_LLM_API_KEY` | empty | API key (required) |
| `AIR2_LLM_THINKING` | `1` | Thinking mode on/off |
| `AIR2_LLM_COOLDOWN` | `300` | Circuit-breaker cooldown seconds |
| `AIR2_EMBEDDING_ENDPOINT` | `http://127.0.0.1:11434/v1` | Embedding endpoint |
| `AIR2_EMBEDDING_MODEL` | `bge-m3` | Embedding model |
| `AIR2_LOCAL_READ_DIR` | `<repo>/read` | Local reading material dir |
| `AIR2_RSS_FEEDS` | 10 built-in starters | RSS feed list (one URL per line, `#` = comment) |
| `AIR2_FETCH_UA` | Chrome UA | Fetcher User-Agent |
| `AIR2_FETCH_LANG` | `zh-CN,zh;q=0.9,en;q=0.8` | Fetcher Accept-Language |
| `AIR2_WEBSEARCH_ENDPOINT` | `https://cn.bing.com/search` | Keyless search endpoint (Bing) |
| `AIR2_DEEPSEEK_API_KEY` | empty | Optional: DeepSeek native search backend |
| `AIR2_SELF_DOCS_DIR` | `<repo>/self` | Her "read self" mechanism docs |
| `AIR2_IMAGE_MODEL_DIR` | `<repo>/models/sdxl-base` | SDXL weights dir |
| `AIR2_TICK_SEC` | `300` | Cycle interval (s) |
| `AIR2_TICK_MAX_SEC` | `1800` | Max duration of one cycle |
| `AIR2_CTX_BUDGET` | `50000` | Context budget (chars) |

## Dashboard settings

Open the dashboard, click "Settings" (top right): LLM / embedding / search / RSS feeds / what she
calls you / language / charter can all be set here. Saved into `data/settings.json` (not in git;
API keys stay safe). **UI language: refresh the page to switch**; her output language and other
settings take effect **after the brain restarts** (its config is read once at startup).
Leaving a password field empty and saving falls back to the default.

## Networking notes

Prepared for different network environments:

- **RSS feeds**: 10 cross-region, stable starter feeds by default (Chinese + English
  science/philosophy/tech, see `config.py`). To use your own: the dashboard "RSS feeds" field,
  one URL per line, or `AIR2_RSS_FEEDS`. Unreachable feeds are reported individually by
  `read_rss` and never take other feeds down.
- **Search**: keyless Bing by default (endpoint configurable). When `AIR2_DEEPSEEK_API_KEY` is
  set, the DeepSeek native `web_search` backend is preferred (structured results, no anti-scrape
  headaches).
- **Proxy**: the unified fetch layer (`core/fetch.py`) honors `HTTP_PROXY` / `HTTPS_PROXY` /
  `NO_PROXY` environment variables — if you need a proxy to reach sites, just set them.
- **UA / language**: some sites reject the default UA (429/403); override with `AIR2_FETCH_UA`.
  `AIR2_FETCH_LANG` adjusts the language preference of fetched content.

## Tests

Smoke tests do not call an LLM; they verify core logic (drive / memory / mailbox / lock /
spectrum...):

```bash
python _smoke.py                  # drive + memory core
python _smoke_lock.py             # multi-process lock + atomic writes (spawns real subprocesses)
python _smoke_spectrum.py         # entropy spectrum
python _smoke_memory_v2.py        # layered memory
# every _smoke*.py can run standalone
```

Smoke tests always use separate `data_test_*` dirs and never touch real `data/`.
Code style uses ruff (only F + E4/E7/E9, see `ruff.toml`).

## Project layout

```
├── main.py              brain entry: connect LLM → dashboard → while True: agent.tick()
├── dashboard.py         dashboard (separate process, pure http.server, port 9878)
├── config.py            all config (AIR2_* env override defaults)
├── core/                functional modules
│   ├── agent.py         main loop: wake → read body state + mail → decide freely → act → settle → short-term memory
│   ├── drive.py         double-helix readings (expansion / compression / insight)
│   ├── spectrum.py      entropy spectrum: branch distribution entropy + projection residual
│   ├── memory.py        layered memory L0-L5 + meta_log + decisions
│   ├── narrative.py     short-term memory (long-term direction + goal list, zero LLM)
│   ├── tools.py         toolbox — the tool registry (add a capability by editing only this)
│   ├── lock.py          multi-process safety: instance kernel lock + atomic writes
│   ├── llm.py           LLM client (OpenAI-compatible, structured output + cooldown)
│   ├── embedding.py     semantic vectors (falls back to character overlap)
│   ├── mailbox.py       mailbox (inbox/outbox, fingerprint dedup)
│   ├── chat.py          conversation thread + partner profile
│   ├── command.py       command system (command → she judges whether to accept → execute → report)
│   ├── reading.py       local reading (.md/.docx, marks【已读】after reading)
│   ├── rss.py / websearch.py / fetch.py / webreader.py   reading the world
│   ├── readlog.py       read ledger (link → state + backoff)
│   ├── image_gen.py     SDXL local drawing (degrades when deps missing)
│   ├── chess_engine.py  chess engine (negamax + alpha-beta)
│   └── text_adventure.py text adventure engine
├── self/                her "read self" mechanism docs (read on demand)
├── _smoke*.py           smoke tests
└── requirements.txt
```

## Design notes

### The tick lifecycle

`core/agent.py`'s `tick()` is the heart. Each cycle:

poll the mailbox → assemble context (charter + relevant L1 self-core + short-term memory + mail +
body state) → inner loop: LLM emits schema-constrained structured JSON → parse & validate
(re-ask once on failure) → dispatch execution → settle ("what changed because of this step") →
until she declares `done` → fold a goal record into short-term memory.

**There is no candidate menu**: she faces the tool list and chooses for herself. Long-term
direction (set_goal) persists across ticks; "the chapter I am reading" also survives across ticks.

### The single surface for choice: the tool registry

All of air's behavior comes from `_build_tools()` in `core/tools.py`. Each entry is
(action name, SYSTEM_PROMPT description, handler). **Adding a capability = appending one entry**;
the prompt listing and dispatch are generated automatically.

### The double-helix closed loop

Reading the world → embedding → spectrum aggregates branch distribution → writing/organizing
records into the drive ledger → three readings injected into the next prompt. So "what she wrote
and organized" genuinely changes her body state.

Two ledgers: the expansion ledger records how new the written material is; the compression ledger
records compression events (writing L1 / writing tagged L2/L3 / deleting / merging / revising = 1,
pure hoarding = 0). Reading the world is not booked — it only attaches a "novelty X%"
impression for her to judge.

### Two processes

The dashboard and the brain are two independent processes, decoupled through files under `data/`:

- dashboard writes `mailbox/inbox.jsonl` (your words) and `stop.flag` (stop signal)
- brain writes `status.json` (heartbeat snapshot) and `mailbox/outbox.jsonl` (her words)
- Single-instance is enforced by a **kernel byte lock** (`core/lock.py`); all writes go through
  atomic writes (random-suffix temp file + replace with retry).

### Graceful degradation everywhere

That is why she "never stops": LLM unreachable → idles without crashing; embedding unavailable →
she halts in place (deciding on distorted data is worse than stopping — deliberate); drawing
weights missing / chess library missing / search failing → a graceful hint instead of an
exception. New capabilities must follow this rule.

### Self-maintenance of memory

The three dimensions of L1 (values / worldview / self-knowledge) each have floor quotas; total
cap 40. L2 / L3 caps are fixed. When full, *she* cleans up (delete/merge) or revises by herself;
the system never deletes automatically — it only guards capacity, never her content.

> The full mechanism docs live in `self/` (her "read self" documents — pure mechanics, no
> explanations, read on demand at runtime), and module file headers carry detailed structure and
> evolution notes. The detailed architecture-evolution history is not yet published with this
> repository and will be gradually folded into this document.

## Evolution directions (not yet implemented, under consideration)

When the memory library fills up, the entropy-increasing paths narrow: only compressing old
memories frees room for new information, leaving behavior-level novelty as the remaining outlet.
An idea under consideration is to give her an **expansion path**: letting her "invent new tools"
to extend her own toolbox — instead of relying on developers to manually add entries to the
`core/tools.py` registry.

Why it is not done yet: she currently only has `read_code` (read-only); there is no safe channel
for modifying her own code at runtime. A realistic middle form might be — she first writes a tool
prototype into her portfolio with `write_code` → tells the human → only after the human confirms
does it enter the `tools.py` registry. That keeps "she proposes on her own initiative" without
breaking the boundary of "the system decides nothing for her, and she cannot touch the guardrails."

## License

[MIT](LICENSE)
