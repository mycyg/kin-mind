/** Forked by the lease tests: waits for `{now}`, tries once to take the lease
 * at that clock value and reports whether it won and which generation. It
 * never releases, like a holder that simply stops answering. */
import {acquireLease} from '../state-lease.mjs';

const [directory,id]=process.argv.slice(2);
process.on('message',({now})=>{
  const lease=acquireLease({directory,id,role:'cli',clock:()=>now,heartbeat:false});
  process.send({pid:process.pid,won:Boolean(lease),generation:lease?.generation??null});
});
process.send({ready:true});
