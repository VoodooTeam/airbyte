#
# Copyright (c) 2023 Airbyte, Inc., all rights reserved.
#

import json
from functools import partial
from typing import Any, Iterable, Mapping
from unittest import skip

import pytest
from facebook_business import FacebookSession
from facebook_business.api import FacebookAdsApi, FacebookAdsApiBatch, FacebookRequest
from source_facebook_marketing.api import MyFacebookAdsApi
from source_facebook_marketing.streams.base_streams import FBMarketingStream


@pytest.fixture(name="mock_batch_responses")
def mock_batch_responses_fixture(requests_mock):
    return partial(requests_mock.register_uri, "POST", f"{FacebookSession.GRAPH}/{FacebookAdsApi.API_VERSION}/")


@pytest.fixture(name="batch")
def batch_fixture(api, mocker):
    batch = FacebookAdsApiBatch(api=api.api)
    mocker.patch.object(batch, "execute", wraps=batch.execute)
    mocker.patch.object(batch, "add_request", wraps=batch.add_request)
    mocker.patch.object(MyFacebookAdsApi, "new_batch", return_value=batch)
    return batch


class SomeTestStream(FBMarketingStream):
    def list_objects(self, params: Mapping[str, Any]) -> Iterable:
        yield from []


class TestBaseStream:
    def test_execute_in_batch_with_few_requests(self, source, api, batch, mock_batch_responses):
        """Should execute single batch if number of requests less than MAX_BATCH_SIZE."""
        mock_batch_responses(
            [
                {
                    "json": [{"body": json.dumps({"name": "creative 1"}), "code": 200, "headers": {}}] * 3,
                }
            ]
        )

        stream = SomeTestStream(source=source, api=api)
        requests = [FacebookRequest("node", "xGET", "endpoint") for _ in range(5)]

        result = list(stream.execute_in_batch(requests))

        assert batch.add_request.call_count == len(requests)
        batch.execute.assert_called_once()
        assert len(result) == 3

    def test_execute_in_batch_with_many_requests(self, source, api, batch, mock_batch_responses):
        """Should execute as many batches as needed if number of requests bigger than MAX_BATCH_SIZE."""
        mock_batch_responses(
            [
                {
                    "json": [{"body": json.dumps({"name": "creative 1"}), "code": 200, "headers": {}}] * 5,
                }
            ]
        )

        stream = SomeTestStream(api=api, source=source)
        requests = [FacebookRequest("node", "GET", "endpoint") for _ in range(50 + 1)]

        result = list(stream.execute_in_batch(requests))

        assert batch.add_request.call_count == len(requests)
        assert batch.execute.call_count == 2
        assert len(result) == 5 * 2

    def test_execute_in_batch_with_retries(self, api, source, batch, mock_batch_responses):
        """Should retry batch execution until succeed"""
        # batch.execute.side_effect = [batch, batch, None]
        mock_batch_responses(
            [
                {
                    "json": [
                        {},
                        {},
                        {"body": json.dumps({"name": "creative 1"}), "code": 200, "headers": {}},
                    ],
                },
                {
                    "json": [
                        {},
                        {"body": json.dumps({"name": "creative 1"}), "code": 200, "headers": {}},
                    ],
                },
                {
                    "json": [
                        {"body": json.dumps({"name": "creative 1"}), "code": 200, "headers": {}},
                    ],
                },
            ]
        )

        stream = SomeTestStream(api=api, source=source)
        requests = [FacebookRequest("node", "GET", "endpoint") for _ in range(3)]

        result = list(stream.execute_in_batch(requests))

        assert batch.add_request.call_count == len(requests)
        assert batch.execute.call_count == 1
        assert len(result) == 3

    def test_execute_in_batch_with_fails(self, source, api, batch, mock_batch_responses):
        """Should fail with exception when any request returns error"""
        mock_batch_responses(
            [
                {
                    "json": [
                        {"body": {"error": {"code": -1}}, "code": 500, "headers": {}},
                        {"body": json.dumps({"name": "creative 1"}), "code": 200, "headers": {}},
                    ],
                }
            ]
        )

        stream = SomeTestStream(source=source, api=api)
        requests = [FacebookRequest("node", "GET", "endpoint") for _ in range(5)]

        with pytest.raises(RuntimeError, match="Batch request failed with response:"):
            list(stream.execute_in_batch(requests))

        assert batch.add_request.call_count == len(requests)
        assert batch.execute.call_count == 1

    @skip("We have a different way to handle batch sizes, this needs more investigation")
    def test_batch_reduce_amount(self, source, api, batch, mock_batch_responses, caplog):
        """Reduce batch size to 1 and finally fail with message"""

        retryable_message = "Please reduce the amount of data you're asking for, then retry your request"
        mock_batch_responses(
            [
                {
                    "json": [
                        {"body": {"error": {"message": retryable_message}}, "code": 500, "headers": {}},
                    ],
                }
            ]
        )

        stream = SomeTestStream(source=source, api=api)
        requests = [FacebookRequest("node", "GET", "endpoint")]
        with pytest.raises(RuntimeError, match="Batch request failed with only 1 request in..."):
            list(stream.execute_in_batch(requests))

        assert batch.add_request.call_count == 7
        assert batch.execute.call_count == 7
        for index, expected_batch_size in enumerate(["25", "13", "7", "4", "2", "1"]):
            assert expected_batch_size in caplog.messages[index]

    def test_execute_in_batch_retry_batch_error(self, source, api, batch, mock_batch_responses):
        """Should retry without exception when any request returns 960 error code"""
        mock_batch_responses(
            [
                {
                    "json": [
                        {"body": json.dumps({"name": "creative 1"}), "code": 200, "headers": {}},
                        {
                            "body": json.dumps(
                                {
                                    "error": {
                                        "message": "Request aborted. This could happen if a dependent request failed or the entire request timed out.",
                                        "type": "FacebookApiException",
                                        "code": 960,
                                        "fbtrace_id": "AWuyQlmgct0a_n64b-D1AFQ",
                                    }
                                }
                            ),
                            "code": 500,
                            "headers": {},
                        },
                        {"body": json.dumps({"name": "creative 3"}), "code": 200, "headers": {}},
                    ],
                },
                {
                    "json": [
                        {"body": json.dumps({"name": "creative 2"}), "code": 200, "headers": {}},
                    ],
                },
            ]
        )

        stream = SomeTestStream(source=source, api=api)
        requests = [FacebookRequest("node", "GET", "endpoint") for _ in range(3)]
        result = list(stream.execute_in_batch(requests))

        assert batch.add_request.call_count == len(requests) + 1
        assert batch.execute.call_count == 2
        assert len(result) == len(requests)



