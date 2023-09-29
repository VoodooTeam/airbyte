#
# Copyright (c) 2023 Airbyte, Inc., all rights reserved.
#
import itertools
import json
import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import TYPE_CHECKING, Any, Iterable, List, Mapping, MutableMapping, Optional, Dict
from functools import partial
from math import ceil
from queue import Queue
from typing import TYPE_CHECKING, Any, Iterable, List, Mapping, MutableMapping, Optional


import gevent
import pendulum
from airbyte_cdk.models import SyncMode
from airbyte_cdk.sources import AbstractSource
from airbyte_cdk.sources.streams import Stream
from airbyte_cdk.sources.streams.availability_strategy import AvailabilityStrategy
from airbyte_cdk.sources.utils.transform import TransformConfig, TypeTransformer
from cached_property import cached_property
from facebook_business.adobjects.abstractobject import AbstractObject
from facebook_business.exceptions import FacebookRequestError
from source_facebook_marketing.streams.common import traced_exception
from facebook_business.api import FacebookAdsApiBatch, FacebookRequest, FacebookResponse

from .common import deep_merge

if TYPE_CHECKING:  # pragma: no cover
    from source_facebook_marketing.api import API


logger = logging.getLogger("airbyte")

FACEBOOK_BATCH_ERROR_CODE = 960
FACEBOOK_PERMISSIONS_ERROR_CODE = 200
TEST_ERROR_CODE = -1
IGNORED_ERRORS = [FACEBOOK_PERMISSIONS_ERROR_CODE, TEST_ERROR_CODE]


