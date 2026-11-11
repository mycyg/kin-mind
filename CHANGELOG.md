## 2026-11-11 — Model replay and background health recovery

- Chat fallback waits 30 seconds for the first real token, including reasoning and hidden relay reasoning; empty stream headers do not count. Explicit errors remain distinguishable from a first-token timeout.
- Claude requests project full dialogue and tool results without incompatible Responses replay state, including state returned by the same Claude relay. Stored history and native replay for other routes remain intact.
- Resident Core requests receive an execution deadline only when dispatched, avoiding queue-induced timeout cascades; uncertain or interrupted writes are never replayed automatically.
- Successful idle polls clear stale background failure streaks, while per-contact review outcomes reach heartbeat health instead of being discarded.

# Changelog


# Changelog


## 2026-11-11

- A shared timed concern now wakes one durable appraisal across recipient checks instead of creating a separate event per recipient.
- Appraisals validate operational state at commit and rebuild their judgment after shared wishes or concerns change. Direct subjective feelings remain freely editable.
- KIN compaction uses an isolated cumulative-summary task, with no executable conversation or tools, and rejects conversational, incomplete, empty or truncated output before replacing context.
- Added regression coverage for cross-recipient timer identity, concurrent stale proposals, retry behavior, checkpoint validation and retained history.

## 2026-11-10 — Validate and release the reviewed runtime

- Keep the channel integration checks aligned with per-conversation ordering while retaining exact message counts, uniqueness and attachment checks. Supply the optional service lookup in the review-health host fixture.
- Release validation: Core 1415 passed / 1 skipped, 10 Node checks, 113 integration checks across nine suites, 11 targeted checks, type checking and build passed. Four Core checks blocked by the local sandbox passed when rerun with loopback access; two integration fixtures were corrected and their suites rerun.
- An isolated snapshot of the running installation's actual memory, persona and model configuration passed native recall/chat checks and structured tool calls through all four configured model routes. Private data and credentials remain outside version control. No test messages were sent to contacts.
- Preserve the installation's existing provider definitions and runtime data; clean-export placeholder endpoints are not deployment configuration. A 32K context with 16K output and 16K compaction headroom still needs a smaller output reservation or a different chat model.

## 2026-11-10 — Fixes from an independent review

- A helper's approval answered in chat is the one the owner was told about, by its number: one already decided or stopped is never answered as another.
- A platform arrival is kept as it came until her mind has taken it in, and tried again each minute for a day: a failed intake no longer loses a message the platform will not send twice.
- Paragraphs sent as she wrote: a message written again takes back whatever of the old attempt had not gone, so nothing of it follows the final reply; only words a platform accepted are told to her as said; after a restart, a half that went out is told to her once.
- Taking back a wait someone asked for releases the relays held for it at the next minute: the wait is read when a relay might go, not stored on it.
- A held reply goes back to its conversation before its wish is settled, and stays held while the conversation cannot be reached.
- Messages held to be answered with the next one are kept per conversation.
- A resident Core process that exits answers only the requests asked of it; a timed-out request restarts only the process it was asked of.
- Recall receipts are completed: a block of memories is taken up with Core before it is shown and acknowledged once the session has kept it, so the same memory is no longer offered every turn.
- Frozen by the owner, her routines rest in DSH and resume on the thaw, and a turn of her own passes nothing on; a conversation still does.
- Her heartbeat tries a failed start-up recovery again (a minute later, then up to ten apart) and a failed day's review after fifteen minutes; a review that finished while she began replying starts no further round.
- Feishu: a connection closed while its address was being asked for is not opened afterwards; a cut or endless protobuf frame is refused instead of read for ever; each chat is taken in its own order, so one chat's download holds up no other.
- A voice request that timed out lets go of its worker at once: the next request starts its own and is answered only by it.
- Recall as she reads it: her standing state besides her mood is shown (the owner's habits with her, her traits, the owner's corrections), said once like her mood and again when it changes; an item of a shape not read here gives its words instead of nothing; a dream is marked as one; when Core could not fit everything related she is told this is not all of it.
- Deletion reaches the recall blocks already in her conversations: each block records the memories and sources it rests on, Core is asked as a turn starts which of them are gone, and her lanes leave such a block out of every request, summaries included.
- A finished compaction is told to Core at once and the next recall waits for it; a recall receipt is given again in log order when the turn settles, so one that failed is not lost.
- Her lanes report the output their routes reserve, and her presets set compaction's headroom to 16,384 tokens, so a 32K or 64K model can compact instead of being refused.
- Her page: the chat box keeps what was typed until a message is actually taken; a slower earlier request no longer overwrites a newer page; each form submits once at a time and clears only the draft it sent (reminders, marked days, diary replies, sealing a letter, memory corrections); a saved persona becomes the baseline to compare against; a reply already committed is not drawn again from its live rows while a tool runs.
- Edges of the fixes above: when the owner changes someone's account or a group's chat, the conversation held under the old one keeps its history and is a place to send nothing (a reply, a reminder, a relay, a contact), what waited to go there is cancelled and settled, and it is a place again once someone the directory knows speaks there; a burst her mind has is tried again each minute until her session takes it, and a burst or held reply her session took is given to her once however often keeping it on disk fails; her routines follow the freeze as it stands, one change at a time, so a thaw close behind a freeze leaves them running; closing the plugin ends the heartbeat's start retry and the conversations' retries; of words sent mid-writing that never became a message, only those a platform accepted join her memory as said, and an unsure one is told to her as unsure.
- A later pass of the same review: a summary DSH wrote of a conversation rests on what the recall blocks it replaced did (read from the session log), and one written before a deletion among them was learned of is no longer sent to a model, while one written afterwards is; what was learned deleted, and when, is kept in `erased.json`, and Core is asked before a step's compaction rather than after it. Her standing state counts as shown once the session has kept its block, so a turn stopped early shows it again. A closed Feishu or WeChat connection hands over and writes nothing more, and Feishu marks a message seen when it is handed over rather than when its download starts. A helper's ending that could not reach her memory or the conversation that asked stays owed on the work and is tried again each minute, for a day. A chat message turned down after newer words were typed comes back above them. An incomplete recall says so even when none of it was shown, and stopping no longer waits on a model's facts that never come.
- Verification: type checking, the plugin build and the three targeted regression scripts (11 checks: stray words and restart recovery joined the paragraph check, kept arrivals are new) passed. Each of the 43 reviewed cases was exercised on the real sources with synthetic platforms, processes and timers; the recall receipts against a real Core. Core is unchanged in this entry. The DSH end-to-end suites were not run.

