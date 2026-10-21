# Original starter packs (no placeholder images)

The directory intentionally contains no fake/placeholder seed pack. Generate and visually
review 5 original packs of 8 images each. Do not copy the user's reference screenshot.
Each pack contains `manifest.json` and eight individual static PNG files. Recommended
512×512 RGBA, sRGB, 8 bit, non-interlaced; no APNG. For a 4×2 contact sheet, generate
2048×1024 and slice precisely into eight 512×512 cells, without shared borders or labels.
Keep text away from slice edges and visually verify each exported tile and OCR.

Manifest format is `kin-meme-pack/v1`, with `id`, `name`, `original:true`, `source` (authorship
and rights/provenance), optional `aliases`, and `items`. Each item has a stable `id`, relative
`file`, actual file `sha256`, `description`, `ocr`, `tags`, `contexts`, `emotions`, `aliases`.
`description` and nonempty `contexts` are required. No remote URL, parent path, symlink
escape, missing image, false hash, animated/corrupt image or unindexed entry is accepted.
Image minimum 256×256, square; recommended 512, at most 5 MiB and 16 Mpixels.

Run `node scripts/import-meme-pack.mjs --manifest /staging/pack/manifest.json --library
/staging/private-memes` for read-only validation. Import first into an isolated staged
library with the same command plus `--apply`, inspect cross-pack and per-pack search,
then include only real approved packs in the reviewed deployment manifest. Production
import/activation is a separate authorized release step. The import never sends images,
calls a model, changes personality, downloads a URL or installs a service.
