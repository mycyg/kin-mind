import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {fragmentText,fileFragment,verifyFragments,textAtoms} from './text-fragments.mjs';
import {measureText,CHANNEL_CONTRACTS} from './channel-contract.mjs';

const bodies=(text,options)=>{const plan=fragmentText(text,options);assert.equal(plan.kind,'text');return plan.fragments.map(f=>text.slice(f.start,f.end));};

test('a text within the limit is one fragment, and fragments are exact offset slices',()=>{
  const text='一句完整的话。';
  assert.deepEqual(fragmentText(text,{limit:4000}).fragments,[{index:0,start:0,end:text.length,body_sha256:createHash('sha256').update(text).digest('hex')}]);
  assert.deepEqual(fragmentText('',{limit:10}).fragments,[]);
  assert.throws(()=>fragmentText('text',{limit:0}),/positive limit/);
});

test('cuts prefer a blank line, then a newline, a sentence end, a clause, a space, and only then a CJK grapheme',()=>{
  assert.deepEqual(bodies('para one line\n\npara two\nline three here',{limit:26}),['para one line\n\n','para two\nline three here'],'blank line over newline');
  assert.deepEqual(bodies('line one here\nSecond. Third part goes on',{limit:26}),['line one here\n','Second. Third part goes on'],'newline over sentence end');
  assert.deepEqual(bodies('这是第一句话。这是第二句，后面还有内容',{limit:14}),['这是第一句话。','这是第二句，后面还有内容'],'sentence end over clause');
  assert.deepEqual(bodies('alpha beta, gamma delta epsilon',{limit:20}),['alpha beta, ','gamma delta epsilon'],'clause over space');
  assert.deepEqual(bodies('一二三四五六 七八九十一二三四',{limit:10}),['一二三四五六 ','七八九十一二三四'],'space over a bare CJK boundary');
  assert.deepEqual(bodies('一二三四五六七八九十',{limit:4}),['一二三四','五六七八','九十']);
  assert.deepEqual(bodies('alpha beta gamma delta epsilon',{limit:12}),['alpha beta ','gamma delta ','epsilon']);
  // Fewer fragments come first: a better cut that would cost an extra fragment loses.
  assert.deepEqual(bodies('first paragraph here\n\nsecond one\nthird line. And more words',{limit:34}),['first paragraph here\n\nsecond one\n','third line. And more words']);
  assert.deepEqual(bodies('这是第一句话。这是第二句，后面还有更多的内容',{limit:14}),['这是第一句话。这是第二句，','后面还有更多的内容'],'and no one-character tail when a clause cut also gives two');
  assert.deepEqual(bodies('短。\n\n后面是一整段没有空行的文字，一直写到超过上限为止',{limit:16}),['短。\n\n后面是一整段没有空行的文','字，一直写到超过上限为止']);
});

test('a code fence, a URL, a non-CJK word and a grapheme cluster are never cut',()=>{
  const fence='```js\nconst answer = 42;\nconsole.log(answer);\n```',url='https://example.com/path/to/page?query=value&other=1#anchor';
  const family='👩‍👩‍👧‍👦',flag='🇨🇳',accent='e\u0301';
  const text=`先看代码：\n${fence}\n再看链接 ${url} 和单词 internationalization，最后是${family}${flag}${accent}收尾。`;
  for(const limit of [60,64,80,120]) {
    const parts=bodies(text,{limit});
    assert.equal(parts.join(''),text);
    for(const atom of [fence,url,'internationalization',family,flag,accent])assert.equal(parts.filter(p=>p.includes(atom)).length,1,`limit ${limit} cut ${JSON.stringify(atom)}`);
    assert.ok(parts.every(p=>p.length<=limit));
  }
  assert.deepEqual(textAtoms('Python，Java（x）。').map(a=>'Python，Java（x）。'.slice(a.start,a.end)),['Python，','Java','（x）。'],'closing marks stay with what they close, opening marks with what they open');
  assert.ok(!bodies('数到十，一二三四五六七八九十',{limit:4}).some(p=>/^[，。]/u.test(p)),'no fragment begins with a closing mark');
  assert.deepEqual(textAtoms('约.5元（P级）好)x').map(a=>'约.5元（P级）好)x'.slice(a.start,a.end)),['约.5','元','（P','级）','好)x'],'a non-CJK run that begins with punctuation stays whole');
});

