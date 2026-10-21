import {deflateSync} from 'node:zlib';
export function png(size=256,value=90){const crc=b=>{let c=0xffffffff;for(const x of b){c^=x;for(let j=0;j<8;j++)c=(c>>>1)^((c&1)?0xedb88320:0);}return (c^0xffffffff)>>>0;};
  const chunk=(t,b)=>{const out=Buffer.alloc(b.length+12);out.writeUInt32BE(b.length);out.write(t,4);b.copy(out,8);out.writeUInt32BE(crc(out.subarray(4,-4)),out.length-4);return out;};
  const header=Buffer.alloc(13);header.writeUInt32BE(size);header.writeUInt32BE(size,4);header[8]=8;header[9]=6;
  const raw=Buffer.alloc((size*4+1)*size,value);for(let y=0;y<size;y++)raw[y*(size*4+1)]=0;
  return Buffer.concat([Buffer.from('89504e470d0a1a0a','hex'),chunk('IHDR',header),chunk('IDAT',deflateSync(raw)),chunk('IEND',Buffer.alloc(0))]);}
