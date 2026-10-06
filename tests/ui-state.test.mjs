import {test} from 'node:test';
import assert from 'node:assert/strict';
import {displayStatus,curveSegments,modeSwitchDecision,walletOriginSecure,liveWalletAccountVerified} from '../src/pm_nautilus/web/ui-state.js';
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
test('mode switch remains available during background polling and never starts trading',()=>{
 const state={displayMode:'TEST',loading:true,performanceLoading:true,tradeRecordsLoading:true,controlPending:false,configDirty:false};
 assert.deepEqual(modeSwitchDecision(state),{nextMode:'LIVE',error:null});
 assert.deepEqual(modeSwitchDecision({...state,displayMode:'LIVE'}),{nextMode:'TEST',error:null});
 assert.equal(modeSwitchDecision({...state,controlPending:true}).nextMode,null);
 assert.equal(modeSwitchDecision({...state,configDirty:true}).nextMode,null);
});
test('hour and day points do not connect across an interrupted bucket',()=>{
 const p=(bucket,breakBefore=false,session='a')=>({at:bucket*3600000+3599000,bucket,breakBefore,session,pnl:'1'});
 assert.deepEqual(curveSegments([p(0),p(1),p(2,true),p(3),p(5),p(6,false,'b')]).map(s=>s.length),[2,2,1,1]);
});
test('wallet secrets require HTTPS except loopback development',()=>{
 assert.equal(walletOriginSecure({protocol:'https:',hostname:'trading.example'}),true);
 assert.equal(walletOriginSecure({protocol:'http:',hostname:'trading.example'}),false);
 assert.equal(walletOriginSecure({protocol:'http:',hostname:'127.0.0.1'}),true);
 assert.equal(walletOriginSecure({protocol:'http:',hostname:'localhost'}),true);
});
test('LIVE funds and start require fresh unlocked enabled wallet state',()=>{
 const state={dashboard:{liveExecutionEnabled:true},wallet:{configured:true,unlocked:true,enabled:true,status:'PAUSED',readiness:{ready:true}},walletAt:1000,now:2000,error:null};
 assert.equal(liveWalletAccountVerified(state),true);
 assert.equal(liveWalletAccountVerified({...state,wallet:{...state.wallet,unlocked:false}}),false);
 assert.equal(liveWalletAccountVerified({...state,wallet:{...state.wallet,status:'BLOCKED'}}),false);
 assert.equal(liveWalletAccountVerified({...state,wallet:{...state.wallet,readiness:{ready:false}}}),false);
 assert.equal(liveWalletAccountVerified({...state,wallet:{...state.wallet,enabled:false}}),false);
 assert.equal(liveWalletAccountVerified({...state,dashboard:{liveExecutionEnabled:false}}),false);
 assert.equal(liveWalletAccountVerified({...state,now:22000}),false);
 assert.equal(liveWalletAccountVerified({...state,error:'unavailable'}),false);
});
