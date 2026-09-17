/** Cut one frozen bubble into transport fragments. Fragments are contiguous
 * offset slices of the text (UTF-16 offsets, as `String.prototype.slice`
 * takes them), so joining them gives the text back exactly. A code fence, a
 * run of non-CJK characters (a word, a URL, a number) and a grapheme cluster
 * are atoms and are never cut. The same text and limit always give the same
 * fragments; the manifest stores them and nothing ever recomputes them. */
import {createHash} from 'node:crypto';
import {measureText} from './channel-contract.mjs';

const sha256=value=>createHash('sha256').update(value).digest('hex');
const segmenter=typeof Intl?.Segmenter==='function'?new Intl.Segmenter('und',{granularity:'grapheme'}):null;
const EXTENDS=/^[\p{M}\u200d\ufe0e\ufe0f\u{1f3fb}-\u{1f3ff}\u{e0020}-\u{e007f}]$/u;
function* graphemes(text,offset) {
  if(segmenter){for(const part of segmenter.segment(text))yield {start:offset+part.index,text:part.segment};return;}
  // Without ICU: keep marks, joiners and modifiers with their base character.
  let start=0,cluster='',joined=false;
  for(const point of text) {
    if(cluster&&!(joined||EXTENDS.test(point)||(cluster.endsWith('\r')&&point==='\n'))){yield {start:offset+start,text:cluster};start+=cluster.length;cluster='';}
    cluster+=point;joined=point==='\u200d';
  }
  if(cluster)yield {start:offset+start,text:cluster};
}