test('an atom larger than the limit makes the bubble unsplittable: one file fragment with a name from its content',()=>{
  const block='```\n'+'x = 1\n'.repeat(900)+'```',text='说明如下：\n\n'+block+'\n\n以上。';
  assert.ok(block.length>4000);
  assert.deepEqual(fragmentText(text,CHANNEL_CONTRACTS.wechat.text),{kind:'file',reason:'atom-exceeds-limit'});
  assert.equal(fragmentText(text,CHANNEL_CONTRACTS.feishu.text).kind,'text','the same bubble fits a Feishu message');
  assert.equal(fragmentText('https://example.com/'+'a'.repeat(5000),{limit:4000}).kind,'file');
  const file=fileFragment(text),digest=createHash('sha256').update(text).digest('hex');
  assert.deepEqual(file,{index:0,start:0,end:text.length,body_sha256:digest,kind:'file',name:'kin-reply-'+digest.slice(0,16)+'.md'});
  assert.deepEqual(fileFragment(text),file,'deterministic');assert.ok(fileFragment('plain '+'z'.repeat(5000)).name.endsWith('.txt'));
  assert.equal(verifyFragments(text,[file]),null);
  assert.equal(fragmentText(' '.repeat(30)+'word',{limit:20}).kind,'file','a fragment of nothing but whitespace is never produced');
});

test('limits count what the platform counts: UTF-8 bytes, or the escaped size inside a Feishu request',()=>{
  const text='一二三四五六七八九十'.repeat(3);
  assert.deepEqual(bodies(text,{limit:12,measure:'utf8'}).map(p=>Buffer.byteLength(p)),[12,12,12,12,12,12,12,6]);
  const quoted='"a" "b" "c" "d" "e" "f"',size=measureText('json2-utf8'),parts=bodies(quoted,{limit:30,measure:'json2-utf8'});
  assert.ok(size(quoted)>30&&quoted.length<30);assert.ok(parts.length>1&&parts.every(p=>size(p)<=30));assert.equal(parts.join(''),quoted);
});

test('verification names what is wrong with a stored fragment list',()=>{
  const text='alpha beta gamma delta epsilon',good=fragmentText(text,{limit:12}).fragments;
  assert.equal(verifyFragments(text,good,{limit:12}),null);
  assert.equal(verifyFragments(text,good,{limit:8}),'fragment-exceeds-limit');
  assert.equal(verifyFragments(text,good.slice(0,2)),'fragments-do-not-cover-text');
  assert.equal(verifyFragments(text,[good[0],{...good[1],start:good[1].start+1}]),'fragments-not-contiguous');
  assert.equal(verifyFragments(text+'!',good),'fragments-do-not-cover-text');
  assert.equal(verifyFragments(text.replace('alpha','ALPHA'),good),'fragment-hash-mismatch');
  assert.equal(verifyFragments(text,[]),'no-fragments');
});

