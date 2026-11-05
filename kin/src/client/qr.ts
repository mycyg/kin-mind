/**
 * A QR code for the WeChat login link: byte mode, error correction level M, the smallest version that fits.
 * Follows the reference algorithm (Project Nayuki's QR Code generator), cut down to what one link needs, so
 * the page carries no QR library. Returns the modules row by row, true for dark.
 */

// Error-correction codewords per block and number of blocks at level M, by version (index 0 unused).
const ECC = [-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28]
const BLOCKS = [-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49]

const MASKS: ((x: number, y: number) => boolean)[] = [
  (x, y) => (x + y) % 2 === 0,
  (_, y) => y % 2 === 0,
  (x) => x % 3 === 0,
  (x, y) => (x + y) % 3 === 0,
  (x, y) => (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0,
  (x, y) => ((x * y) % 2) + ((x * y) % 3) === 0,
  (x, y) => (((x * y) % 2) + ((x * y) % 3)) % 2 === 0,
  (x, y) => (((x + y) % 2) + ((x * y) % 3)) % 2 === 0,
]

function rawModules(version: number) {
  let n = (16 * version + 128) * version + 64
  if (version >= 2) {
    const align = Math.floor(version / 7) + 2
    n -= (25 * align - 10) * align - 55
    if (version >= 7) n -= 36
  }
  return n
}

const dataCodewords = (version: number) => Math.floor(rawModules(version) / 8) - ECC[version] * BLOCKS[version]

/** GF(2^8) multiplication modulo x^8 + x^4 + x^3 + x^2 + 1. */
function multiply(x: number, y: number) {
  let z = 0
  for (let i = 7; i >= 0; i--) {
    z = (z << 1) ^ ((z >>> 7) * 0x11d)
    z ^= ((y >>> i) & 1) * x
  }
  return z
}

function reedSolomon(data: number[], degree: number) {
  const divisor = new Array<number>(degree).fill(0)
  divisor[degree - 1] = 1
  for (let i = 0, root = 1; i < degree; i++, root = multiply(root, 2)) {
    for (let j = 0; j < degree; j++) divisor[j] = multiply(divisor[j], root) ^ (j + 1 < degree ? divisor[j + 1] : 0)
  }
  const remainder = new Array<number>(degree).fill(0)
  for (const byte of data) {
    const factor = byte ^ remainder.shift()!
    remainder.push(0)
    divisor.forEach((coefficient, i) => { remainder[i] ^= multiply(coefficient, factor) })
  }
  return remainder
}

export function qr(text: string): boolean[][] {
  const data = new TextEncoder().encode(text)
  let version = 1
  while (4 + (version < 10 ? 8 : 16) + data.length * 8 > dataCodewords(version) * 8) {
    if (++version > 40) throw new Error('too long for a QR code')
  }

  // Data codewords: mode, length, bytes, terminator, padding.
  const bits: number[] = []
  const put = (value: number, length: number) => { for (let i = length - 1; i >= 0; i--) bits.push((value >>> i) & 1) }
  put(4, 4)
  put(data.length, version < 10 ? 8 : 16)
  for (const byte of data) put(byte, 8)
  const capacity = dataCodewords(version) * 8
  put(0, Math.min(4, capacity - bits.length))
  put(0, (8 - (bits.length % 8)) % 8)
  for (let pad = 0xec; bits.length < capacity; pad ^= 0xec ^ 0x11) put(pad, 8)
  const codewords = Array.from({ length: bits.length / 8 }, (_, i) => bits.slice(i * 8, i * 8 + 8).reduce((byte, bit) => (byte << 1) | bit, 0))

  // Split into blocks, add error correction, interleave.
  const blocks = BLOCKS[version], eccLength = ECC[version], raw = Math.floor(rawModules(version) / 8)
  const short = blocks - (raw % blocks), shortLength = Math.floor(raw / blocks)
  const parts: number[][] = []
  for (let i = 0, k = 0; i < blocks; i++) {
    const length = shortLength - eccLength + (i < short ? 0 : 1)
    const block = codewords.slice(k, k + length)
    k += length
    const ecc = reedSolomon(block, eccLength)
    if (i < short) block.push(0)
    parts.push([...block, ...ecc])
  }
  const all: number[] = []
  for (let i = 0; i < parts[0].length; i++) parts.forEach((block, j) => { if (i !== shortLength - eccLength || j >= short) all.push(block[i]) })

  // Function patterns.
  const size = version * 4 + 17
  const modules = Array.from({ length: size }, () => new Array<boolean>(size).fill(false))
  const fixed = Array.from({ length: size }, () => new Array<boolean>(size).fill(false))
  const set = (x: number, y: number, dark: boolean) => { modules[y][x] = dark; fixed[y][x] = true }
  for (let i = 0; i < size; i++) {
    set(6, i, i % 2 === 0)
    set(i, 6, i % 2 === 0)
  }
  for (const [cx, cy] of [[3, 3], [size - 4, 3], [3, size - 4]]) {
    for (let dy = -4; dy <= 4; dy++) for (let dx = -4; dx <= 4; dx++) {
      const x = cx + dx, y = cy + dy, distance = Math.max(Math.abs(dx), Math.abs(dy))
      if (x >= 0 && x < size && y >= 0 && y < size) set(x, y, distance !== 2 && distance !== 4)
    }
  }
  const align: number[] = []
  if (version > 1) {
    const count = Math.floor(version / 7) + 2
    const step = version === 32 ? 26 : Math.ceil((version * 4 + 4) / (count * 2 - 2)) * 2
    align.push(6)
    for (let position = size - 7; align.length < count; position -= step) align.splice(1, 0, position)
  }
  align.forEach((ax, i) => align.forEach((ay, j) => {
    const last = align.length - 1
    if ((i === 0 && j === 0) || (i === 0 && j === last) || (i === last && j === 0)) return
    for (let dy = -2; dy <= 2; dy++) for (let dx = -2; dx <= 2; dx++) set(ax + dx, ay + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1)
  }))
  const format = (mask: number) => {
    let remainder = mask // level M is 00
    for (let i = 0; i < 10; i++) remainder = (remainder << 1) ^ ((remainder >>> 9) * 0x537)
    const word = ((mask << 10) | remainder) ^ 0x5412
    const bit = (i: number) => ((word >>> i) & 1) === 1
    for (let i = 0; i <= 5; i++) set(8, i, bit(i))
    set(8, 7, bit(6))
    set(8, 8, bit(7))
    set(7, 8, bit(8))
    for (let i = 9; i < 15; i++) set(14 - i, 8, bit(i))
    for (let i = 0; i < 8; i++) set(size - 1 - i, 8, bit(i))
    for (let i = 8; i < 15; i++) set(8, size - 15 + i, bit(i))
    set(8, size - 8, true)
  }
  format(0)
  if (version >= 7) {
    let remainder = version
    for (let i = 0; i < 12; i++) remainder = (remainder << 1) ^ ((remainder >>> 11) * 0x1f25)
    const word = (version << 12) | remainder
    for (let i = 0; i < 18; i++) {
      const dark = ((word >>> i) & 1) === 1, a = size - 11 + (i % 3), b = Math.floor(i / 3)
      set(a, b, dark)
      set(b, a, dark)
    }
  }

  // Data in the zigzag, two columns at a time from the right.
  let i = 0
  for (let right = size - 1; right >= 1; right -= 2) {
    if (right === 6) right = 5
    for (let vertical = 0; vertical < size; vertical++) {
      for (let j = 0; j < 2; j++) {
        const x = right - j, y = ((right + 1) & 2) === 0 ? size - 1 - vertical : vertical
        if (!fixed[y][x] && i < all.length * 8) {
          modules[y][x] = ((all[i >>> 3] >>> (7 - (i & 7))) & 1) === 1
          i++
        }
      }
    }
  }

  // The mask that leaves the fewest look-alike patterns.
  const flip = (mask: number) => {
    for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) if (!fixed[y][x] && MASKS[mask](x, y)) modules[y][x] = !modules[y][x]
  }
  const penalty = () => {
    let score = 0, dark = 0
    const lines = [...modules.map((row) => row.map(Number).join('')), ...modules.map((_, x) => modules.map((row) => Number(row[x])).join(''))]
    for (const line of lines) {
      for (const run of line.match(/0{5,}|1{5,}/g) ?? []) score += run.length - 2
      score += (line.match(/(?=10111010000|00001011101)/g) ?? []).length * 40
    }
    for (let y = 0; y < size - 1; y++) for (let x = 0; x < size - 1; x++) {
      const c = modules[y][x]
      if (c === modules[y][x + 1] && c === modules[y + 1][x] && c === modules[y + 1][x + 1]) score += 3
    }
    for (const row of modules) for (const module of row) if (module) dark++
    return score + (Math.ceil(Math.abs(dark * 20 - size * size * 10) / (size * size)) - 1) * 10
  }
  let best = 0
  let lowest = Infinity
  for (let mask = 0; mask < 8; mask++) {
    flip(mask)
    format(mask)
    const score = penalty()
    if (score < lowest) [best, lowest] = [mask, score]
    flip(mask)
  }
  flip(best)
  format(best)
  return modules
}
