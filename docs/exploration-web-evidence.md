# Web reads and delivered evidence

The exploration executor uses `kin_web.web_search` to discover candidates and
`kin_web.read_page` to read public HTML, text or JSON. Search results are not
article evidence. Computer/browser tool readiness is a separate capability and
does not prove that a page was opened successfully.

For environments with synthetic DNS responses, the host can explicitly enable
the [destination-bound HTTP transport](public-http-transport.md). It validates
each redirect before requesting the next destination. It does not change system
DNS, proxy settings or browser permissions. Browser navigation retains its own
address restrictions; enabling HTTP transport does not relax those restrictions.

## Read contract

`read_page(url, offset=0, limit=16000, evidence_id=None, expected_version=None)`
returns a real text page, its character range and hash. The first request freezes
the full extracted text in the execution's private storage. Continuation uses
the returned evidence ID, version and next offset; it reads that same snapshot
rather than refetching a potentially changed URL. Invalid versions, missing
content, tampering and cross-execution reads fail explicitly.

The decoded HTTP entity is streamed with a 512 KiB limit. HTTP errors, empty
content and unsupported media types are failed reads. A successful HTTP response
only proves that content was obtained: DeepSeek must distinguish articles from
login, consent, challenge or error pages using the actual text. Empty search
results include a bounded response excerpt for this judgment.

| Field | Meaning |
| --- | --- |
| `version`, `content_sha256` | SHA-256 of the full extracted UTF-8 text |
| `raw_body_sha256`, `raw_body_bytes` | Hash and size of the decoded HTTP entity, not compressed wire bytes |
| `body_bytes_representation` | `decoded-http-entity` |
| `text`, `delivered_range` | Text returned by this tool call, with start/end character offsets and SHA-256 |
| `delivered_ranges` | Deduplicated ranges returned across calls; not proof of model comprehension |
| `next_offset`, `continuation` | How to request more of the same content version |
| `semantic_classification` | `model-required`; transport does not classify factual support |

The ledger retains an excerpt for compact memory storage. It does not represent
that excerpt as the full page. Tool responses keep their preview inside the
current delivered range. Duplicate continuation calls do not accumulate new
ranges, and each source has a bounded range list.

## Citation and history

New `kin-source-receipt-v2` seals include the content version, delivered ranges,
page hashes and the requirement for semantic interpretation. These fields remain
bound when a source becomes historical or is carried into a later attempt.
Existing v1 receipts retain their original digests and historical meaning.

Only successful host observations or supplied, verified historical material are
citable. A failed request, search hit or model-written URL is not promoted into a
read receipt. Model findings map to exact evidence IDs or locators. The host
verifies provenance; DeepSeek judges whether the delivered text supports each
claim and reports gaps when it does not.

MCP completion records retain status and redacted error codes without copying
tool arguments, page bodies or credentials into diagnostic summaries. Provider
usage, isolated verification and naturally scheduled exploration are separate
receipts; none substitutes for another.