class FBMarketingStream(Stream, ABC):
    """Base stream class"""

    primary_key = "id"
    transformer: TypeTransformer = TypeTransformer(TransformConfig.DefaultSchemaNormalization)

    # use batch API to retrieve details for each record in a stream
    use_batch = True
    # this flag will override `include_deleted` option for streams that does not support it
    enable_deleted = True
    # entity prefix for `include_deleted` filter, it usually matches singular version of stream name
    entity_prefix = None
    # In case of Error 'Too much data was requested in batch' some fields should be removed from request
    fields_exceptions = []

    @property
    def availability_strategy(self) -> Optional["AvailabilityStrategy"]:
        return None

    def __init__(self, source: AbstractSource, api: "API", include_deleted: bool = False, page_size: int = 100, **kwargs):
        super().__init__(**kwargs)
        self._source = source
        self._api = api
        self._token_hash = api.token_hash
        self.page_size = page_size if page_size is not None else 100
        self._include_deleted = include_deleted if self.enable_deleted else False
        self.max_batch_size = 50

    @cached_property
    def fields(self) -> List[str]:
        """List of fields that we want to query, for now just all properties from stream's schema"""
        return list(self.get_json_schema().get("properties", {}).keys())


    @classmethod
    def fix_date_time(cls, record):
        date_time_fields = (
            "created_time",
            "creation_time",
            "updated_time",
            "event_time",
            "start_time",
            "first_fired_time",
            "last_fired_time",
        )

        if isinstance(record, dict):
            for field, value in record.items():
                if isinstance(value, str):
                    if field in date_time_fields:
                        record[field] = value.replace("t", "T").replace(" 0000", "+0000")
                else:
                    cls.fix_date_time(value)

        elif isinstance(record, list):
            for entry in record:
                cls.fix_date_time(entry)

    def _execute_batch(self, batch: FacebookAdsApiBatch) -> None:
        """Execute batch, retry in case of failures"""
        while batch:
            batch = batch.execute()
            if batch:
                logger.info("Retry failed requests in batch")

    def execute_batch(self, pending_requests: Iterable[FacebookRequest]) -> Iterable[MutableMapping[str, Any]]:
        """Execute list of requests in batches"""
        requests_q = Queue()
        batch_size = 0
        batch_retries = {}
        records = []
        for r in pending_requests:
            requests_q.put(r)
            batch_size += 1

        def success(response: FacebookResponse):
            self.max_batch_size = 50
            records.append(response.json())

        def reduce_batch_size(request: FacebookRequest):
            if self.max_batch_size == 1 and set(self.fields_exceptions) & set(request._fields):
                logger.warning(
                    f"Removing fields from object {self.name} with id={request._node_id} : {set(self.fields_exceptions) & set(request._fields)}"
                )
                request._fields = [x for x in request._fields if x not in self.fields_exceptions]
            elif self.max_batch_size == 1:
                raise RuntimeError("Batch request failed with only 1 request in it")
            self.max_batch_size = ceil(self.max_batch_size / 2)
            logger.warning(f"Caught retryable error: Too much data was requested in batch. Reducing batch size to {self.max_batch_size}")

        def failure(response: FacebookResponse, request: Optional[FacebookRequest] = None):
            # although it is Optional in the signature for compatibility, we need it always
            assert request, "Missing a request object"
            resp_body = response.json()
            req_path = request._path
            logger.warning(f"Batch request to {req_path} failed (will be retried) with response: {resp_body}")
            if not isinstance(resp_body, dict) or resp_body.get("error", {}).get("code") in IGNORED_ERRORS:
                raise RuntimeError(f"Batch request to {req_path} failed (aborted) with response: {resp_body}")
            elif resp_body.get("error", {}).get("code") != FACEBOOK_BATCH_ERROR_CODE:
                raise RuntimeError(f"Batch request to {req_path} failed (aborted) with response: {resp_body}, unknown error code")
            elif resp_body.get("error", {}).get("message") == "Please reduce the amount of data you're asking for, then retry your request":
                nonlocal batch_size
                # reduce current batch size
                if batch_size > 1:
                    batch_size -= 1
                    logger.debug(f"Reducing batch size to {batch_size}")
                #reduce_batch_size(reques) # this is what happens in the Master branch

            requests_q.put(request)
            # nonlocal batch_size
            # # reduce current batch size
            # if batch_size > 1:
            #     batch_size -= 1
            #     logger.debug(f"Reducing batch size to {batch_size}")

        api_batch: FacebookAdsApiBatch = self._api.api.new_batch()
        while not requests_q.empty():
            request = requests_q.get()
            api_batch.add_request(request, success=success, failure=partial(failure, request=request))
            if requests_q.empty() or len(api_batch) >= batch_size:
                self._execute_batch(api_batch)
                api_batch: FacebookAdsApiBatch = self._api.api.new_batch()
        return records

    def execute_in_batch(self, pending_requests: Iterable[FacebookRequest]) -> Iterable[MutableMapping[str, Any]]:
        requests_q = Queue()
        for r in pending_requests:
            requests_q.put(r)
        jobs = []
        batch = []
        while not requests_q.empty():
            batch.append(requests_q.get())
            # make a batch for every max_batch_size items or less if it is the last call
            if len(batch) == self.max_batch_size or requests_q.empty():
                jobs.append(gevent.spawn(self.execute_batch, batch))
                batch = []
        with gevent.iwait(jobs) as completed_jobs:
            for job in completed_jobs:
                if job.value:
                    yield from job.value
                    # To force eager release of memory
                    job.value.clear()

    @property
    def state_checkpoint_interval(self) -> Optional[int]:
        return 500

    def read_records(
            self,
            sync_mode: SyncMode,
            cursor_field: List[str] = None,
            stream_slice: Mapping[str, Any] = None,
            stream_state: Mapping[str, Any] = None,
    ) -> Iterable[Mapping[str, Any]]:
        """Main read method used by CDK"""
        try:
            records_iter = self.list_objects(params=self.request_params(stream_state=stream_state))
            loaded_records_iter = (record.api_get(fields=self.fields, pending=self.use_batch) for record in records_iter)
            if self.use_batch:
                loaded_records_iter = self.execute_in_batch(loaded_records_iter)

            for record in loaded_records_iter:
                if isinstance(record, AbstractObject):
                    yield record.export_all_data()  # convert FB object to dict
                else:
                    yield record  # execute_in_batch will emmit dicts
        except FacebookRequestError as exc:
            raise traced_exception(exc)




    @abstractmethod
    def list_objects(self, params: Mapping[str, Any]) -> Iterable:
        """List FB objects, these objects will be loaded in read_records later with their details.

        :param params: params to make request
        :return: list of FB objects to load
        """

    def generate_facebook_stream_log(self, log_dict: Dict, previous_unix_time = None):
        log = {
            "source": "facebook-marketing-custom",
            "time": str(datetime.now()),
            "unix_time": time.time(),
            "previous_time_diff_seconds": time.time() - previous_unix_time if previous_unix_time else None
        }
        log.update(log_dict)
        return json.dumps(log)

    def _list_objects(self, api_call_wrapper, **kwargs):
        previous_now = time.time()
        count = 0
        jobs = [gevent.spawn(api_call_wrapper, account=account, **kwargs) for account in self._api.accounts]
        with gevent.iwait(jobs) as completed_jobs:
            for job in completed_jobs:
                if job.exception:
                    raise job.exception
                for value in job.value:
                    count += 1
                    yield value
                job.value.clear()
            logger.info(self.generate_facebook_stream_log({
                "source": self._source.name,
                "token_hash": self._token_hash,
                "stream": self.entity_prefix,
                "accounts_count": len(self._api.accounts),
                "count": count
            }, previous_now))

    def request_params(self, **kwargs) -> MutableMapping[str, Any]:
        """Parameters that should be passed to query_records method"""
        params = {"limit": self.page_size}

        if self._include_deleted:
            params.update(self._filter_all_statuses())

        return params

    def _filter_all_statuses(self) -> MutableMapping[str, Any]:
        """Filter that covers all possible statuses thus including deleted/archived records"""
        filt_values = [
            "active",
            "archived",
            "completed",
            "limited",
            "not_delivering",
            "deleted",
            "not_published",
            "pending_review",
            "permanently_deleted",
            "recently_completed",
            "recently_rejected",
            "rejected",
            "scheduled",
            "inactive",
        ]

        return {
            "filtering": [
                {"field": f"{self.entity_prefix}.delivery_info", "operator": "IN", "value": filt_values},
            ],
        }