const CJK=/^[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}\p{Script=Bopomofo}\u2e80-\u2fdf\u3000-\u303f\u31c0-\u31ef\uff00-\uffef]/u;
const NO_START=/^[、。，．：；？！）］｝〉》」』】〕〗〙”’…‥ー～·!%),.:;?\]}]/u;   // never begins a fragment
const NO_END=/[（［｛〈《「『【〔〖〘“‘(\[{]$/u;                                  // never ends one
const CLOSERS=/[”’"'）)」』】》\]]+$/u;
const SENTENCE=/[。！？!?…．.]$/u,CLAUSE=/[，,、；;：:]$/u;
const FENCE_OPEN=/^[ \t]*(`{3,}|~{3,})(.*)$/,FENCE_CLOSE=/^[ \t]*(`{3,}|~{3,})[ \t]*$/;

/** Code fences as [start,end) ranges, by the CommonMark rule: a fence closes on
 * a bare line of the same character, at least as long as the one that opened
 * it; an unclosed fence runs to the end. A fenced block inside a longer fence
 * therefore stays one atom. */
function fences(text) {
  const ranges=[];let open=null,position=0;
  for(const line of text.split('\n')) {
    const body=line.endsWith('\r')?line.slice(0,-1):line,marker=body.match(open?FENCE_CLOSE:FENCE_OPEN);
    if(marker&&!open){if(marker[1][0]!=='`'||!marker[2].includes('`'))open={start:position,char:marker[1][0],length:marker[1].length};}
    else if(marker&&marker[1][0]===open.char&&marker[1].length>=open.length){ranges.push([open.start,position+line.length]);open=null;}
    position+=line.length+1;
  }
  if(open)ranges.push([open.start,text.length]);
  return ranges;
}

/** Atoms in order: `{start,contentEnd,end,type}`, type fence | word | cjk |
 * space. The whitespace after an atom belongs to it, so a cut never falls
 * inside or in front of a whitespace run; `space` is leading whitespace only. */
export function textAtoms(text) {
  const atoms=[];
  const prose=(from,to)=>{
    for(const part of graphemes(text.slice(from,to),from)) {
      const end=part.start+part.text.length,last=atoms.at(-1);
      if(/^\s+$/u.test(part.text)){if(last)last.end=end;else atoms.push({start:part.start,contentEnd:part.start,end,type:'space'});continue;}
      const type=CJK.test(part.text)?'cjk':'word';
      const adjacent=last&&last.end===last.contentEnd&&['word','cjk'].includes(last.type);
      if(adjacent&&((type==='word'&&last.type==='word')||NO_START.test(part.text)||NO_END.test(text.slice(last.start,last.contentEnd)))) {
        // The atom goes on as what it now ends with: "Python，" is closed for the
        // next word, while "（P" and "好.5" continue as a non-CJK run.
        last.end=last.contentEnd=end;last.type=type;
      } else atoms.push({start:part.start,contentEnd:end,end,type});
    }
  };
  let position=0;
  for(const [start,end] of fences(text)){prose(position,start);atoms.push({start,contentEnd:end,end,type:'fence'});position=end;}
  prose(position,text.length);
  return atoms;
}

/** How good a cut after `atom` is: 1 blank line, 2 newline, 3 sentence end,
 * 4 clause, 5 space, 6 any other atom boundary (a CJK grapheme). */
function cutClass(text,atom) {
  const gap=text.slice(atom.contentEnd,atom.end),newlines=gap.split('\n').length-1;
  if(newlines>1)return 1;
  if(newlines===1)return 2;
  if(atom.type==='word'||atom.type==='cjk') {
    const before=text.slice(atom.start,atom.contentEnd).replace(CLOSERS,'');
    if(SENTENCE.test(before)&&(gap||before.at(-1)!=='.'))return 3;
    if(CLAUSE.test(before))return 4;
  }
  return gap?5:6;
}

/** `{kind:'text',fragments:[{index,start,end,body_sha256}]}` or, when some atom
 * alone exceeds the limit, `{kind:'file',reason}`: the bubble is unsplittable. */
export function fragmentText(text,{limit,measure='utf16'}) {
  if(typeof text!=='string'||!Number.isSafeInteger(limit)||limit<1)throw Error('Fragmenting needs a text and a positive limit');
  const size=measureText(measure),slice=(start,end)=>({start,end,body_sha256:sha256(text.slice(start,end))});
  if(size(text)<=limit)return {kind:'text',fragments:text?[{index:0,...slice(0,text.length)}]:[]};
  const atoms=textAtoms(text),total=[0];
  for(const atom of atoms) {
    const cost=size(text.slice(atom.start,atom.end));
    if(cost>limit)return {kind:'file',reason:'atom-exceeds-limit'};
    total.push(total.at(-1)+cost);
  }
  const cuts=[];let from=0;
  while(from<atoms.length) {
    if(total[atoms.length]-total[from]<=limit){cuts.push([from,atoms.length]);break;}
    let low=from+1,high=atoms.length;          // the farthest cut that still fits
    while(low<high){const middle=(low+high+1)>>1;if(total[middle]-total[from]<=limit)low=middle;else high=middle-1;}
    // Among cuts that do not cost an extra fragment, take the best class, and
    // within it the latest one: when two fragments can hold the rest, any cut
    // that leaves a second one that fits; otherwise any that leaves this one
    // at least half full. Failing that, the farthest cut.
    const rest=total[atoms.length]-total[from],least=rest<=2*limit?rest-limit:limit/2;
    let best=null;
    for(let index=low;index>from;index--) {
      if(best&&total[index]-total[from]<least)break;
      const rank=cutClass(text,atoms[index-1]);
      if(!best||rank<best.rank)best={index,rank};
      if(rank===1)break;
    }
    cuts.push([from,best.index]);from=best.index;
  }
  const fragments=cuts.map(([a,b],index)=>({index,...slice(atoms[a].start,atoms[b-1].end)}));
  // A fragment of nothing but whitespace cannot be sent as a message.
  if(fragments.some(f=>!text.slice(f.start,f.end).trim()))return {kind:'file',reason:'blank-fragment'};
  return {kind:'text',fragments};
}

/** The complete bubble as one file fragment, named from its content alone. */
export function fileFragment(text) {
  const body=sha256(text);
  return {index:0,start:0,end:text.length,body_sha256:body,kind:'file',name:'kin-reply-'+body.slice(0,16)+(fences(text).length?'.md':'.txt')};
}

/** Null when the fragments are exactly the text, in order, within the limit; otherwise a static reason. */
export function verifyFragments(text,fragments,{limit,measure='utf16'}={}) {
  if(!Array.isArray(fragments)||!fragments.length)return 'no-fragments';
  const size=limit?measureText(measure):null;let position=0;
  for(const [index,fragment] of fragments.entries()) {
    if(fragment.index!==index||fragment.start!==position||!(fragment.end>fragment.start))return 'fragments-not-contiguous';
    const body=text.slice(fragment.start,fragment.end);
    if(sha256(body)!==fragment.body_sha256)return 'fragment-hash-mismatch';
    if(size&&fragment.kind!=='file'&&size(body)>limit)return 'fragment-exceeds-limit';
    position=fragment.end;
  }
  return position===text.length?null:'fragments-do-not-cover-text';
}
