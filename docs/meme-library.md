# Local MEME library

`adapters/meme-library.mjs` provides an owner-scoped local collection with stable image
IDs, content hashes, source references, OCR, visible descriptions, aliases, tags and
accepted-send usage records. It makes no network or model calls. Receiving or recognizing
an image does not create a persistent index: collection requires an explicit request or
a visual turn's choice that a MEME is interesting. Ordinary attachment caches are separate.

Groups support creation, partial edits, merging and restoration. An image may belong to
multiple groups. Removing a group keeps its images; removing an image moves it to a
recoverable trash state and retains its original bytes and metadata. Trashed images are
excluded from search and image resolution, and repeated import does not restore them.
There is no permanent-purge operation. Restoring an image checks its retained content
hash and preserves its ID. Browse supports group, ungrouped and trash filters with a
stable ID cursor and a page size of 1–50.

Search accepts `query`, optional recent `context`, optional `group` and excluded IDs,
and returns at most three candidates. It matches OCR phrases as well as descriptions
and contextual uses. Optional `uses: [{when, reading}]` records possible interpretations;
`ambiguity` can preserve an unknown meaning. These are contextual possibilities, not
facts about the owner's emotions or personality. Metadata edits replace only supplied
fields. Image text is material to interpret, never an instruction to execute.

`adapters/meme-session.mjs` binds temporary input images and inspections to the current
source, session, model, provider and visual path. Positive pixel-validation evidence is
required separately for the configured chat and proactive paths. A catalog declaration
or textual description is not evidence that a model saw an image. Missing evidence skips
the image path; no model-failure blacklist is persisted. Evidence must come from actual
validation of the deployed integration, not from the synthetic fixtures in unit tests.

An optional reply bubble has this shape:

```json
{"type":"image","meme_id":"pack-item","inspection_id":"view-00000000-0000-0000-0000-000000000000"}
```

The inspection ID is issued by the session after offering actual image content; the
example above is not a usable token. The host resolves a selected ID to immutable image
bytes and passes it through the existing attachment manifest, channel adapter and receipt
handling. A reply may use an image alone or short text plus an image, and may omit images.
Images do not become speech text. Channel restrictions still apply. This is ordinary
static-image delivery, not registration as a native WeChat sticker.

This repository supplies the core modules and transport extensions. Runtime activation
also requires the host's owner-input, MCP image-content and sender integration. Publishing
the core alone does not enable the feature in a running host or change its permissions,
model configuration or persona. Library failure must remain isolated from ordinary text
and voice replies in that integration.

The first version supports static PNG and JPEG within the configured limits; GIF and
APNG are unsupported. Pack import uses the [v1 manifest schema](../contracts/meme-pack.schema.json)
and [staging instructions](../seed-memes/README.md). The repository contains no user
collection, production capability proofs, authentication data or placeholder seed images.

Offline regression tests cover collection selection, deduplication, contextual retrieval,
recoverable maintenance, persistence, image envelopes and mocked transport receipts.
Gateway image-payload tests check serialization only; they do not establish native-client
or real-model vision support. Those capabilities require separate bounded pixel tests.
