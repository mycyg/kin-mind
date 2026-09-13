/** Paragraphs are intentional bubbles. Words, fences and URLs are never cut. */
export function splitChatText(text) {
  const parts=[]; let block=[],fence=null;
  for(const line of text.split('\n')) {
    const marker=line.trimStart().match(/^(`{3,}|~{3,})/);
    if(marker) {if(!fence)fence=marker[1][0];else if(marker[1][0]===fence)fence=null;}
    if(!line.trim()&&!fence&&block.length){parts.push(block.join('\n').trim());block=[];}
    else block.push(line);
  }
  if(block.length)parts.push(block.join('\n').trim());
  return parts.filter(Boolean);
}

export const chatVoice = '聊天通常每句话不超过20字，用有情绪的完整口语短句，词语写全。语气词、网络梗和颜文字随语境使用。按自然停顿分气泡，以空行分隔；必要说明可以更长，代码、链接、文件名和工作成稿保持完整。闲扯、撒娇、怪念头都可以聊，不需要先交研究作业。';