class TestDateTimeValue:
    def test_date_time_value(self):
        record = {
            "bla": "2023-01-19t20:38:59 0000",
            "created_time": "2023-01-19t20:38:59 0000",
            "creation_time": "2023-01-19t20:38:59 0000",
            "updated_time": "2023-01-19t20:38:59 0000",
            "event_time": "2023-01-19t20:38:59 0000",
            "first_fired_time": "2023-01-19t20:38:59 0000",
            "last_fired_time": "2023-01-19t20:38:59 0000",
            "sub_list": [
                {
                    "bla": "2023-01-19t20:38:59 0000",
                    "created_time": "2023-01-19t20:38:59 0000",
                    "creation_time": "2023-01-19t20:38:59 0000",
                    "updated_time": "2023-01-19t20:38:59 0000",
                    "event_time": "2023-01-19t20:38:59 0000",
                    "first_fired_time": "2023-01-19t20:38:59 0000",
                    "last_fired_time": "2023-01-19t20:38:59 0000",
                }
            ],
            "sub_entries1": {
                "sub_entries2": {
                    "bla": "2023-01-19t20:38:59 0000",
                    "created_time": "2023-01-19t20:38:59 0000",
                    "creation_time": "2023-01-19t20:38:59 0000",
                    "updated_time": "2023-01-19t20:38:59 0000",
                    "event_time": "2023-01-19t20:38:59 0000",
                    "first_fired_time": "2023-01-19t20:38:59 0000",
                    "last_fired_time": "2023-01-19t20:38:59 0000",
                }
            },
        }
        FBMarketingStream.fix_date_time(record)
        assert {
                   "bla": "2023-01-19t20:38:59 0000",
                   "created_time": "2023-01-19T20:38:59+0000",
                   "creation_time": "2023-01-19T20:38:59+0000",
                   "updated_time": "2023-01-19T20:38:59+0000",
                   "event_time": "2023-01-19T20:38:59+0000",
                   "first_fired_time": "2023-01-19T20:38:59+0000",
                   "last_fired_time": "2023-01-19T20:38:59+0000",
                   "sub_list": [
                       {
                           "bla": "2023-01-19t20:38:59 0000",
                           "created_time": "2023-01-19T20:38:59+0000",
                           "creation_time": "2023-01-19T20:38:59+0000",
                           "updated_time": "2023-01-19T20:38:59+0000",
                           "event_time": "2023-01-19T20:38:59+0000",
                           "first_fired_time": "2023-01-19T20:38:59+0000",
                           "last_fired_time": "2023-01-19T20:38:59+0000",
                       }
                   ],
                   "sub_entries1": {
                       "sub_entries2": {
                           "bla": "2023-01-19t20:38:59 0000",
                           "created_time": "2023-01-19T20:38:59+0000",
                           "creation_time": "2023-01-19T20:38:59+0000",
                           "updated_time": "2023-01-19T20:38:59+0000",
                           "event_time": "2023-01-19T20:38:59+0000",
                           "first_fired_time": "2023-01-19T20:38:59+0000",
                           "last_fired_time": "2023-01-19T20:38:59+0000",
                       }
                   },
               } == record

