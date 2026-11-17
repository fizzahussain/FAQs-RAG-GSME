"""Real incremental parsing / SSE transport, with synthetic providers only."""

import json
import os
import socket
import time
from contextlib import contextmanager
from threading import Thread, Event
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, MagicMock, patch
from uuid import uuid4

import httpx
import uvicorn

from fastapi.testclient import TestClient

from gsme_rag.generation.answers import (AnswerSettings, AnswerCancelled, AnswerProviderUnavailable,
    CommandAnswers, InvalidAnswer, OllamaAnswers, generate_answer)
from gsme_rag.generation.gemini import GeminiAnswers
from gsme_rag.generation.streaming import completed_claims, strict_loads
from gsme_rag.hosted.app import create_hosted_app
from gsme_rag.hosted.access import Principal
from gsme_rag.storage.config import Settings
from tests.test_answer_generation import report, provider


@contextmanager
def local_server(app):
    listener=socket.socket();listener.bind(('127.0.0.1',0))
    server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
    thread=Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True)
    try:
        thread.start();deadline=time.monotonic()+10
        while not server.started and time.monotonic()<deadline:time.sleep(.02)
        if not server.started:raise AssertionError('Local synthetic stream server did not start')
        yield f'http://127.0.0.1:{listener.getsockname()[1]}'
    finally:
        server.should_exit=True;thread.join(10);listener.close()


def claim(text="Contact Example Team.", quote="E1Q1"):
    return {"text":text,"supports":[{"quote_id":quote}]}


class StreamFixture:
    name, model = "cohere", "synthetic-stream"

    def __init__(self, parts):
        self.parts, self.closed = parts, False

    def generate_stream(self, question, evidence):
        try:
            yield from self.parts()
        finally:
            self.closed = True