## 2026-11-10 — Dated wakes, days and routines on her calendar, approvals in chat, paragraph sends, a wait when asked

- Core: a sealed letter or diary that opened, a concern whose time to come up again arrived, and a day that is an anniversary of a shared moment each wake her idle review once, only with their settings on; whether to say anything stays hers. Add the read-only resident action `anniversaries` for a range of days.
- Her calendar: a stretch of days can be marked by the owner on the page or by her with `kin_calendar_mark` (a trip, an exam week), lies across its days, and is told to her at the start of each turn while it lasts; `kin_routine` gives her repeating things of her own, run in her own conversation; anniversaries show on their dates; any item can be taken into the chat box.
- A helper's waiting approval can be answered by the owner in the conversation in his own words: she answers for him with `kin_work_answer`, only on a turn whose latest words are the owner's, one approval at a time, the one she was told about; DSH's prompt still works and whichever answers first withdraws the other.
- `kin_quiet`: someone who asks her to leave them be until a time hears nothing she starts or passes on until then; replies go as usual, and a reminder still starts its turn with that fact in view. The owner can take the wait back on her page.
- On WeChat and Feishu a reply goes out paragraph by paragraph as she writes it, still one delivery per message; a message written again after a broken attempt sends only what differs, and words that went out without becoming a message are told to her as said.
- Receipts and retries of what she already sent run every minute even while she is replying, each behind whatever waits to go in its conversation.
- Desktop chat box: voice input through her local transcription (`/api/kin/hear`), pictures open large, and the calendar can prefill a line.
- Verification: type checking, the plugin build and the three targeted regression scripts (10 checks, one new for paragraph sends) passed. Core: the 4 new tests passed; the one full run gave 1395 passed, 1 skipped and 20 failed, all 20 in compression and token-budget tests that need the token encoding in the test cache and fail the same on the unchanged source in the borrowed environment. The DSH end-to-end suites were not run for this entry.

## 2026-11-10 — Mid-turn messages, her calendar, chat attachments and helper teams

- Deliver what is said while Kin is busy at her next step instead of after the whole turn: channel bursts and the desktop chat box use DSH steering, as the old host did, and recall and intake follow such a step. The owner's 20-second burst wait is unchanged.
- Queue platform arrivals per chat, so one voice transcription no longer holds up other chats; run another person's review in a lane of its own, so receipts and retries do not wait behind it.
- Give a chat route that has a fallback 20 seconds to start answering; a route that just failed waits five minutes behind the others and stays shown as failed.
- Add a reply timeline to her health card: burst wait, wait for her turn, recall, first word and platform arrival of her latest replies.
- Add her calendar page and the `kin_calendar` tool over one gathering: reminders, held replies, quiet-hour deliveries, letters, timed plan steps, open plans, wishes, helper work and agreed things, and what past days held. A reminder the owner leaves on the page joins her memory as the owner's words; her reviews see the next two days of it.
- Let the desktop chat box send pictures and files, and name them in her memory.
- Tell her when a helper has waited for the owner's approval; brief her on handed-out work as each turn starts; add `kin_delegate` `team` over DSH Agent Teams. The `kin-crew` preset ships disabled until tried on a running DSH.
- For a turn nobody started (a reminder, word of her helpers), tell her when and where the person last spoke and whether her page is open; `kin_tell` may name the place (`via`), and no longer waits on its own turn when aimed at the conversation she is in.
- Stop model routes jittering while dragged; nudge her review after a diary reply; name platforms in Chinese in her context.
- Verification: type checking, the plugin build and the three targeted regression scripts (9 checks) passed. Core is unchanged. Core tests and the DSH end-to-end suites were not run for this entry.