class FBMarketingIncrementalStream(FBMarketingStream, ABC):
    """Base class for incremental streams"""

    cursor_field = "updated_time"

    def __init__(self, start_date: Optional[datetime], end_date: Optional[datetime], **kwargs):
        super().__init__(**kwargs)
        self._start_date = pendulum.instance(start_date) if start_date else None
        self._end_date = pendulum.instance(end_date) if end_date else None

    def get_updated_state(self, current_stream_state: MutableMapping[str, Any], latest_record: Mapping[str, Any]):
        """Update stream state from latest record"""
        potentially_new_records_in_the_past = self._include_deleted and not current_stream_state.get("include_deleted", False)
        record_value = latest_record[self.cursor_field]
        state_value = current_stream_state.get(self.cursor_field) or record_value
        max_cursor = max(pendulum.parse(state_value), pendulum.parse(record_value))
        if potentially_new_records_in_the_past:
            max_cursor = record_value

        return {
            self.cursor_field: str(max_cursor),
            "include_deleted": self._include_deleted,
        }

    def request_params(self, stream_state: Mapping[str, Any], **kwargs) -> MutableMapping[str, Any]:
        """Include state filter"""
        params = super().request_params(**kwargs)
        params = deep_merge(params, self._state_filter(stream_state=stream_state or {}))
        return params

    def _state_filter(self, stream_state: Mapping[str, Any]) -> Mapping[str, Any]:
        """Additional filters associated with state if any set"""

        state_value = stream_state.get(self.cursor_field)
        if stream_state:
            filter_value = pendulum.parse(state_value)
        elif self._start_date:
            filter_value = self._start_date
        else:
            # if start_date is not specified then do not use date filters
            return {}

        potentially_new_records_in_the_past = self._include_deleted and not stream_state.get("include_deleted", False)
        if potentially_new_records_in_the_past:
            self.logger.info(f"Ignoring bookmark for {self.name} because of enabled `include_deleted` option")
            if self._start_date:
                filter_value = self._start_date
            else:
                # if start_date is not specified then do not use date filters
                return {}

        return {
            "filtering": [
                {
                    "field": f"{self.entity_prefix}.{self.cursor_field}",
                    "operator": "GREATER_THAN",
                    "value": filter_value.int_timestamp,
                },
            ],
        }


class FBMarketingReversedIncrementalStream(FBMarketingIncrementalStream, ABC):
    """The base class for streams that don't support filtering and return records sorted desc by cursor_value"""

    enable_deleted = False  # API don't have any filtering, so implement include_deleted in code

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._cursor_value = None
        self._max_cursor_value = None

    @property
    def state(self) -> Mapping[str, Any]:
        """State getter, get current state and serialize it to emmit Airbyte STATE message"""
        if self._cursor_value:
            return {
                self.cursor_field: self._cursor_value,
                "include_deleted": self._include_deleted,
            }

        return {}

    @state.setter
    def state(self, value: Mapping[str, Any]):
        """State setter, ignore state if current settings mismatch saved state"""
        if self._include_deleted and not value.get("include_deleted"):
            logger.info(f"Ignoring bookmark for {self.name} because of enabled `include_deleted` option")
            return

        self._cursor_value = pendulum.parse(value[self.cursor_field])

    def _state_filter(self, stream_state: Mapping[str, Any]) -> Mapping[str, Any]:
        """Don't have classic cursor filtering"""
        return {}

    def get_record_deleted_status(self, record) -> bool:
        return False

    def read_records(
            self,
            sync_mode: SyncMode,
            cursor_field: List[str] = None,
            stream_slice: Mapping[str, Any] = None,
            stream_state: Mapping[str, Any] = None,
    ) -> Iterable[Mapping[str, Any]]:
        """Main read method used by CDK
        - save initial state
        - save maximum value (it is the first one)
        - update state only when we reach the end
        - stop reading when we reached the end
        """
        try:
            records_iter = self.list_objects(params=self.request_params(stream_state=stream_state))
            for record in records_iter:
                record_cursor_value = pendulum.parse(record[self.cursor_field])
                if self._cursor_value and record_cursor_value < self._cursor_value:
                    break
                if not self._include_deleted and self.get_record_deleted_status(record):
                    continue

                self._max_cursor_value = max(self._max_cursor_value, record_cursor_value) if self._max_cursor_value else record_cursor_value
                record = record.export_all_data()
                self.fix_date_time(record)
                yield record

            self._cursor_value = self._max_cursor_value
        except FacebookRequestError as exc:
            raise traced_exception(exc)