class StructuredStreamTests(TestCase):
    def test_streamed_vague_stage_clarification_recovers_only_final_cited_source_text(self):
        context=report();context['results'][0]['context_parts'][0]['text']='Manufacturing involves three main stages: fabrication, assembly, and testing.'
        events=[];fallback=provider('ollama')
        primary=StreamFixture(lambda:iter(['{"status":"clarification_needed",','"claims":[]}']))
        result=generate_answer('What are the three main semiconductor manufacturing stages?',context,lambda _:True,
            primary=primary,fallback=fallback,on_event=events.append)
        self.assertEqual(result['status'],'answered')
        self.assertEqual(result['citations'][0]['chunk_id'],'c1')
        self.assertIn('fabrication, assembly, and testing',result['text'])
        self.assertFalse([event for event in events if event['type']=='claim'])
        self.assertTrue(primary.closed);fallback.generate.assert_not_called()

    def test_first_claim_arrives_while_provider_is_still_generating(self):
        events=[]
        def parts():
            yield '{"status":"answered","claims":[' + json.dumps(claim()) + ','
            self.assertEqual([e['text'] for e in events if e['type']=='claim'], ['Contact Example Team. [E1]'])
            yield json.dumps(claim('Example Team is the contact.')) + ']}'
        primary=StreamFixture(parts)
        result=generate_answer('Who?', report(), lambda _:True, primary=primary, on_event=events.append)
        self.assertEqual(result['status'],'answered')
        self.assertEqual(len([e for e in events if e['type']=='claim']),2)
        self.assertTrue(primary.closed)

    def test_partial_escapes_and_embedded_claims_key_are_not_executable_structure(self):
        value=claim('A quoted "]} claims [" string with a backslash \\ and µ.')
        whole=json.dumps({'status':'answered','claims':[value]},ensure_ascii=False)
        for i in range(len(whole)):
            status,claims=completed_claims(whole[:i])
            self.assertLessEqual(len(claims),1)
            if claims:self.assertEqual(claims[0],value)
        self.assertEqual(completed_claims(whole),('answered',[value]))

    def test_unsupported_numbers_unknown_quotes_and_duplicate_keys_never_stream(self):
        for raw in [json.dumps({'status':'answered','claims':[claim('Contact team 912.')]}),
                    json.dumps({'status':'answered','claims':[claim(quote='UNKNOWN')]}),
                    '{"status":"answered","status":"answered","claims":['+json.dumps(claim())+']}',
                    '{"status":"answered","claims":[{"text":"Safe","text":"Bad","supports":[{"quote_id":"E1Q1"}]}]}']:
            events=[]; primary=StreamFixture(lambda:iter([raw])); fallback=provider('ollama')
            result=generate_answer('Who?',report(),lambda _:True,primary=primary,fallback=fallback,on_event=events.append)
            self.assertEqual(result['status'],'answer_validation_failed')
            self.assertFalse([e for e in events if e['type']=='claim'])
            self.assertTrue(primary.closed)
            fallback.generate.assert_not_called()

    def test_late_invalid_suffix_is_a_failure_not_a_saved_success(self):
        events=[]
        primary=StreamFixture(lambda:iter(['{"status":"answered","claims":['+json.dumps(claim()), '],"unexpected":true}']))
        result=generate_answer('Who?',report(),lambda _:True,primary=primary,on_event=events.append)
        self.assertTrue([e for e in events if e['type']=='claim'])
        self.assertEqual(result['status'],'answer_validation_failed')
        self.assertEqual(result['claims'],[])

    def test_status_can_arrive_after_claims_without_releasing_an_insufficient_answer(self):
        for status in ('answered','insufficient_evidence'):
            raw='{"claims":['+json.dumps(claim())+'],"status":'+json.dumps(status)+'}'
            events=[]
            result=generate_answer('Who?',report(),lambda _:True,primary=StreamFixture(lambda:iter([raw])),on_event=events.append)
            self.assertEqual(bool([e for e in events if e['type']=='claim']),status=='answered')
            self.assertEqual(result['status'],'answered' if status=='answered' else 'answer_validation_failed')

    def test_source_revoked_during_stream_discards_final_claims(self):
        available=True; events=[]
        def parts():
            nonlocal available
            yield '{"status":"answered","claims":['+json.dumps(claim())+','
            available=False
            yield json.dumps(claim('Example Team is the contact.'))+']}'
        result=generate_answer('Who?',report(),lambda _:available,primary=StreamFixture(parts),on_event=events.append)
        self.assertEqual(result['status'],'evidence_changed')
        self.assertEqual(result['claims'],[])
        self.assertEqual(len([e for e in events if e['type']=='claim']),1)

    def test_cancellation_closes_provider_without_fallback(self):
        stopped=False
        def cancel_on_claim(event):
            nonlocal stopped
            if event['type']=='claim':stopped=True
        primary=StreamFixture(lambda:iter(['{"status":"answered","claims":['+json.dumps(claim())+',',json.dumps(claim())+']}']))
        fallback=provider('ollama')
        with self.assertRaises(AnswerCancelled):
            generate_answer('Who?',report(),lambda _:True,primary=primary,fallback=fallback,
                            on_event=cancel_on_claim,cancelled=lambda:stopped)
        self.assertTrue(primary.closed)
        fallback.generate.assert_not_called()

    def test_operational_fallback_sends_reset_before_new_claims(self):
        events=[]
        def outage():
            yield '{"status":"answered","claims":['+json.dumps(claim())+','
            raise AnswerProviderUnavailable()
        fallback=StreamFixture(lambda:iter([json.dumps({'status':'answered','claims':[claim()]})]))
        fallback.name='ollama'
        result=generate_answer('Who?',report(),lambda _:True,primary=StreamFixture(outage),fallback=fallback,on_event=events.append)
        self.assertEqual(result['status'],'answered')
        self.assertTrue(result['fallback_used'])
        self.assertEqual([e['type'] for e in events],['status','claim','reset','status','claim'])

    def test_overlong_stream_is_bounded(self):
        result=generate_answer('Who?',report(),lambda _:True,primary=StreamFixture(lambda:iter([' '*128001])),on_event=lambda _:None)
        self.assertEqual(result['status'],'answer_validation_failed')
        with self.assertRaises(ValueError):strict_loads('{"status":1,"status":2}')


