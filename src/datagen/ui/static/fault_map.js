
async function faultIncidents(id){
  try{ return await (await fetch('/api/faults/'+encodeURIComponent(id)+'/incidents')).json(); }
  catch(e){ return {error:String(e), incidents:[]}; }
}
function faultMapUrl(id, got){
  return '/map?'+new URLSearchParams({fault:id, inc:got[0].incident_id, ids:got.map(x=>x.incident_id).join(',')});
}
