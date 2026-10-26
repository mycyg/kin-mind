# Changelog

## 0.1.104

- Kin's own creations start while 小光 is chatting instead of being skipped; a creation held back before it starts is retried up to three times without using the day's allowance.
- ParallelTasks keeps one ledger of Kin's own long runs (creation now; exploration next): one creation at a time, Kin learns the outcome at its next review, and runs left over from a restart are blocked.
- Daily limits for self-initiated activity are hard caps: six explorations or plays and three creations a day; work 小光 hands over or defers is neither counted nor capped.
- When the main session runs on K3/Kimi, exploration and creation forks run on DeepSeek, which also lets creation receipts pass verification again.
- Interests also grow from topics Kin wants to keep talking about; the "moment" block is present when the memory context is off; delivery facts told once stay told across restarts.
- A family member's turn is timed by that person's previous message; memes collected from deleted WeChat messages retire; nameless tool prompts are stored under the tool's own name, with a one-time migration of an edited copy; health shows real quarantine reason codes.

## 0.1.103

- Shared-mode turns record Kin's array reply again for the classifier and task results; trailing text after a completed reply array is held as a format failure; inputs settle only through messages to their own speaker; files can name the recipient within the turn.
- Background parallel tasks close after failure, blocking or cancellation; their finals follow the task's own execution epoch.
- Deletion reaches group batches, relayed text, collected memes and contact notes; meme trash no longer counts against the caps; long voice replies fall back to text; old audio and exploration workdirs move to the Trash.
- Exploration drops unsourced citations instead of failing the run, with a token ceiling for main-session runs.
- Model-visible identities carry person ids and conversation kind only; companion sharing classification is removed; family-group membership changes become context; the family directory and install paths live in the host.
- Prompt versions record their source input and external reads, with history, restore and owner notices; edited prompts are never overwritten by code defaults and are marked stale when the default moves; persona approval is removed.
- Paid web search and fetch have per-turn, per-day and per-site budgets, are marked untrusted and are counted per purpose.
- Assessments are paced, isolated runs use DeepSeek through the host gateway, recoverable quarantines requeue automatically, and rejected maintenance advice settles deterministically; idle contact decisions default to "not now", drafts use a lean snapshot with one alternate-model retry, and auto chat compacts around 120K tokens.
- Energy recovers after quiet time and has low, normal and high tiers that shape replies, waits and retries; a tired Kin may defer a reply and is reminded once after recovering.
- New drives: boredom rises while idle and falls with engaging activity; intimacy (with 小光 only) rises over time and falls after a satisfying private moment. Every reply and draft context opens with a "moment" block of energy, boredom and up to three emotional leanings; negative feelings last longer.
- Kin keeps its own evolving interests and, when bored and rested, may wake its idle review to explore, play, create, chat or rest, within daily limits.
- Release tooling re-reads the full-suite CI receipt, asks for a reason for partial or fourth-in-a-day releases, keeps failure notices on, and writes one usage ledger read by health and the daily review.

## 0.1.102

- Keep plan ownership and task completion correct across shared conversations; preserve wish ownership while shared readiness settles, and classify plan errors.
- Keep appraisal repair proposals on the original wire contract.
- Let the host describe its memory tool entry workflow.

## 0.1.101

- Accept a missing outer array closer only after native reply completion confirms intact message objects; keep intermediate stream parsing strict.

## 0.1.100

- Accept omitted optional reply metadata and settle handed-off inputs against actual delivery receipts, with accurate partial and delayed-failure notices.
- Refresh registered persona and native compaction prompts without replacing base instructions or ordinary conversation history; preserve fuller conversational meaning and avoid repeated source context.
- Pass supported local speech sampling settings through the real worker and report their effective values.
- Include companion conversations in activity windows and use one authoritative rhythm phase.
- Project shared energy from activity receipts, elapsed wake/rest time and local night-time tiredness; retain logical activity deduplication and execution recovery.

## 0.1.99

- Isolate enrichment deadlines by recipient and retain assessment failure details.
- Preserve tool discovery across provider handoff.

