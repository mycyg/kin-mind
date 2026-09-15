/** Clear a legacy transport alarm only from reconciled submission boundaries. */
export function reconcileRoutingAlarm(status, router) {
  if(status.routingError!=='input-acceptance-unconfirmed')return {};
  const inputs=Object.values(router?.inputs??{});
  const uncertain=inputs.some(i=>['selected','preparing','submitting','unconfirmed'].includes(i.state));
  const proofs=inputs.filter(i=>i.state==='failed-before-submit'&&i.reconciliation?.kind==='native-submit-not-reached');
  if(uncertain||!proofs.length)return {};
  if(proofs.some(i=>!i.reconciliation.source_sha256))return {};
  return {routingError:null,routingRecovery:{kind:'verified-input-boundaries',inputIds:proofs.map(i=>i.id),
    previousError:status.routingError,previousErrorAt:status.lastRoutingErrorAt??null}};
}