class StreamingAdapterTests(TestCase):
    def test_cohere_sdk_deltas_exclude_thinking_and_require_complete_end(self):
        events=[NS(type='content-delta',delta=NS(message=NS(content=NS(thinking='SECRET_THOUGHT',text=None)))),
                NS(type='content-delta',delta=NS(message=NS(content=NS(text='{"status":')))),
                NS(type='content-delta',delta=NS(message=NS(content=NS(text='"insufficient_evidence","claims":[]}')))),
                NS(type='message-end',delta=NS(finish_reason='COMPLETE'))]
        with patch.dict(os.environ,{'COHERE_API_KEY':'synthetic'}),patch('cohere.ClientV2') as factory:
            factory.return_value.chat_stream.return_value=iter(events)
            raw=''.join(CommandAnswers(AnswerSettings()).generate_stream('Synthetic',[]))
            self.assertNotIn('SECRET_THOUGHT',raw)
            self.assertEqual(json.loads(raw)['status'],'insufficient_evidence')
            self.assertEqual(factory.return_value.chat_stream.call_args.kwargs['temperature'],0)
            factory.return_value.chat_stream.return_value=iter(events[:-1])
            with self.assertRaises(InvalidAnswer):list(CommandAnswers(AnswerSettings()).generate_stream('Synthetic',[]))

    def test_ollama_stream_fixed_endpoint_and_final_marker(self):
        with patch('gsme_rag.generation.answers.httpx.Client') as factory:
            client=factory.return_value.__enter__.return_value
            response=client.stream.return_value.__enter__.return_value
            response.iter_lines.return_value=iter([json.dumps({'message':{'content':'{}'}}),json.dumps({'done':True,'done_reason':'stop'})])
            self.assertEqual(''.join(OllamaAnswers(AnswerSettings()).generate_stream('Synthetic',[])),'{}')
            call=client.stream.call_args
            self.assertEqual(call.args,('POST','http://127.0.0.1:11434/api/chat'))
            self.assertTrue(call.kwargs['json']['stream'])
            self.assertFalse(factory.call_args.kwargs['trust_env'])
            response.iter_lines.return_value=iter([json.dumps({'message':{'content':'{}'}})])
            with self.assertRaises(InvalidAnswer):list(OllamaAnswers(AnswerSettings()).generate_stream('Synthetic',[]))

    def test_gemini_sse_protocol_uses_key_header_and_ignores_thoughts(self):
        env={'GSME_GEMINI_CONTENT_APPROVED':'yes','GSME_EXTERNAL_CONTENT_APPROVED':'yes','GEMINI_API_KEY':'synthetic'}
        with patch.dict(os.environ,env),patch('gsme_rag.generation.gemini.httpx.Client') as factory:
            client=factory.return_value.__enter__.return_value
            response=client.stream.return_value.__enter__.return_value
            response.status_code=200;response.is_success=True
            response.iter_lines.return_value=iter(['data: '+json.dumps({'candidates':[{'content':{'parts':[
                {'text':'SECRET_THOUGHT','thought':True},{'text':'{}'}]},'finishReason':'STOP'}]}),''])
            adapter=GeminiAnswers(AnswerSettings(primary_provider='gemini',gemini_model='gemini-fixture'))
            self.assertEqual(''.join(adapter.generate_stream('Synthetic',[])),'{}')
            self.assertTrue(client.stream.call_args.args[1].endswith(':streamGenerateContent?alt=sse'))
            self.assertNotIn('synthetic',client.stream.call_args.args[1])
            self.assertEqual(client.stream.call_args.kwargs['headers']['x-goog-api-key'],'synthetic')
            response.iter_lines.return_value=iter(['data: '+json.dumps({'candidates':[{'finishReason':'MAX_TOKENS'}]}),''])
            with self.assertRaises(InvalidAnswer):list(adapter.generate_stream('Synthetic',[]))