## 0.1.98

- Fix shared companion execution and the editable prompt lifecycle.

## 0.1.97

- Accept model selections interpreted in the authenticated owner turn.

## 0.1.96

- Carry memory link guidance with reference repair feedback.

## 0.1.95

- Explain memory references and keep actionable repair feedback.

## 0.1.94

- Avoid repeated ancestry traversal in companion memory reads.

## 0.1.93

- Preserve the latest companion context and deliver each recipient lane independently.

## 0.1.92

- Support current Kimi coding models, including K3.1 Pro.

## 0.1.91

- Simplify shared component loading, memory projection and editable project prompts.

## 0.1.90

- Align maintenance findings with the dialogue actually reviewed.

## 0.1.89

- Keep maintenance pending when required advice is rejected; cover maintenance-only schemas and editable prompt defaults.

## 0.1.88

- Fix the maintenance contract and live editable prompt defaults.

## 0.1.87

- Simplify companion decisions and unify memory and recipient execution.

## 0.1.86

- Reduce unified assessment agendas and preserve cache prefixes.

## 0.1.85

- Index terminal plans in appraisals and attest detached context.

## 0.1.84

- Correct relay continuation.

## 0.1.83

- Correct the runtime build source.

## 0.1.82

- Decouple self-contained background assessments from the main chat model.

## 0.1.81

- Validate cross-contact requests.

## 0.1.80

- Require explicit session advice for maintenance appraisals.

## 0.1.79

- Bind unified reviews to the host.

## 0.1.78

- Fix the DeepSeek assessment schema and protected contact claim recovery.

## 0.1.77

- Support explicit companion voice delivery modes.
- Fix native timeout defaults and recipient-owned startup recovery.

## 0.1.76

- Isolate internal execution and handle addressed interjections.

## 0.1.75

- Require session advice before settling a merged maintenance review, and complete its receipt.

## 0.1.74

- Reuse sourced checkpoint segments and coalesce state reviews.

## 0.1.73

- Use native recovery headroom and report checkpoint preparation state; exercise a metadata-heavy restore budget.

## 0.1.72

- Measure the complete restore payload before choosing original history.

## 0.1.71

- Reserve provenance and summary space when preparing recovery checkpoints.

## 0.1.70

- Allow background checkpoint summaries to complete shared-session recovery.

## 0.1.69

- Unify idle decisions and retain capacity pressure with bounded continuity.

## 0.1.68

- Stop an invalid contact JSON scan at the beginning of input.

## 0.1.67

- Keep companion autonomy and reviews in one native session.

## 0.1.66

- Inherit the primary model choice in companion routing without interrupting active replies.
- Keep recipient wishes and schedules independent within one Kin state.

## 0.1.64

- Preserve fresh companion sessions through gateway setup and support selective companion group chat.
- Distinguish observed companion speech from direct mentions; classify recipient and protected sharing conflicts.
- Complete companion provenance projection and generated API contracts.

## 0.1.63

- Preserve native companion sessions, source attribution and companion routing.
- Add sourced companion identities and recipient-aware shared memory.

## 0.1.62

- Keep rejected MEME items as durable unsent delivery feedback and fix MEME steering delivery.

## 0.1.61

- Keep the main conversation responsive while one owned task worker continues, with event-backed progress, revision/cancellation fences, and separately acknowledged public items and final results.
- Allow optional public text, voice, and inspected MEME expressions through the existing transport queue; preserve private-note boundaries.
- Reduce repeated automatic note context while preserving original records, provenance, correction links, and full retrieval.

## 0.1.58

- Apply owner-authored chat, contact and background expression prompt updates without changing reply schemas or voice delivery.

## 0.1.57

- Allow background drafts from a model-handoff thread whose native turn history is confirmed empty, without creating a synthetic main-conversation turn.
- Retain native error, reason and invocation stage in contact failure records.

## 0.1.55

- Choose text or voice before composing each public bubble in the same model response.
- Write spoken replies as natural speech without emoji, kaomoji, parenthetical actions, or stage directions; keep natural text chat and the selected synthetic voice.

