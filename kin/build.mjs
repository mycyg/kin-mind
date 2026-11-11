// Build both halves of dsh-kin into lib/.
import { build } from 'esbuild'
import { rmSync } from 'node:fs'

// Modules the DSH page shares with every client plugin (its seed module table).
const SHARED = [
  'react', 'react/jsx-runtime', 'react-dom', 'react-dom/client', '@deepseek-ai/cordis', 'zod',
  '@tanstack/react-virtual', '@deepseek-ai/dsh-client-store', '@deepseek-ai/dsh-client-ui-slots',
  '@deepseek-ai/dsh-client-ui-primitives', '@deepseek-ai/dsh-client-ui-dockkit',
]

rmSync('lib', { recursive: true, force: true })

// Host half: ESM for Node. Packages stay external so DSH resolves its own copies (peers).
await build({
  entryPoints: ['src/index.ts', 'src/preset.ts', 'src/compaction.ts'],
  outdir: 'lib',
  bundle: true,
  splitting: true,
  format: 'esm',
  platform: 'node',
  target: 'node24',
  packages: 'external',
  sourcemap: true,
  logLevel: 'warning',
})

// Browser half: DSH's lazy-CJS factory; shared modules come from the page's module loader.
await build({
  entryPoints: ['src/client/index.tsx'],
  outfile: 'lib/client.js',
  bundle: true,
  format: 'cjs',
  platform: 'browser',
  target: 'es2022',
  jsx: 'automatic',
  external: SHARED,
  loader: { '.css': 'text' },
  banner: { js: 'window.__ModuleLoader__.load({id:"dsh-kin",factory:(require)=>{var module={exports:{}},exports=module.exports;' },
  footer: { js: 'return module.exports}});' },
  logLevel: 'warning',
})

console.log('dsh-kin built → lib/')