## 2026-11-09 — Complete per-person review targets

- Give per-person reviews the same validated recipient routes and agendas as the main heartbeat, while retaining the original person and conversation.
- Allow a friend-origin review to create a wish or create/update a plan for another bound recipient; continue rejecting unbound targets.
- Keep one shared memory, original queue ownership, owner-only intimacy context and existing delivery controls unchanged.
- Add red/green real-host regressions for both review entry points, cross-recipient wishes and plans, refusal boundaries and follow-up ownership.
- Verification: 1411 Core tests (1 skipped), 10 Node checks, 113 integration checks across nine suites, typechecking and all three targeted regression scripts passed.

## 2026-11-09 — Preserve judgments and complete review evidence

- Let a concern reinterpret its existing valid evidence without requiring a new source; keep new-outcome and owner-participation checks, true no-op detection, and freely changing subjective concerns.
- Keep every source edge of a recalled aggregate through assessment and revalidation, while continuing to reject unseen or stale evidence.
- Repair only invalid structured fields and retain valid judgment text, values and actions. Keep technical follow-up reasons in diagnostics rather than the public thought or dimension reasons.
- Pass bound people and group agendas through the main heartbeat's supported host entry point without changing other-person review scope.
- Observe an uncertain delivery before waiting for an unrelated contact in the integration scenario, so a slow runner does not turn the receipt-confirmation case into a retry case. Keep the strict single-send assertion and separate bounded-retry coverage.
- Explain missing-evidence and exhausted-retry digest failures accurately; count uncharged foreground-priority deferrals as waiting rather than failed assessments.
- Add synthetic boundary, two-round isolation, real-host routing and audited reason-only recovery regressions. Recovery never rewrites historical events or restores outdated intentions.
- Verification: 1411 Core tests (1 skipped), 10 Node checks, 113 integration checks across nine suites, typechecking and all three targeted regression scripts passed.

## 2026-11-08 — Isolate contact receipt recovery

- Keep each delivery's result-recording failure separate, so one unavailable conversation does not block other proactive contacts.
- Count confirmed contacts once by delivery id and retain their original receipt time across retries and restarts; never resend accepted messages to repair their records.
- Expose pending result-recording errors in health checks and retain the actual session recovery error message.
- Defer background receipt notices until a native system head exists, preventing late-system-head logs that DSH cannot reopen.
- Retry persistence without appending another receipt after a failed flush, including retries beyond the recent-event window.
- Add synthetic regressions for failure isolation, restart-safe counting, concurrent settlement, accepted-send preservation, and strict session restoration.
- Verification: 1385 Core tests (1 skipped), 10 Node checks, 108 integration checks, typechecking and all three targeted regression scripts passed.

## 2026-11-07 — Relay thought-paragraph privacy fix

- Decode the relay’s `💭 ` thought paragraph before streaming, delivery, memory ingestion, or assistant-history replay, alongside the existing brain/razor envelope.
- Keep incomplete private paragraphs hidden and preserve ordinary inline emoji and tool blocks.
- Add synthetic regressions for every stream split, one-character chunks, unfinished envelopes, and reused block indices.

## 2026-11-05 — Kin on DSH 0.2.2-rc.1

- Move the active product to `core/` and the `kin/` DSH bundle; retire the prior standalone host/UI layout from the working tree (history remains in Git).
- Restore original persona and migration settings, source-aware recall, direct persona edits, ordered model fallback, and active-only plan context.
- Fix diary/dream text layout, pause unfocused animations and polling, retain worker result scope, and stop only the originating conversation’s workers.
- Adapt model selection and subagent composition to DSH 0.2.2-rc.1; refresh dependency lock and illustrated README.
- Wait for already-arrived messages to finish ingestion before flushing a burst; ignore superseded timers.
- Decode explicit relay thought/answer envelopes before streaming or remembering replies; clean older assistant excerpts on read.
- Check unknown receipts before up to three same-ID retries; notify only after retry exhaustion.
- Include queued output and pending platform receipts in health activity; retain unknown delivery IDs across restarts.
- Wait for silent group arrivals and their graph nodes before checking multi-person memory ownership.
- Restore the playful first-person README and illustrate the current DSH/plugin/Core architecture.
- Keep the cross-session fixture within the recent-message window on slower CI runners.
- Run pytest through Python so repository scripts resolve on clean CI runners.
- Verification: 106 integration checks, 1385 Core tests (1 skipped), and 10 Node checks passed.


## 0.1.106

- Share one operational notification receipt across inputs in the same failed native turn, including restored inputs and emoji messages. Keep actual reply settlement tied to explicit coverage or handoff evidence.
- Route operational notices privately to the owner with the original conversation name for context. Keep successful release reports and inactive runtime candidate status in maintenance logs; preserve failure and rollback notifications.

## 0.1.105

- Apply the owner-approved factual-correction contract to live understanding, concern content and topics, and unfinished wishes in the same assessment. Name the existing concern and wish `action` fields explicitly.
- Cover the effective assessment request, durable state, foreground and background projections, and the next contact candidate with a synthetic correction regression.

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
