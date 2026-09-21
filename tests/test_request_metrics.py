import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from admin.metrics import MetricsMiddleware, RequestMetrics, normalize_usage, usage_from_event, carries_token, day_key, _new_bucket


class MetricsTests(unittest.IsolatedAsyncioTestCase):
    async def run_request(self, status=200, chunks=None, stream=False, source="api", path="/v1/responses", crash=False):
        metrics = RequestMetrics()
        chunks = chunks if chunks is not None else [b'{"ok":true}']
        sent = []
        async def app(scope, receive, send):
            self.assertEqual(metrics.snapshot()['in_flight'], int(path == '/v1/responses'))
            await send({'type':'http.response.start','status':status,'headers':[(b'content-type', b'text/event-stream' if stream else b'application/json')]})
            for chunk in chunks:
                await send({'type':'http.response.body','body':chunk,'more_body':True})
            if crash:
                raise RuntimeError('interrupted')
            await send({'type':'http.response.body','body':b'','more_body':False})
        async def receive():
            return {'type':'http.request','body':b''}
        async def send(message):
            sent.append(message)
        scope={'type':'http','method':'POST','path':path}
        middleware=MetricsMiddleware(app,metrics,source)
        if crash:
            with self.assertRaises(RuntimeError): await middleware(scope,receive,send)
        else:
            await middleware(scope,receive,send)
        self.assertEqual(b''.join(m.get('body',b'') for m in sent), b''.join(chunks))
        return metrics.snapshot()

    async def test_success_and_http_error(self):
        s=await self.run_request(); self.assertEqual(s['completed'],1);self.assertEqual(s['success_rate'],100);self.assertEqual(s['in_flight'],0)
        s=await self.run_request(status=401);self.assertEqual(s['failed'],1);self.assertEqual(s['http_success_rate'],0)

    async def test_stream_error_with_200_is_not_success(self):
        s=await self.run_request(stream=True,chunks=[b'data: {"err',b'or":{"code":429}}\n\ndata: [DONE]\n\n'])
        self.assertEqual(s['http_success_rate'],100);self.assertEqual(s['success_rate'],0)
        self.assertEqual(s['recent'][0]['outcome'],'stream_error')

    async def test_complete_and_incomplete_stream(self):
        s=await self.run_request(stream=True,chunks=[b'data: {"type":"response.completed"}\n\n'])
        self.assertEqual(s['success_rate'],100)
        s=await self.run_request(stream=True,chunks=[b'data: {"delta":"text"}\n\n'])
        self.assertEqual(s['success_rate'],0);self.assertEqual(s['recent'][0]['outcome'],'interrupted')
        s=await self.run_request(stream=True,chunks=[b'data: {"type":"response.completed","response":{"status":"failed"}}\n'])
        self.assertEqual(s['failed'],1)

    async def test_exception_and_excluded_paths(self):
        s=await self.run_request(crash=True);self.assertEqual(s['failed'],1);self.assertEqual(s['in_flight'],0)
        s=await self.run_request(path='/admin/api/overview');self.assertEqual(s['completed'],0)
        s=await self.run_request(source='test');self.assertEqual(s['test_count'],1);self.assertEqual(s['api_count'],0)

    async def test_privacy_and_bounded_history(self):
        s=await self.run_request(chunks=[b'{"secret":"do-not-store"}'])
        self.assertNotIn('do-not-store',json.dumps(s))
        metrics=RequestMetrics()
        for _ in range(110):
            metrics.begin();metrics.finish('/v1/messages','api',200,True,10,'success')
        s=metrics.snapshot();self.assertEqual(len(s['recent']),100);self.assertEqual(s['completed'],110);self.assertEqual(s['avg_duration_ms'],10)

    async def test_client_disconnect(self):
        metrics=RequestMetrics()
        async def app(scope,receive,send):
            await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'text/event-stream')]})
            await receive()
        async def receive():return {'type':'http.disconnect'}
        async def send(m):pass
        await MetricsMiddleware(app,metrics)({'type':'http','method':'POST','path':'/v1/messages'},receive,send)
        s=metrics.snapshot();self.assertEqual(s['failed'],1);self.assertEqual(s['recent'][0]['outcome'],'interrupted')