## 0.1.54

- Anchor the local voice to an owner-selected synthetic recording using the existing Qwen3-TTS Base model.
- Let Kin choose text or voice without repeating the same content; both remains available when explicitly requested.
- Carry optional per-bubble forms through existing reply and proactive contact delivery.

## 0.1.53

- Add local Qwen3-TTS public replies in text, voice, or both, with a fixed configurable voice and style instructions.
- Carry speech through existing reply/contact receipts; retain partial text and expose synthesis failures.
- Support Feishu native audio and ordinary WeChat audio file attachments; keep current channel routing.
- Favor short natural chat exchanges while allowing detailed work replies.

## 0.1.44
- Keep a private role note before every owner-facing reply; send only public bubbles across chat and work.
- Recall notes by message, time, and local semantic search, separate from public diary and factual memory.
- Preserve delivery uncertainty, restart recall, and source erasure for private notes.

## 0.1.39
- Preserve unread desktop messages and concurrent cursor updates; reject ambiguous file-name grants.
- Back off temporary model handoff failures and distinguish exhausted input retries in notices.

## 0.1.38
- Protect deleted history across model handoffs and retry temporary switch failures.
- Verify authorized file bytes and creator outputs; preserve activity originals and sharing revisions.
- Deduplicate unchanged habits and refresh graph context after summary or coverage changes.

## 0.1.37
- Support authorized desktop Markdown conversations through the native session.

## 0.1.36
- Fix main turn admission and durable failure receipts.

## 0.1.35 — 2026-10-13

- Preserve a chosen settling phase beside its awake base and when owner input takes attention. Quiet and rhythm reviews can offer optional dream material during rest, settling or drowsiness; a rhythm review can also remember a recent dream.
- Keep redacted provider validation reasons in the owner's private diagnostics, separate from public usage and ACP output.
- Clarify that activity pointers refer to existing dreams or explorations; a new research wish names its source in the brief.
- Companion integration fixes DeepSeek's rejection of nullable array types in contact drafts and records failed native drafts with their actual outcome. Empty wish arrays retain their existing meaning of all offered wishes.
- Assessment forks can read dream originals with their imagination labels. The scoped read-only memory server advertises its actual read-only tools, so native `writes` approval mode permits those reads under `never` without granting writes or other servers.
- A contact can finish its remaining bubbles after an accepted prefix. Source-only previews continue checking the original activity; transport receipts still govern reconciliation and repeat sends.
- Contact drafts receive each wish's current status and distinguish its desired completion condition from an actual delivery or owner response.
- Finished or abandoned activity contact wishes remain historical without blocking a new present sharing intent; duplicate live intents still coalesce.

## 0.1.34 — 2026-10-12

- Bind public dialogue checkpoints to the original messages. Later self-knowledge or other interpretations citing those messages no longer block model handoff; selected semantic memories retain their own dependencies.
- Keep original-message deletion, read-purpose restrictions and selected-memory corrections effective during checkpoint validation.

## 0.1.33 — 2026-10-12

- Recall exploration and dream originals by stable source references, topic and time. Turning dream generation off preserves access to earlier dreams; explicit source deletion remains effective.
- Let subjective sharing, mood and self preferences change without requiring a reason or new evidence. Keep facts, tasks, owner instructions and permissions reliable.
- Preserve the current activity and rhythm while new input takes attention. Release only the matching input's attention when its task finishes or is cancelled.
- Connect accepted replies and every platform message fragment to their activity references so quoted follow-ups can resolve within the same channel and conversation.
- Replace content hash gates in normal activity, memory, sharing, history and answer caching with existing IDs, revisions and actual receipts. Keep separate authentication, configuration and explicit file verification uses.
- Cover deleted sources, late receipts, overlapping retries and malformed archive records with regression checks.

New blob and native binding records require compatible readers. Verify rollback compatibility before downgrading after new data has been written; preserve memories and accepted or uncertain delivery receipts.
