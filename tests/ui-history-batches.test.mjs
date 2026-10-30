import {test} from 'node:test';
import assert from 'node:assert/strict';
import {pageHarness, seedTestDashboard} from './ui-app-harness.mjs';

const row = (n, quantity='1') => ({id:`order-${n}`,type:'OPEN',firstSeq:n,quantity});
const reply = (body, ok=true) => ({ok,headers:{get(){return null;}},async text(){return JSON.stringify(body);}});
const pageData = (extra={}) => ({accountId:1,generation:'g',head:'g:10',changesHead:'g:10',nextChangesCursor:null,totalCount:20,nextCursor:'g:5',records:[row(10)],changes:[],...extra});
function setup() {
  const page=pageHarness();seedTestDashboard(page);
  page.ui.activeTab='records';page.ui.tradeRecordsExpanded=true;
  return page;
}
function network(page,bodies) {
  const requests=[];
  page.context.fetch=async path=>{requests.push(new URL(path,'https://localhost'));assert.ok(bodies.length);return reply(bodies.shift());};
  return requests;
}
async function initial(page) {
  network(page,[pageData()]);
  await page.context.loadTradeRecordsForTest();
}
const partial = () => pageData({head:'g:12',changesHead:'g:20',nextChangesCursor:'g:10:20:12',records:[row(20),row(19)],changes:[row(11),row(12)]});

test('one request reads one delta batch and only a completed round advances the head',async()=>{
  const page=setup();await initial(page);
  const bodies=[partial(),pageData({head:'g:20',changesHead:'g:20',records:[row(20),row(19)],changes:[13,14,15,16,17,18].map(n=>row(n))})];
  const requests=network(page,bodies);
  await page.context.loadTradeRecordsForTest({silent:true});
  assert.equal(requests.length,1);
  assert.equal(requests[0].searchParams.get('changes_paged'),'true');
  assert.equal(page.ui.recordHead,'g:10');
  assert.equal(page.ui.recordChangesCursor,'g:10:20:12');
  assert.equal(page.ui.recordChangesFirstSeq,10);
  await page.context.loadTradeRecordsForTest({silent:true});
  assert.equal(requests.length,2);
  assert.equal(requests[1].searchParams.get('since'),'g:10');
  assert.equal(requests[1].searchParams.get('changes_cursor'),'g:10:20:12');
  assert.equal(page.ui.recordHead,'g:20');
  assert.equal(page.ui.recordChangesCursor,null);
  assert.equal(page.ui.recordChangesFirstSeq,null);
  assert.deepEqual(Array.from(page.ui.tradeRecords,r=>r.firstSeq),[20,19,18,17,16,15,14,13,12,11,10]);
});

test('loading older pages during catch-up preserves the delta cursor and insertion boundary',async()=>{
  const page=setup();await initial(page);
  const requests=network(page,[partial(),pageData({head:'g:25',changesHead:'g:25',records:[row(1,'3')],nextCursor:'g:1'}),pageData({head:'g:20',changesHead:'g:20',records:[row(20)],changes:[row(13),row(1,'4')]})]);
  await page.context.loadTradeRecordsForTest();
  await page.context.loadTradeRecordsForTest({more:true});
  assert.equal(requests[1].searchParams.get('since'),null);
  assert.equal(requests[1].searchParams.get('changes_cursor'),null);
  assert.equal(page.ui.recordHead,'g:10');
  assert.equal(page.ui.recordChangesCursor,'g:10:20:12');
  assert.equal(page.ui.recordChangesFirstSeq,10);
  assert.equal(page.ui.recordCursor,'g:1');
  await page.context.loadTradeRecordsForTest();
  assert.equal(requests[2].searchParams.get('changes_cursor'),'g:10:20:12');
  assert.equal(page.ui.tradeRecords.find(r=>r.id==='order-1').quantity,'4');
  assert.ok(page.ui.tradeRecords.some(r=>r.id==='order-13'));
  assert.equal(page.ui.recordCursor,'g:1');
});

test('a failed continuation is retried without acknowledging or discarding its batch',async()=>{
  const page=setup();await initial(page);network(page,[partial()]);
  await page.context.loadTradeRecordsForTest();
  const requests=[];let fail=true;
  page.context.fetch=async path=>{requests.push(new URL(path,'https://localhost'));if(fail){fail=false;return reply({error:'temporary history error'},false);}return reply(pageData({head:'g:20',changesHead:'g:20',records:[row(20)],changes:[row(13)]}));};
  await page.context.loadTradeRecordsForTest({silent:true});
  assert.equal(page.ui.recordHead,'g:10');
  assert.equal(page.ui.recordChangesCursor,'g:10:20:12');
  await page.context.loadTradeRecordsForTest({silent:true});
  assert.equal(requests[0].search,requests[1].search);
  assert.equal(page.ui.recordHead,'g:20');
  assert.equal(page.ui.recordChangesCursor,null);
});

