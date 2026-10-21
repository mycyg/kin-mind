/** Automatic context is a projection, not a rewrite of the stored role notes.
 * Full replies, nested notes and delivery bubbles remain available via read_private_notes. */
export function privateNoteContext(recent) {
  if(!recent||!Array.isArray(recent.notes))return recent;
  return {...recent,notes:recent.notes.map(note=>({
    ...Object.fromEntries(['id','at','text','basis','visibility','evidence_ids','input_id','group_id','corrects','delivery_state','model','provider','session','turn_id'].filter(k=>k in note).map(k=>[k,note[k]])),
    ...(Array.isArray(note.delivery)?{delivery:note.delivery.map(d=>({state:d.state,channel:d.channel}))}:{}),
    ...(Array.isArray(note.later_notes)?{later_note_ids:note.later_notes.map(n=>n.id).filter(Boolean)}:{}),
  }))};
}
