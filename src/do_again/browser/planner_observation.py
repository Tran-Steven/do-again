"""Read the response following one exact prompt; never navigate or send."""
from __future__ import annotations
import hashlib
import json
from . import runtime
from ..supervisor.macos_execution import ExecutionBlocked


def original_reply(target, message, *, conversation_id):
    expression = '(() => {' + runtime._MESSAGE_NODES_JS + r'''
const users=messageNodes('user'), assistants=messageNodes('assistant');
const ordered=[...users.map(n=>({n,role:'user'})),...assistants.map(n=>({n,role:'assistant'}))]
  .sort((a,b)=>a.n.compareDocumentPosition(b.n)&Node.DOCUMENT_POSITION_FOLLOWING?-1:1);
const stop=document.querySelector('[data-testid="stop-button"],button[aria-label="Stop generating"],button[aria-label="Stop response"],button[aria-label="Stop"]');
return {url:location.href,busy:!!(stop&&!stop.disabled),messages:ordered.map(v=>({role:v.role,text:(v.n.innerText||v.n.textContent||'').trim()}))};
})()'''
    snapshot = runtime.cdp.evaluate(target, expression, timeout=15.0)
    if (not isinstance(snapshot, dict) or runtime._chat_id(snapshot.get('url', '')) != conversation_id):
        raise ExecutionBlocked('original reply observation changed conversation')
    if snapshot.get('busy'): return None
    matches = []
    nodes = snapshot.get('messages', [])
    expected = hashlib.sha256(message.encode()).hexdigest()
    for i, node in enumerate(nodes):
        if node.get('role') != 'user': continue
        text = str(node.get('text') or '')
        if text.startswith('You said:\n'): text = text[len('You said:\n'):]
        # The UI appends this exact expansion control to a long prompt. Do
        # not strip arbitrary text, whitespace, or recover by call ID alone.
        if text.endswith('\n…\nShow more'): text = text[:-len('\n…\nShow more')]
        if hashlib.sha256(text.encode()).hexdigest() == expected: matches.append(i)
    if len(matches) != 1: return None
    i = matches[0] + 1
    if i >= len(nodes) or nodes[i].get('role') != 'assistant': return None
    reply = runtime._normalize_assistant_text(nodes[i].get('text'))
    return {'conversation_id': conversation_id, 'response': reply} if reply else None
