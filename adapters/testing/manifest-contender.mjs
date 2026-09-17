/** Forked by the manifest tests as a second, real process working on the same
 * reply-manifest directory. It waits for `{now,crashAfterSends}`, runs one
 * pass over the group with a fake transport, and reports what it did. With
 * `crashAfterSends` it exits hard right after that many sends: the receipt is
 * on disk, the manifest is not, and the lease is still held. */
import {TransportManifests} from '../transport-manifest.mjs';
import {createFakeTransport} from './fake-transport.mjs';

const [directory,outbox,groupId]=process.argv.slice(2);
process.on('message',async({now,crashAfterSends=0,slowMs=0})=>{
  const writes=[],transport=createFakeTransport({directory:outbox,clock:()=>now});
  if(slowMs)transport.platform.onSubmit(()=>new Promise(resolve=>setTimeout(resolve,slowMs)));
  const manifests=new TransportManifests({directory,clock:()=>now,role:'cli',sleep:async()=>{},lease:{heartbeat:false},hooks:{
    beforeSave:manifest=>{writes.push(manifest.leaseGeneration);},
    afterSend:()=>{if(crashAfterSends&&transport.sends.length>=crashAfterSends)process.exit(86);}}});
  const result=await manifests.run(groupId,{transport});
  process.send({pid:process.pid,busy:Boolean(result.busy),state:result.manifest?.state??null,writes,sent:transport.sends.map(delivery=>delivery.id)});
});
process.send({ready:true});
