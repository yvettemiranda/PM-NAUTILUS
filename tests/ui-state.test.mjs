import {test} from 'node:test';
import assert from 'node:assert/strict';
import {displayStatus,curveSegments} from '../src/pm_nautilus/web/ui-state.js';
const base=()=>({executionMode:'TEST',strategy:{status:'RUNNING'},positions:[{currentSellPriceStatus:'NO_BID'}],marketScan:{lastScanAt:'2026-09-22',diagnostics:{streams:{groups:[{taskRunning:true,state:'READY',readyBookCount:2,tokenCount:2}]}}}});
const opts={lastSuccess:1000,now:2000};
test('quiet/no-bid is healthy; stale UI cannot imply running',()=>{
 assert.equal(displayStatus(base(),opts).feed,'ready');
 assert.equal(displayStatus(base(),{...opts,now:12000}).run,'error');
 assert.equal(displayStatus(base(),{...opts,error:'offline'}).feed,'error');
});
test('fatal, missing books and scanning have separate status',()=>{
 const d=base();d.marketScan.scanning=true;assert.equal(displayStatus(d,opts).scan,'waiting');
 d.positions[0].currentSellPriceStatus='NOT_READY';assert.equal(displayStatus(d,opts).feed,'waiting');
 d.marketScan.serviceError='fatal';assert.equal(displayStatus(d,opts).run,'error');
 d.executionMode='LIVE';d.liveExecutionEnabled=false;assert.equal(displayStatus(d,opts).run,'off');
});
test('curve never joins unknown, restart or unsampled interval',()=>{
 const p=(at,pnl,session='a')=>({at,pnl,session});
 const series=curveSegments([p(0,'0'),p(60000,'1'),p(120000,null),p(180000,'-1'),p(240000,'2','b'),p(400000,'3','b')]);
 assert.deepEqual(series.map(s=>s.length),[2,1,1,1]);
 assert.equal(curveSegments([p(0,null)]).length,0);
});
