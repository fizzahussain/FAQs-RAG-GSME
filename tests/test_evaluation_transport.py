from unittest import TestCase
import httpx
from tools.evaluate_answer_increment import inspect_provider_response


class EvaluationTransportTests(TestCase):
    def test_inspection_consumes_stream_without_preventing_sdk_read(self):
        response = httpx.Response(200, stream=httpx.ByteStream(b'{"finish_reason":"COMPLETE"}'))
        self.assertEqual(inspect_provider_response(response)["finish_reason"], "COMPLETE")
        self.assertEqual(response.json()["finish_reason"], "COMPLETE")