class UsageTests(unittest.TestCase):
    def test_every_upstream_dialect_normalizes(self):
        chat=normalize_usage({'prompt_tokens':46,'completion_tokens':34,'total_tokens':80,'prompt_cache_hit_tokens':20,'completion_thinking_tokens':32})
        self.assertEqual((chat['prompt_tokens'],chat['completion_tokens'],chat['cache_hit_tokens'],chat['reasoning_tokens']),(46,34,20,32))
        responses=normalize_usage({'input_tokens':100,'output_tokens':20,'input_tokens_details':{'cached_tokens':80}})
        self.assertEqual((responses['prompt_tokens'],responses['cache_hit_tokens']),(100,80))
        anthropic=normalize_usage({'input_tokens':100,'output_tokens':20,'cache_read_input_tokens':60})
        self.assertEqual((anthropic['prompt_tokens'],anthropic['cache_hit_tokens']),(100,60))
        for empty in ({},None,{'foo':1}):
            self.assertIsNone(normalize_usage(empty))

    def test_cache_hit_never_exceeds_prompt_tokens(self):
        usage=normalize_usage({'input_tokens':10,'output_tokens':5,'cache_read_input_tokens':999})
        self.assertEqual(usage['cache_hit_tokens'],10)

    def test_usage_extracted_from_each_protocol_frame(self):
        self.assertEqual(usage_from_event({'type':'message_start','message':{'usage':{'input_tokens':5}}})['input_tokens'],5)
        self.assertEqual(usage_from_event({'type':'message_delta','usage':{'output_tokens':7}})['output_tokens'],7)
        self.assertEqual(usage_from_event({'type':'response.completed','response':{'usage':{'input_tokens':3}}})['input_tokens'],3)
        self.assertEqual(usage_from_event({'choices':[],'usage':{'prompt_tokens':9}})['prompt_tokens'],9)
        self.assertIsNone(usage_from_event({'type':'response.created','response':{}}))

    def test_first_token_detection_covers_every_protocol(self):
        for frame in [{'choices':[{'delta':{'content':'a'}}]},{'choices':[{'delta':{'reasoning_content':'r'}}]},
                      {'type':'response.output_text.delta','delta':'a'},{'type':'response.reasoning_summary_text.delta','delta':'r'},
                      {'type':'content_block_delta','delta':{'type':'text_delta','text':'a'}},
                      {'type':'content_block_delta','delta':{'type':'thinking_delta','thinking':'r'}},
                      {'type':'content_block_start','content_block':{'type':'tool_use'}}]:
            self.assertTrue(carries_token(frame),frame)
        for empty in [{'choices':[{'delta':{'role':'assistant','content':''}}]},{'type':'response.created','response':{}},
                      {'type':'content_block_delta','delta':{'type':'text_delta','text':''}},{'type':'message_start'}]:
            self.assertFalse(carries_token(empty),empty)


