import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

import httpx
from fastapi import HTTPException

from core import converter
from admin.server import Store
from admin.pool import AccountPool, PoolMiddleware, REQUEST_CREDENTIAL, summarize_packages, request_affinity
from test_admin_server import credential


class PoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = Store(root/'management', root/'auth', 'api-test', 'admin-long-enough-test-key')
        self.a = self.store.save_browser_account(credential('one'), 'one')['account_id']
        self.b = self.store.save_browser_account(credential('two'), 'two')['account_id']
        self.now = 1800000000
        self.checked = False
        self.checkin_posts = 0
        self.fail = False
        self.pages = []
        def respond(req):
            if self.fail:
                return httpx.Response(500)
            if req.url.path.endswith('checkin-activity-status'):
                return httpx.Response(200,json={'code':0,'data':{'active':True,'today_checked_in':self.checked}})
            if req.url.path.endswith('daily-checkin'):
                self.checkin_posts += 1; self.checked=True
                return httpx.Response(200,json={'code':0,'data':{}})
            body=json.loads(req.content)
            self.pages.append(body['PageNumber'])
            return httpx.Response(200,json={'code':0,'data':{'Response':{'Data':{'TotalCount':1,'Accounts':[{'CycleCapacityRemainPrecise':'479.59','CycleCapacitySize':500,'CapacityRemain':500}]}}}})
        self.pool=AccountPool(self.store,clock=lambda:self.now,client_factory=lambda:httpx.Client(transport=httpx.MockTransport(respond)))
        for aid,item in self.store.data['accounts'].items():
            self.store.manager_for(aid,item).get_headers=Mock(return_value={'X-Domain':'www.codebuddy.cn'})

    async def asyncTearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_precise_cycle_balance_and_unknown(self):
        result=summarize_packages([{'CapacityRemain':500,'CapacitySize':500,'CycleCapacityRemainPrecise':'479.59','CycleCapacitySizePrecise':'500'},{'CapacityRemainPrecise':'0.12','CapacitySize':100}])
        self.assertEqual(result['remaining'],479.71)
        with self.assertRaises(ValueError): summarize_packages([{}])
        self.assertEqual(self.pool.state(self.store.account_rows()[0]),'available')

    def test_round_robin_paused_cooling_exhausted_and_manual(self):
        self.assertEqual([self.pool.select()[0] for _ in range(4)],[self.a,self.b,self.a,self.b])
        self.store.data['accounts'][self.a]['enabled']=False
        self.assertEqual(self.pool.select()[0],self.b)
        self.pool.record(self.b,429)
        with self.assertRaises(HTTPException):self.pool.select()
        self.now+=1801
        self.assertEqual(self.pool.select()[0],self.b)
        self.pool.update(self.b,remaining=0,packages=1,credits_updated=self.now)
        with self.assertRaises(HTTPException):self.pool.select()
        self.now+=601
        self.assertEqual(self.pool.select()[0],self.b)
        self.store.data['pool']['routing']='manual';self.store.data['active']=self.a
        with self.assertRaises(HTTPException):self.pool.select()

    def test_affinity_failover_persistence_expiry_and_manual(self):
        key = request_affinity({b'authorization': b'Bearer api-test'}, {'model': 'm', 'prompt_cache_key': 'session-one'})
        self.assertEqual([self.pool.select(key)[0] for _ in range(3)], [self.a] * 3)
        self.pool.record(self.a)
        self.assertEqual(AccountPool(self.store, clock=lambda: self.now).select(key)[0], self.a)
        self.pool.record(self.a, 429)
        self.assertEqual(self.pool.select(key)[0], self.b)
        self.now += 1801
        self.assertEqual(self.pool.select(key)[0], self.b)
        self.store.data['pool']['routing'] = 'manual'
        self.store.data['active'] = self.a
        self.assertEqual(self.pool.select(key)[0], self.a)
        self.now += 86401
        self.pool.select()
        self.assertNotIn(key, self.store.data['session_bindings'])

    def test_affinity_is_scoped_to_caller_model_and_session(self):
        headers = {b'authorization': b'Bearer one'}
        body = {'model': 'm', 'prompt_cache_key': 'secret-session'}
        key = request_affinity(headers, body)
        self.assertEqual(len(key), 64)
        self.assertNotEqual(key, request_affinity({b'authorization': b'Bearer two'}, body))
        self.assertNotEqual(key, request_affinity(headers, dict(body, model='other')))
        self.assertEqual(key, request_affinity(headers, dict(body, input='next question')))
        self.assertEqual(request_affinity(headers, {'input': [{'role': 'user', 'content': 'first'}]}),
                         request_affinity(headers, {'input': [{'role': 'user', 'content': 'first'}, {'role': 'user', 'content': 'next'}]}))

    async def test_middleware_replays_body_and_reuses_session_account(self):
        seen = []
        async def app(scope, receive, send):
            message = await receive()
            seen.append((REQUEST_CREDENTIAL.get().path.name, json.loads(message['body'])))
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=PoolMiddleware(app, self.pool)), base_url='http://test') as client:
            bodies = [{'prompt_cache_key': 'same', 'input': 'first'}, {'prompt_cache_key': 'same', 'input': 'next'}]
            for body in bodies:
                response = await client.post('/v1/responses', json=body, headers={'Authorization': 'Bearer api-test'})
                self.assertEqual(response.status_code, 200)
        self.assertEqual(seen[0][0], seen[1][0])
        self.assertEqual([item[1] for item in seen], bodies)

    async def test_responses_failed_event_cools_account(self):
        async def app(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': [(b'content-type', b'text/event-stream')]})
            await send({'type': 'http.response.body', 'body': b'event: response.failed\ndata: {"type":"response.failed","response":{"error":{"code":"429"}}}\n\n'})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=PoolMiddleware(app, self.pool)), base_url='http://test') as client:
            await client.post('/v1/responses', json={'input': 'hello'}, headers={'Authorization': 'Bearer api-test'})
        self.assertGreater(self.store.data['account_status'][self.a]['cooldown_until'], self.now)

    async def test_checkin_idempotent_and_failure_retains_balance(self):
        a,b=await asyncio.gather(asyncio.to_thread(self.pool.operate,self.a,'checkin'),asyncio.to_thread(self.pool.operate,self.a,'checkin'))
        self.assertTrue(a['ok'] and b['ok']);self.assertEqual(self.checkin_posts,1)
        self.assertEqual(self.store.data['account_status'][self.a]['remaining'],479.59)
        self.fail=True
        result=await asyncio.to_thread(self.pool.operate,self.a,'status')
        self.assertFalse(result['ok']);self.assertEqual(self.store.data['account_status'][self.a]['remaining'],479.59)

    async def test_paused_excluded_from_batch_and_schedule_persists(self):
        self.store.data['accounts'][self.b]['enabled']=False
        self.store.data['pool']['checkin_time']='00:00'
        await self.pool.tick()
        self.assertEqual(self.checkin_posts,1)
        self.assertNotIn(self.b,self.store.data['account_status'])
        restarted=AccountPool(self.store,clock=lambda:self.now,client_factory=self.pool.client_factory)
        await restarted.tick()
        self.assertEqual(self.checkin_posts,1)
        data=json.loads(json.dumps(self.store.db.load_state()))
        self.assertTrue(data['account_status'][self.a]['checkin_date'])

    async def test_context_isolation_concurrent_requests_and_auth(self):
        async def app(scope,receive,send):
            before=REQUEST_CREDENTIAL.get().path.name
            await asyncio.sleep(.01)
            after=REQUEST_CREDENTIAL.get().path.name
            data=json.dumps({'before':before,'after':after}).encode()
            await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'application/json')]})
            await send({'type':'http.response.body','body':data})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=PoolMiddleware(app,self.pool)),base_url='http://test') as client:
            self.assertEqual((await client.post('/v1/responses')).status_code,401)
            results=await asyncio.gather(*(client.post('/v1/responses',headers={'Authorization':'Bearer api-test'}) for _ in range(4)))
            files=[r.json()['before'] for r in results]
            self.assertEqual(len(set(files)),2)
            self.assertTrue(all(r.json()['before']==r.json()['after'] for r in results))
        self.assertIsNone(REQUEST_CREDENTIAL.get())

    async def test_sse_error_marks_cooldown_without_replay(self):
        calls=[]
        async def app(scope,receive,send):
            calls.append(1)
            await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'text/event-stream')]})
            for part in [b'data: {"error":',b'{"code":429}}\n\n']:
                await send({'type':'http.response.body','body':part,'more_body':True})
            await send({'type':'http.response.body','body':b''})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=PoolMiddleware(app,self.pool)),base_url='http://test') as client:
            response=await client.post('/v1/messages',headers={'Authorization':'Bearer api-test'})
            self.assertEqual(response.status_code,200)
        self.assertEqual(len(calls),1)
        self.assertGreater(self.store.data['account_status'][self.a]['cooldown_until'],self.now)
        self.assertEqual(self.pool.select()[0],self.b)

    def test_concurrency_limit_skips_saturated_account_and_releases(self):
        self.store.data['accounts'][self.a]['max_concurrent'] = 1
        self.assertEqual(self.pool.select()[0], self.a)
        self.assertEqual(self.pool.inflight[self.a], 1)
        # a 已达并发上限：状态变为 busy，轮询跳到 b。
        row = next(r for r in self.store.account_rows() if r['id'] == self.a)
        self.assertEqual(self.pool.state(row), 'busy')
        self.assertEqual(self.pool.select()[0], self.b)
        # 释放名额后 a 重新可用。
        self.pool.release(self.a)
        self.assertNotIn(self.a, self.pool.inflight)
        self.assertEqual(self.pool.select()[0], self.a)

    def test_all_accounts_saturated_fails_fast(self):
        self.store.data['accounts'][self.a]['max_concurrent'] = 1
        self.store.data['accounts'][self.b]['max_concurrent'] = 1
        self.pool.select()
        self.pool.select()
        with self.assertRaises(HTTPException):
            self.pool.select()
        self.pool.release(self.a)
        self.assertEqual(self.pool.select()[0], self.a)

    def test_zero_limit_is_unbounded_and_rows_expose_concurrency(self):
        self.store.data['accounts'][self.a]['max_concurrent'] = 0
        for _ in range(5):
            self.pool.select()
        self.assertEqual(self.pool.inflight.get(self.a), 3)
        rows = self.pool.rows(self.store.account_rows())
        row = next(r for r in rows if r['id'] == self.a)
        self.assertEqual(row['max_concurrent'], 0)
        self.assertEqual(row['inflight'], 3)

    def test_release_is_idempotent_and_ignores_unknown(self):
        self.pool.release('missing')
        self.pool.select()
        self.pool.release(self.a)
        self.pool.release(self.a)
        self.assertNotIn(self.a, self.pool.inflight)

    async def test_middleware_releases_slot_after_each_request(self):
        self.store.data['accounts'][self.a]['max_concurrent'] = 1
        self.store.data['accounts'][self.b]['max_concurrent'] = 1
        async def app(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=PoolMiddleware(app, self.pool)), base_url='http://test') as client:
            for _ in range(4):
                response = await client.post('/v1/responses', json={'input': 'x'}, headers={'Authorization': 'Bearer api-test'})
                self.assertEqual(response.status_code, 200)
        self.assertEqual(self.pool.inflight, {})

    async def test_middleware_saturates_then_fails_fast(self):
        self.store.data['accounts'][self.a]['max_concurrent'] = 1
        self.store.data['accounts'][self.b]['max_concurrent'] = 1
        gate = asyncio.Event()
        async def app(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await gate.wait()
            await send({'type': 'http.response.body', 'body': b'ok'})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=PoolMiddleware(app, self.pool)), base_url='http://test') as client:
            held = [asyncio.create_task(client.post('/v1/responses', json={'input': str(i)}, headers={'Authorization': 'Bearer api-test'})) for i in range(2)]
            for _ in range(200):
                if sum(self.pool.inflight.values()) >= 2:
                    break
                await asyncio.sleep(.01)
            third = await client.post('/v1/responses', json={'input': '3'}, headers={'Authorization': 'Bearer api-test'})
            self.assertEqual(third.status_code, 503)
            gate.set()
            results = await asyncio.gather(*held)
            self.assertTrue(all(r.status_code == 200 for r in results))
        self.assertEqual(self.pool.inflight, {})

    async def test_middleware_publishes_selected_account_to_scope(self):
        seen = []
        async def app(scope, receive, send):
            seen.append(scope.get('account_label'))
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})
        self.store.data['pool']['routing'] = 'manual'
        self.store.data['active'] = self.b
        self.store.data['accounts'][self.b]['name'] = '账号乙'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=PoolMiddleware(app, self.pool)), base_url='http://test') as client:
            response = await client.post('/v1/responses', json={'input': 'x'}, headers={'Authorization': 'Bearer api-test'})
            self.assertEqual(response.status_code, 200)
        self.assertEqual(seen, ['账号乙'])

    def test_account_label_falls_back_to_id_when_unnamed(self):
        self.store.data['accounts'][self.a]['name'] = ''
        self.assertEqual(self.pool.account_label(self.a), self.a)

    def test_affinity_rebinds_when_bound_account_temporarily_busy(self):
        key = request_affinity({b'authorization': b'Bearer api-test'}, {'model': 'm', 'prompt_cache_key': 'sticky'})
        self.store.data['accounts'][self.a]['max_concurrent'] = 1
        self.assertEqual(self.pool.select(key)[0], self.a)
        # a 满员：本次落到 b，绑定随即改写到 b。
        self.assertEqual(self.pool.select(key)[0], self.b)
        self.assertEqual(self.store.data['session_bindings'][key]['account_id'], self.b)
        # a 名额释放后会话仍留在 b（不再回到原账号）。
        self.pool.release(self.a)
        self.assertEqual(self.pool.select(key)[0], self.b)

    def test_pagination_is_complete(self):
        def handler(req):
            page=json.loads(req.content)['PageNumber']
            accounts=[{'CapacityRemainPrecise':'1.25','CapacitySize':2}]* (100 if page==1 else 1)
            return httpx.Response(200,json={'code':0,'data':{'Response':{'Data':{'TotalCount':101,'Accounts':accounts}}}})
        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            result=self.pool.credits(c,{})
        self.assertEqual(result['packages'],101)
        self.assertEqual(result['remaining'],126.25)


if __name__=='__main__':unittest.main()