test('switching accounts discards continuation state and an old account response',async()=>{
  const page=setup();await initial(page);network(page,[partial()]);
  await page.context.loadTradeRecordsForTest();
  let resolve;
  page.context.fetch=()=>new Promise(r=>{resolve=r;});
  const pending=page.context.loadTradeRecordsForTest({silent:true});
  // A switch starts a separate dashboard request; keep the earlier resolver.
  const stale=resolve;
  const select=page.element('#test-account-select');select.value='2';select.handlers.change({target:select});
  assert.equal(page.ui.recordHead,null);
  assert.equal(page.ui.recordChangesCursor,null);
  assert.equal(page.ui.recordChangesFirstSeq,null);
  stale(reply(pageData({head:'g:20',changesHead:'g:20',records:[row(20)]})));
  await pending;
  assert.equal(page.ui.testAccountId,2);
  assert.equal(page.ui.recordHead,null);
  assert.equal(page.ui.tradeRecords.length,0);
});

test('an invalid generation cursor clears the full catch-up state',async()=>{
  const page=setup();await initial(page);network(page,[partial()]);
  await page.context.loadTradeRecordsForTest();
  page.context.fetch=async()=>reply({error:'交易历史增量游标已失效，请重新读取'},false);
  await page.context.loadTradeRecordsForTest({silent:true});
  assert.equal(page.ui.recordHead,null);
  assert.equal(page.ui.recordChangesCursor,null);
  assert.equal(page.ui.recordChangesFirstSeq,null);
  assert.equal(page.ui.tradeRecords.length,0);
  assert.equal(page.ui.tradeRecordsLoading,false);
});

test('servers without delta fields remain compatible with the new UI',async()=>{
  const page=setup();
  network(page,[{accountId:1,generation:'g',head:'g:10',nextCursor:'g:1',totalCount:1,records:[row(10)]}]);
  await page.context.loadTradeRecordsForTest();
  assert.equal(page.ui.recordHead,'g:10');
  assert.equal(page.ui.recordChangesCursor,null);
});

test('a round completed below later new orders retains the next round insertion boundary',async()=>{
  const page=setup();await initial(page);
  network(page,[partial(),pageData({head:'g:20',changesHead:'g:20',records:[row(20),row(19)],changes:[13,14,15,16,17,18].map(n=>row(n))}),pageData({head:'g:22',changesHead:'g:40',nextChangesCursor:'g:20:40:22',records:[row(40),row(39)],changes:[row(21),row(22)]}),pageData({head:'g:40',changesHead:'g:40',records:[row(40),row(39)],changes:Array.from({length:16},(_,i)=>row(23+i))})]);
  await page.context.loadTradeRecordsForTest();
  assert.match(page.element('#trade-records-count').textContent,/同步中/);
  await page.context.loadTradeRecordsForTest();
  assert.equal(page.ui.recordHead,'g:20');
  assert.equal(page.ui.tradeRecords[0].firstSeq,20);
  await page.context.loadTradeRecordsForTest();
  assert.equal(page.ui.recordChangesFirstSeq,20);
  await page.context.loadTradeRecordsForTest();
  assert.deepEqual(Array.from(page.ui.tradeRecords,r=>r.firstSeq),Array.from({length:31},(_,i)=>40-i));
  assert.doesNotMatch(page.element('#trade-records-count').textContent,/同步中/);
});

test('expired midround state reloads a fresh first page and rebuilds older paging',async()=>{
  const page=setup();await initial(page);network(page,[partial()]);
  await page.context.loadTradeRecordsForTest();
  page.context.fetch=async()=>reply({error:'交易历史增量游标已失效，请重新读取最新记录'},false);
  await page.context.loadTradeRecordsForTest({silent:true});
  const requests=network(page,[pageData({head:'g:1000',changesHead:'g:1000',records:[row(1000)],nextCursor:'g:1000'}),pageData({head:'g:1000',changesHead:'g:1000',records:[row(999)],nextCursor:'g:999'})]);
  await page.context.loadTradeRecordsForTest();
  assert.equal(requests[0].searchParams.get('since'),null);
  assert.equal(requests[0].searchParams.get('cursor'),null);
  assert.equal(requests[0].searchParams.get('changes_cursor'),null);
  assert.equal(page.ui.recordCursor,'g:1000');
  await page.context.loadTradeRecordsForTest({more:true});
  assert.equal(requests[1].searchParams.get('cursor'),'g:1000');
  assert.deepEqual(Array.from(page.ui.tradeRecords,r=>r.firstSeq),[1000,999]);
});