class StatsAggregateTests(unittest.TestCase):
    def record(self,metrics,usage=None,ttft=None,duration=1000,ok=True,model='m1',source='api'):
        metrics.begin()
        metrics.finish('/v1/chat/completions',source,200,ok,duration,'success' if ok else 'stream_error',usage=usage,ttft_ms=ttft,model=model)

    def test_cache_rate_and_throughput_fold(self):
        metrics=RequestMetrics()
        self.record(metrics,{'prompt_tokens':1000,'completion_tokens':200,'prompt_cache_hit_tokens':900},ttft=2000,duration=4000)
        self.record(metrics,{'prompt_tokens':1000,'completion_tokens':100,'prompt_cache_hit_tokens':100},ttft=1000,duration=3000)
        today=metrics.stats()['today']
        self.assertEqual(today['requests'],2)
        self.assertEqual(today['success_rate'],100.0)
        self.assertEqual(today['cache_hit_rate'],50.0)
        self.assertEqual(today['cache_hit_tokens'],1000)
        self.assertEqual(today['prompt_tokens'],2000)
        self.assertEqual(today['completion_tokens'],300)
        # decode spans 4000-2000 and 3000-1000 ms over 300 output tokens.
        self.assertEqual(today['tokens_per_second'],75.0)
        self.assertEqual(today['avg_duration_ms'],3500)
        self.assertEqual(today['ttft_samples'],2)

    def test_ttft_percentile_is_histogram_interpolated(self):
        metrics=RequestMetrics()
        for ttft in (1000,2000,3000,4000):
            self.record(metrics,{'prompt_tokens':10,'completion_tokens':5},ttft=ttft,duration=ttft+1000)
        today=metrics.stats()['today']
        # Bucketed, so the median lands inside the 1500-2000 ms bucket rather
        # than exactly on the sample mean; both readings stay bounded by it.
        self.assertEqual(today['ttft_p50_ms'],2000)
        self.assertEqual(today['ttft_avg_ms'],2500)

    def test_percentile_without_samples_is_null(self):
        metrics=RequestMetrics()
        self.record(metrics,{'prompt_tokens':10,'completion_tokens':5},ttft=None)
        today=metrics.stats()['today']
        self.assertIsNone(today['ttft_p50_ms']);self.assertIsNone(today['tokens_per_second'])
        # Prompt tokens were reported without any cache hits: a real 0%, not "unknown".
        self.assertEqual(today['cache_hit_rate'],0.0)
        bare=RequestMetrics();self.record(bare)
        self.assertIsNone(bare.stats()['today']['cache_hit_rate'])

    def test_failures_and_sources_split(self):
        metrics=RequestMetrics()
        self.record(metrics,ok=False,source='test')
        self.record(metrics,ok=True,source='api')
        today=metrics.stats()['today']
        self.assertEqual((today['requests'],today['failed'],today['success_rate']),(2,1,50.0))
        self.assertEqual((today['api_requests'],today['test_requests']),(1,1))

    def test_models_ranked_by_requests(self):
        metrics=RequestMetrics()
        for _ in range(2): self.record(metrics,model='b')
        self.record(metrics,model='a')
        rows=metrics.stats()['today']['models']
        self.assertEqual([row['model'] for row in rows],['b','a'])
        self.assertEqual([row['requests'] for row in rows],[2,1])

    def test_all_range_sums_days_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'metrics.json'
            metrics=RequestMetrics(path)
            self.record(metrics,{'prompt_tokens':100,'completion_tokens':10,'prompt_cache_hit_tokens':50},ttft=500,duration=1500,model='m1')
            metrics.days['2026-01-01']={'all':_new_bucket(),'models':{}}
            metrics.dirty=True
            metrics.save()
            self.assertTrue(path.exists())
            reloaded=RequestMetrics(path)
            stats=reloaded.stats()
            self.assertEqual(stats['all']['requests'],1)
            self.assertEqual(stats['all']['days'],2)
            self.assertEqual(stats['all']['since'],'2026-01-01')
            self.assertEqual(stats['today']['requests'],1)
            self.assertEqual(stats['all']['cache_hit_rate'],50.0)
            self.assertNotIn('secret',path.read_text(encoding='utf-8'))

    def test_privacy_and_bounded_day_history(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'metrics.json'
            metrics=RequestMetrics(path)
            self.record(metrics,{'prompt_tokens':1,'completion_tokens':1},model='m1')
            self.record(metrics)
            today=day_key()
            # Day keys are calendar dates, so the oldest buckets prune first.
            metrics.days.update({f'2020-01-{i%28+1:02d}-{i:03d}':{'all':_new_bucket(),'models':{}} for i in range(120)})
            metrics.dirty=True
            metrics.save()
            raw=json.loads(path.read_text(encoding='utf-8'))
            self.assertLessEqual(len(raw['days']),90)
            self.assertIn(today,raw['days'])
            self.assertEqual(metrics.stats()['all']['requests'],2)
            # Only counters survive: no usage payload, prompt or completion text.
            text=path.read_text(encoding='utf-8')
            self.assertNotIn('prompt_tokens_details',text)
            self.assertNotIn('secret-answer',text)
            self.assertNotIn('sk-',text)

    def test_model_names_are_bounded_in_persisted_state(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'metrics.json'
            metrics=RequestMetrics(path)
            self.record(metrics,model='x'*500)
            metrics.save()
            raw=json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(len(next(iter(raw['days'].values()))['models']),1)
            self.assertEqual(len(next(iter(next(iter(raw['days'].values()))['models']))),80)

    def test_corrupt_state_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'metrics.json'
            path.write_text('{not json',encoding='utf-8')
            self.assertEqual(RequestMetrics(path).stats()['all']['requests'],0)

    def test_day_key_uses_beijing_calendar(self):
        # 2026-01-01T00:30+08:00 is still Dec 31 UTC.
        self.assertEqual(day_key(1767198600),'2026-01-01')


class StreamUsageTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self,chunks,path='/v1/chat/completions',stream=True):
        metrics=RequestMetrics()
        async def app(scope,receive,send):
            await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'text/event-stream' if stream else b'application/json')]})
            for chunk in chunks:
                await send({'type':'http.response.body','body':chunk,'more_body':True})
            await send({'type':'http.response.body','body':b'','more_body':False})
        async def receive():return {'type':'http.request','body':b''}
        async def send(m):pass
        await MetricsMiddleware(app,metrics)({'type':'http','method':'POST','path':path},receive,send)
        return metrics.stats()

    async def test_openai_stream_usage_and_ttft(self):
        body=(b'data: {"model":"m1","choices":[{"delta":{"role":"assistant","content":""}}]}\n\n'
              b'data: {"model":"m1","choices":[{"delta":{"content":"hi"}}]}\n\n'
              b'data: {"model":"m1","choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":100,"completion_tokens":10,"prompt_cache_hit_tokens":80}}\n\n'
              b'data: [DONE]\n\n')
        stats=await self.collect([body])
        today=stats['today']
        self.assertEqual(today['requests'],1)
        self.assertEqual(today['success_rate'],100.0)
        self.assertEqual(today['cache_hit_rate'],80.0)
        self.assertEqual(today['completion_tokens'],10)
        self.assertEqual(today['models'][0]['model'],'m1')
        self.assertEqual(today['ttft_samples'],1)

    async def test_anthropic_stream_usage_from_message_delta(self):
        body=(b'event: message_start\ndata: {"type":"message_start","message":{"model":"m2","usage":{"input_tokens":0,"output_tokens":0}}}\n\n'
              b'event: message_delta\ndata: {"type":"message_delta","usage":{"input_tokens":200,"output_tokens":30,"cache_read_input_tokens":150}}\n\n'
              b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
        stats=await self.collect([body],path='/v1/messages')
        today=stats['today']
        self.assertEqual(today['success_rate'],100.0)
        self.assertEqual(today['prompt_tokens'],200)
        self.assertEqual(today['cache_hit_rate'],75.0)
        self.assertEqual(today['models'][0]['model'],'m2')

    async def test_responses_stream_usage_from_completed(self):
        body=(b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"hi"}\n\n'
              b'event: response.completed\ndata: {"type":"response.completed","response":{"model":"m3","status":"completed","usage":{"input_tokens":50,"output_tokens":5,"input_tokens_details":{"cached_tokens":25}}}}\n\n')
        stats=await self.collect([body],path='/v1/responses')
        today=stats['today']
        self.assertEqual(today['success_rate'],100.0)
        self.assertEqual(today['cache_hit_rate'],50.0)
        self.assertEqual(today['models'][0]['model'],'m3')
        self.assertEqual(today['ttft_samples'],1)

    async def test_nonstream_body_usage_without_ttft(self):
        body=json.dumps({'model':'m4','choices':[{'message':{'content':'hi'}}],'usage':{'prompt_tokens':40,'completion_tokens':4,'prompt_cache_hit_tokens':20}}).encode()
        stats=await self.collect([body],stream=False)
        today=stats['today']
        self.assertEqual(today['prompt_tokens'],40)
        self.assertEqual(today['cache_hit_rate'],50.0)
        self.assertIsNone(today['ttft_p50_ms'])
        self.assertEqual(today['models'][0]['model'],'m4')

    async def test_usage_never_leaks_into_response_or_snapshot(self):
        body=(b'data: {"choices":[{"delta":{"content":"secret-answer"}}]}\n\n'
              b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":7,"completion_tokens":2}}\n\n'
              b'data: [DONE]\n\n')
        stats=await self.collect([body])
        self.assertNotIn('secret-answer',json.dumps(stats))


if __name__=='__main__':unittest.main()