class HostedStreamTests(TestCase):
    def setUp(self):
        self.store=Mock();self.store.connect.return_value=MagicMock()
        self.scoped=Mock(principal=Principal('google:synthetic'))
        self.scoped.is_source_current_approved.return_value=True
        self.scoped.approved_source_keys.side_effect=lambda keys:keys
        self.settings=Settings('unused','unused',Path('data'))

    def test_stream_endpoint_sends_claim_then_final_citations_and_keeps_request_id(self):
        primary=StreamFixture(lambda:iter([json.dumps({'status':'answered','claims':[claim()]})]))
        with patch('gsme_rag.hosted.app.AccessStore',return_value=self.scoped), \
             patch('gsme_rag.hosted.app.require_answer_permission'), \
             patch('gsme_rag.hosted.app.retrieve',return_value={**report(),'status':'results','rerank_input_count':2,'timings_ms':{}}), \
             patch('gsme_rag.generation.answers.CommandAnswers',return_value=primary):
            app=create_hosted_app(self.settings,store=self.store,providers=lambda:(Mock(),Mock(),Mock()),auto_worker=False)
            with TestClient(app,base_url='http://127.0.0.1:8020') as client:
                response=client.post('/api/chat/stream',json={'question':'Who?'})
        self.assertEqual(response.status_code,200)
        self.assertIn('text/event-stream',response.headers['content-type'])
        frames=[f for f in response.text.split('\n\n') if f.startswith('event:')]
        self.assertTrue(any(f.startswith('event: claim') for f in frames))
        last=json.loads(frames[-1].split('data: ',1)[1])['result']
        self.assertEqual(last['answer']['status'],'answered')
        self.assertEqual(last['request_id'],response.headers['X-GSME-Request-ID'])
        self.assertEqual(len(last['answer_sources']),1)

    def test_stream_error_is_actionable_and_does_not_leak_provider_payload(self):
        from gsme_rag.storage.providers import ProviderOperationError
        with patch('gsme_rag.hosted.app.AccessStore',return_value=self.scoped), \
             patch('gsme_rag.hosted.app.require_answer_permission',side_effect=ProviderOperationError('PRIVATE_PROVIDER_PAYLOAD',error_code='quota_exhausted')):
            with TestClient(create_hosted_app(self.settings,store=self.store,auto_worker=False),base_url='http://127.0.0.1:8020') as client:
                response=client.post('/api/chat/stream',json={'question':'Who?'})
        self.assertIn('event: error',response.text)
        self.assertIn('quota_exhausted',response.text)
        self.assertNotIn('PRIVATE_PROVIDER_PAYLOAD',response.text)

    def test_source_revoked_while_claim_is_queued_is_not_transmitted(self):
        self.scoped.approved_source_keys.side_effect=lambda keys:set()
        primary=StreamFixture(lambda:iter([json.dumps({'status':'answered','claims':[claim()]})]))
        with patch('gsme_rag.hosted.app.AccessStore',return_value=self.scoped), \
             patch('gsme_rag.hosted.app.require_answer_permission'), \
             patch('gsme_rag.hosted.app.retrieve',return_value={**report(),'status':'results','rerank_input_count':2,'timings_ms':{}}), \
             patch('gsme_rag.generation.answers.CommandAnswers',return_value=primary):
            with TestClient(create_hosted_app(self.settings,store=self.store,providers=lambda:(Mock(),Mock(),Mock()),auto_worker=False),
                            base_url='http://127.0.0.1:8020') as client:
                response=client.post('/api/chat/stream',json={'question':'Who?'})
        self.assertNotIn('Contact Example Team',response.text)
        self.assertNotIn('event: claim',response.text)
        self.assertIn('evidence_changed',response.text)

    def test_idp_interpretation_is_explicit_and_does_not_require_choosing_a_file(self):
        with patch('gsme_rag.hosted.app.AccessStore',return_value=self.scoped), \
             patch('gsme_rag.hosted.app.require_answer_permission'), \
             patch('gsme_rag.hosted.app.retrieve',return_value={**report(),'status':'results','rerank_input_count':2,'timings_ms':{}}) as retrieval, \
             patch('gsme_rag.hosted.app.generate_answer',return_value={'status':'answered','text':'Source-supported comparison.','claims':[],'citations':[]}):
            with TestClient(create_hosted_app(self.settings,store=self.store,providers=lambda:(Mock(),Mock(),Mock()),auto_worker=False),base_url='http://127.0.0.1:8020') as client:
                response=client.post('/api/chat',json={'question':'Fabless vs IDP'})
        self.assertEqual(retrieval.call_args.args[6],'Fabless vs IDM')
        self.assertIn('Assuming you mean IDM',response.json()['answer']['text'])
        self.assertEqual(response.json()['answer']['question_interpretation']['original_question'],'Fabless vs IDP')
        self.assertNotIn('choose a document',response.json()['answer']['text'])

    def test_real_http_receives_claim_before_provider_completion(self):
        from fastapi import FastAPI, Request
        from gsme_rag.hosted.streaming import answer_stream
        release,finished=Event(),Event()
        app=FastAPI()
        def run(publish,cancelled):
            publish({'type':'claim','index':0,'text':'Synthetic checked claim.'})
            release.wait(5)
            finished.set()
            return {'answer':{'status':'answered','text':'Synthetic checked claim.'},'results':[]}
        @app.post('/stream')
        def stream(request:Request):
            return answer_stream(run,request,lambda _:{'detail':'Safe error'},lambda:None)
        with local_server(app) as origin:
            try:
                with httpx.Client(timeout=10,trust_env=False) as client, client.stream('POST',origin+'/stream') as response:
                    iterator=response.iter_lines()
                    for line in iterator:
                        if line=='event: claim':break
                    else:self.fail('No claim received over the real HTTP stream')
                    self.assertFalse(finished.is_set(),'Transport buffered the entire reply')
                    release.set()
                    self.assertIn('event: done','\n'.join(iterator))
            finally:release.set()

    def test_real_http_disconnect_does_not_save_a_partial_turn(self):
        # Use the actual chat wrapper and transport; mock only source/model I/O.
        from gsme_rag.hosted.app import HostedSettings
        stopped=Event();history=Mock()
        def waiting_answer(*args,**kwargs):
            kwargs['on_event']({'type':'claim','index':0,'text':'Synthetic checked claim.','source_refs':[]})
            deadline=time.monotonic()+8
            while not kwargs['cancelled']() and time.monotonic()<deadline:time.sleep(.02)
            stopped.set()
            return {'status':'answered','text':'Synthetic checked claim.','claims':[],'citations':[]}
        with patch('gsme_rag.hosted.app.AccessStore',return_value=self.scoped), \
             patch('gsme_rag.hosted.app.ChatHistory',return_value=history), \
             patch('gsme_rag.hosted.app.require_answer_permission'), \
             patch('gsme_rag.hosted.app.retrieve',return_value={**report(),'status':'results','rerank_input_count':2,'timings_ms':{}}), \
             patch('gsme_rag.hosted.app.generate_answer',side_effect=waiting_answer):
            app=create_hosted_app(self.settings,store=self.store,providers=lambda:(Mock(),Mock(),Mock()),auto_worker=False)
            # TrustedHost permits 127.0.0.1 independently of this ephemeral port.
            with local_server(app) as origin:
                with httpx.Client(timeout=10,trust_env=False) as client, client.stream('POST',origin+'/api/chat/stream',
                        json={'question':'Synthetic question','chat_id':str(uuid4())}) as response:
                    for line in response.iter_lines():
                        if line=='event: claim':break
                    else:self.fail('No provisional claim received')
                self.assertTrue(stopped.wait(5),'Disconnected worker was not notified')
                history.append.assert_not_called()