// Deterministic pseudo-random inputs: the same seed always builds the same texts.
function random(seed){return()=>{seed|=0;seed=seed+0x6D2B79F5|0;let t=Math.imul(seed^seed>>>15,1|seed);t=t+Math.imul(t^t>>>7,61|t)^t;return((t^t>>>14)>>>0)/4294967296;};}
const PIECES=[
  ()=>'今天的天气其实还不错',()=>'我们慢慢来',()=>'好的',()=>'，',()=>'。',()=>'！',()=>'？',()=>'、',()=>'：',()=>'（补充一下）',()=>'「引用的话」',
  ()=>' ',()=>' ',()=>'\n',()=>'\n\n',()=>'  \n',()=>'word',()=>'another-word',()=>'snake_case_name',()=>'3.14159',()=>'e.g.',()=>'Sentence ends here.',()=>'clause, then more;',
  ()=>'https://example.com/a/b?x=1&y=2#frag',()=>'http://example.org/路径'.slice(0,19),()=>'www.example.net/page',
  ()=>'```\ncode line one\n  indented 行\n```',()=>'~~~text\n~~~',()=>'😀',()=>'👩‍👩‍👧‍👦',()=>'🇨🇳',()=>'👍🏽',()=>'e\u0301',()=>'한국어',()=>'ひらがなカタカナ',()=>'"quoted \\ text"',
];
const URL_RANGE=/(?:https?:\/\/|www\.)[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+/g;
// An independent reading of the fence rule: opened by a line of three or more
// backticks or tildes, closed by a bare line of at least as many of the same.
function fenceRanges(text) {
  const ranges=[];let from=null,fence=null,offset=0;
  for(const line of text.split('\n')) {
    const run=(line.trim().match(/^(`+|~+)/)??[''])[0],rest=line.trim().slice(run.length);
    if(from===null&&run.length>=3&&!(run[0]==='`'&&rest.includes('`'))){from=offset;fence=run;}
    else if(from!==null&&run.length>=fence.length&&run[0]===fence[0]&&!rest){ranges.push([from,offset+line.length]);from=null;}
    offset+=line.length+1;
  }
  if(from!==null)ranges.push([from,text.length]);
  return ranges;
}
const graphemeStarts=text=>new Set([...new Intl.Segmenter('und',{granularity:'grapheme'}).segment(text)].map(s=>s.index));
const NON_CJK_WORD=/[\p{L}\p{N}_]/u,CJK_CHAR=/[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u;

test('property: random texts are never cut inside a word, a fence, a URL or a grapheme, and the slices rebuild the text',()=>{
  const next=random(20260917);let fragmented=0,files=0;
  for(let round=0;round<400;round++) {
    const text=Array.from({length:3+Math.floor(next()*40)},()=>PIECES[Math.floor(next()*PIECES.length)]()).join('').trim();
    if(!text)continue;
    const measure=['utf16','utf8','json2-utf8'][round%3],limit=24+Math.floor(next()*180),size=measureText(measure),plan=fragmentText(text,{limit,measure});
    assert.deepEqual(fragmentText(text,{limit,measure}),plan,'deterministic');
    if(plan.kind==='file') {
      files++;
      const blank=plan.reason==='blank-fragment',oversize=textAtoms(text).some(a=>size(text.slice(a.start,a.end))>limit);
      assert.ok(blank||oversize,`round ${round}: a file needs an atom over the limit`);continue;
    }
    const parts=plan.fragments.map(f=>text.slice(f.start,f.end));
    assert.equal(parts.join(''),text,`round ${round}: slices rebuild the text`);
    assert.equal(verifyFragments(text,plan.fragments,{limit,measure}),null);
    assert.ok(parts.every(p=>size(p)<=limit&&p.trim()),`round ${round}: every fragment fits and says something`);
    if(parts.length>1)fragmented++;
    const starts=graphemeStarts(text),protectedRanges=[...fenceRanges(text),...[...text.matchAll(URL_RANGE)].map(m=>[m.index,m.index+m[0].length])];
    for(const {start} of plan.fragments.slice(1)) {
      assert.ok(starts.has(start),`round ${round}: cut inside a grapheme at ${start}`);
      assert.ok(!protectedRanges.some(([a,b])=>start>a&&start<b),`round ${round}: cut inside a fence or URL at ${start}`);
      const before=String.fromCodePoint(text.codePointAt(start-(/[\udc00-\udfff]/.test(text[start-1])?2:1))),after=String.fromCodePoint(text.codePointAt(start));
      const word=c=>NON_CJK_WORD.test(c)&&!CJK_CHAR.test(c);
      assert.ok(!(word(before)&&word(after)),`round ${round}: cut inside a word at ${start}: ${JSON.stringify(text.slice(start-6,start+6))}`);
    }
  }
  assert.ok(fragmented>100&&files>20,`the property was exercised (${fragmented} fragmented, ${files} files)`);
});
