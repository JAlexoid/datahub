import hashlib
import json
import logging
import os
import textwrap
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import (
    Any,
    Callable,
    Collection,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Tuple,
    Union,
)

import humanfriendly
from google.cloud.bigquery import Client as BigQueryClient
from google.cloud.logging_v2.client import Client as GCPLoggingClient
from ratelimiter import RateLimiter

from datahub.configuration.time_window_config import get_time_bucket
from datahub.emitter.mce_builder import make_user_urn
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.api.closeable import Closeable
from datahub.ingestion.api.workunit import MetadataWorkUnit
from datahub.ingestion.source.bigquery_v2.bigquery_audit import (
    BQ_AUDIT_V2,
    AuditEvent,
    AuditLogEntry,
    BigQueryAuditMetadata,
    BigQueryTableRef,
    QueryEvent,
    ReadEvent,
)
from datahub.ingestion.source.bigquery_v2.bigquery_config import BigQueryV2Config
from datahub.ingestion.source.bigquery_v2.bigquery_report import BigQueryV2Report
from datahub.ingestion.source.bigquery_v2.common import (
    BQ_DATE_SHARD_FORMAT,
    BQ_DATETIME_FORMAT,
    _make_gcp_logging_client,
    get_bigquery_client,
)
from datahub.ingestion.source.usage.usage_common import (
    TOTAL_BUDGET_FOR_QUERY_LIST,
    make_usage_workunit,
)
from datahub.metadata.schema_classes import OperationClass, OperationTypeClass
from datahub.utilities.file_backed_collections import ConnectionWrapper, FileBackedDict
from datahub.utilities.perf_timer import PerfTimer

logger: logging.Logger = logging.getLogger(__name__)


# See https://cloud.google.com/java/docs/reference/google-cloud-bigquery/latest/com.google.cloud.bigquery.JobStatistics.QueryStatistics.StatementType
# https://pkg.go.dev/google.golang.org/genproto/googleapis/cloud/audit may be more complete
OPERATION_STATEMENT_TYPES = {
    "INSERT": OperationTypeClass.INSERT,
    "UPDATE": OperationTypeClass.UPDATE,
    "DELETE": OperationTypeClass.DELETE,
    "MERGE": OperationTypeClass.UPDATE,
    "CREATE": OperationTypeClass.CREATE,
    "CREATE_TABLE_AS_SELECT": OperationTypeClass.CREATE,
    "CREATE_EXTERNAL_TABLE": OperationTypeClass.CREATE,
    "CREATE_SNAPSHOT_TABLE": OperationTypeClass.CREATE,
    "CREATE_VIEW": OperationTypeClass.CREATE,
    "CREATE_MATERIALIZED_VIEW": OperationTypeClass.CREATE,
    "CREATE_SCHEMA": OperationTypeClass.CREATE,
    "DROP_TABLE": OperationTypeClass.DROP,
    "DROP_EXTERNAL_TABLE": OperationTypeClass.DROP,
    "DROP_SNAPSHOT_TABLE": OperationTypeClass.DROP,
    "DROP_VIEW": OperationTypeClass.DROP,
    "DROP_MATERIALIZED_VIEW": OperationTypeClass.DROP,
    "DROP_SCHEMA": OperationTypeClass.DROP,
    "ALTER_TABLE": OperationTypeClass.ALTER,
    "ALTER_VIEW": OperationTypeClass.ALTER,
    "ALTER_MATERIALIZED_VIEW": OperationTypeClass.ALTER,
    "ALTER_SCHEMA": OperationTypeClass.ALTER,
}

READ_STATEMENT_TYPES: List[str] = ["SELECT"]
STRING_ENCODING = "utf-8"
MAX_QUERY_LENGTH = TOTAL_BUDGET_FOR_QUERY_LIST


@dataclass(frozen=True, order=True)
class OperationalDataMeta:
    statement_type: str
    last_updated_timestamp: int
    actor_email: str
    custom_type: Optional[str] = None


def bigquery_audit_metadata_query_template(
    dataset: str,
    use_date_sharded_tables: bool,
    table_allow_filter: Optional[str] = None,
    limit: Optional[int] = None,
) -> str:
    """
    Receives a dataset (with project specified) and returns a query template that is used to query exported
    v2 AuditLogs containing protoPayloads of type BigQueryAuditMetadata.
    :param dataset: the dataset to query against in the form of $PROJECT.$DATASET
    :param use_date_sharded_tables: whether to read from date sharded audit log tables or time partitioned audit log
           tables
    :param table_allow_filter: regex used to filter on log events that contain the wanted datasets
    :param limit: maximum number of events to query for
    :return: a query template, when supplied start_time and end_time, can be used to query audit logs from BigQuery
    """
    allow_filter = f"""
      AND EXISTS (SELECT *
              from UNNEST(JSON_EXTRACT_ARRAY(protopayload_auditlog.metadataJson,
                                             "$.jobChange.job.jobStats.queryStats.referencedTables")) AS x
              where REGEXP_CONTAINS(x, r'(projects/.*/datasets/.*/tables/{table_allow_filter if table_allow_filter else ".*"})'))
    """

    limit_text = f"limit {limit}" if limit else ""

    shard_condition = ""
    if use_date_sharded_tables:
        from_table = f"`{dataset}.cloudaudit_googleapis_com_data_access_*`"
        shard_condition = (
            """ AND _TABLE_SUFFIX BETWEEN "{start_date}" AND "{end_date}" """
        )
    else:
        from_table = f"`{dataset}.cloudaudit_googleapis_com_data_access`"

    query = f"""
        SELECT
            timestamp,
            logName,
            insertId,
            protopayload_auditlog AS protoPayload,
            protopayload_auditlog.metadataJson AS metadata
        FROM
            {from_table}
        WHERE (
            timestamp >= "{{start_time}}"
            AND timestamp < "{{end_time}}"
        )
        {shard_condition}
        AND (
                (
                    protopayload_auditlog.serviceName="bigquery.googleapis.com"
                    AND JSON_EXTRACT_SCALAR(protopayload_auditlog.metadataJson, "$.jobChange.job.jobStatus.jobState") = "DONE"
                    AND JSON_EXTRACT(protopayload_auditlog.metadataJson, "$.jobChange.job.jobConfig.queryConfig") IS NOT NULL
                    {allow_filter}
                )
            OR
            JSON_EXTRACT_SCALAR(protopayload_auditlog.metadataJson, "$.tableDataRead.reason") = "JOB"
        )
        {limit_text};
    """

    return textwrap.dedent(query)


class BigQueryUsageState(Closeable):
    read_events: FileBackedDict[ReadEvent]
    query_events: FileBackedDict[QueryEvent]
    column_accesses: FileBackedDict[Tuple[str, str]]
    queries: FileBackedDict[str]

    def __init__(self, config: BigQueryV2Config):
        self.conn = ConnectionWrapper()
        self.read_events = FileBackedDict[ReadEvent](
            shared_connection=self.conn,
            tablename="read_events",
            extra_columns={
                "resource": lambda e: str(e.resource),
                "name": lambda e: e.jobName,
                "timestamp": lambda e: get_time_bucket(
                    e.timestamp, config.bucket_duration
                ),
                "user": lambda e: e.actor_email,
            },
            cache_max_size=config.file_backed_cache_size,
            # Evict entire cache to reduce db calls.
            cache_eviction_batch_size=max(int(config.file_backed_cache_size * 0.9), 1),
            delay_index_creation=True,
            should_compress_value=True,
        )
        # Keyed by job_name
        self.query_events = FileBackedDict[QueryEvent](
            shared_connection=self.conn,
            tablename="query_events",
            extra_columns={
                "query": lambda e: e.query,
                "is_read": lambda e: int(e.statementType in READ_STATEMENT_TYPES),
            },
            cache_max_size=config.file_backed_cache_size,
            cache_eviction_batch_size=max(int(config.file_backed_cache_size * 0.9), 1),
            delay_index_creation=True,
            should_compress_value=True,
        )
        # Created just to store column accesses in sqlite for JOIN
        self.column_accesses = FileBackedDict[Tuple[str, str]](
            shared_connection=self.conn,
            tablename="column_accesses",
            extra_columns={"read_event": lambda p: p[0], "field": lambda p: p[1]},
            cache_max_size=config.file_backed_cache_size,
            cache_eviction_batch_size=max(int(config.file_backed_cache_size * 0.9), 1),
            delay_index_creation=True,
        )
        self.queries = FileBackedDict[str](cache_max_size=config.file_backed_cache_size)

    def close(self) -> None:
        self.read_events.close()
        self.query_events.close()
        self.column_accesses.close()
        self.conn.close()

        self.queries.close()

    def create_indexes(self) -> None:
        self.read_events.create_indexes()
        self.query_events.create_indexes()
        self.column_accesses.create_indexes()

    def standalone_events(self) -> Iterable[AuditEvent]:
        query = """
        SELECT r.value, q.value
        FROM read_events r
        LEFT JOIN query_events q ON r.name = q.key
        """
        for read_value, query_value in self.read_events.sql_query_iterator(query):
            read_event = self.read_events.deserializer(read_value)
            query_event = (
                self.query_events.deserializer(query_value) if query_value else None
            )
            yield AuditEvent(read_event=read_event, query_event=query_event)
        for _, query_event in self.query_events.items_snapshot("NOT is_read"):
            yield AuditEvent(query_event=query_event)

    @staticmethod
    def usage_statistics_query(top_n: int) -> str:
        return f"""
        SELECT a.timestamp, a.resource, a.query_count, b.query_freq, c.user_freq, d.column_freq FROM (
            SELECT
                r.timestamp,
                r.resource,
                COUNT(q.query) query_count
            FROM
                read_events r
                INNER JOIN query_events q ON r.name = q.key
            GROUP BY r.timestamp, r.resource
        ) a
        LEFT JOIN (
            SELECT timestamp, resource, json_group_array(json_array(query, query_count)) as query_freq FROM (
                SELECT
                    r.timestamp,
                    r.resource,
                    q.query,
                    COUNT(r.key) as query_count,
                    ROW_NUMBER() over (PARTITION BY r.timestamp, r.resource, q.query ORDER BY COUNT(r.key) DESC, q.query) as rank
                FROM
                    read_events r
                    INNER JOIN query_events q ON r.name = q.key
                GROUP BY r.timestamp, r.resource, q.query
                ORDER BY r.timestamp, r.resource, query_count DESC, q.query
            ) WHERE rank <= {top_n}
            GROUP BY timestamp, resource
        ) b ON a.timestamp = b.timestamp AND a.resource = b.resource
        LEFT JOIN (
            SELECT timestamp, resource, json_group_array(json_array(user, user_count)) as user_freq FROM (
                SELECT
                    r.timestamp,
                    r.resource,
                    r.user,
                    COUNT(r.key) user_count
                FROM
                    read_events r
                GROUP BY r.timestamp, r.resource, r.user
                ORDER BY r.timestamp, r.resource, user_count DESC, r.user
            )
            GROUP BY timestamp, resource
        ) c ON a.timestamp = c.timestamp AND a.resource = c.resource
        LEFT JOIN (
            SELECT timestamp, resource, json_group_array(json_array(column, column_count)) as column_freq FROM (
                SELECT
                    r.timestamp,
                    r.resource,
                    c.field column,
                    COUNT(r.key) column_count
                FROM
                    read_events r
                    INNER JOIN column_accesses c ON r.key = c.read_event
                GROUP BY r.timestamp, r.resource, c.field
                ORDER BY r.timestamp, r.resource, column_count DESC, c.field
            )
            GROUP BY timestamp, resource
        ) d ON a.timestamp = d.timestamp AND a.resource = d.resource
        ORDER BY a.timestamp, a.resource
        """

    @dataclass
    class UsageStatistic:
        timestamp: str
        resource: str
        query_count: int
        query_freq: List[Tuple[str, int]]
        user_freq: List[Tuple[str, int]]
        column_freq: List[Tuple[str, int]]

    def usage_statistics(self, top_n: int) -> Iterator[UsageStatistic]:
        query = self.usage_statistics_query(top_n)
        rows = self.read_events.sql_query_iterator(
            query, refs=[self.query_events, self.column_accesses]
        )
        for row in rows:
            yield self.UsageStatistic(
                timestamp=row["timestamp"],
                resource=row["resource"],
                query_count=row["query_count"],
                query_freq=json.loads(row["query_freq"] or "[]"),
                user_freq=json.loads(row["user_freq"] or "[]"),
                column_freq=json.loads(row["column_freq"] or "[]"),
            )

    def report_disk_usage(self, report: BigQueryV2Report) -> None:
        report.usage_state_size = str(
            {
                "main": humanfriendly.format_size(os.path.getsize(self.conn.filename)),
                "queries": humanfriendly.format_size(
                    os.path.getsize(self.queries._conn.filename)
                ),
            }
        )


class BigQueryUsageExtractor:
    """
    This plugin extracts the following:
    * Statistics on queries issued and tables and columns accessed (excludes views)
    * Aggregation of these statistics into buckets, by day or hour granularity

    :::note
    1. Depending on the compliance policies setup for the bigquery instance, sometimes logging.read permission is not sufficient. In that case, use either admin or private log viewer permission.
    :::
    """

    def __init__(self, config: BigQueryV2Config, report: BigQueryV2Report):
        self.config: BigQueryV2Config = config
        self.report: BigQueryV2Report = report
        # Replace hash of query with uuid if there are hash conflicts
        self.uuid_to_query: Dict[str, str] = {}

    def _is_table_allowed(self, table_ref: Optional[BigQueryTableRef]) -> bool:
        return (
            table_ref is not None
            and self.config.dataset_pattern.allowed(table_ref.table_identifier.dataset)
            and self.config.table_pattern.allowed(table_ref.table_identifier.table)
        )

    def run(
        self, projects: Iterable[str], table_refs: Collection[str]
    ) -> Iterable[MetadataWorkUnit]:
        events = self._get_usage_events(projects)
        yield from self._run(events, table_refs)

    def _run(
        self, events: Iterable[AuditEvent], table_refs: Collection[str]
    ) -> Iterable[MetadataWorkUnit]:
        try:
            with BigQueryUsageState(self.config) as usage_state:
                self._ingest_events(events, table_refs, usage_state)
                usage_state.create_indexes()
                usage_state.report_disk_usage(self.report)

                if self.config.usage.include_operational_stats:
                    yield from self._generate_operational_workunits(
                        usage_state, table_refs
                    )

                yield from self._generate_usage_workunits(usage_state)
                usage_state.report_disk_usage(self.report)
        except Exception as e:
            logger.error("Error processing usage", exc_info=True)
            self.report.report_warning("usage-ingestion", str(e))

    def _ingest_events(
        self,
        events: Iterable[AuditEvent],
        table_refs: Collection[str],
        usage_state: BigQueryUsageState,
    ) -> None:
        """Read log and store events in usage_state."""
        num_aggregated = 0
        for audit_event in events:
            try:
                num_aggregated += self._store_usage_event(
                    audit_event, usage_state, table_refs
                )
            except Exception as e:
                logger.warning(
                    f"Unable to store usage event {audit_event}", exc_info=True
                )
                self._report_error("store-event", e)
        logger.info(f"Total number of events aggregated = {num_aggregated}.")

    def _generate_operational_workunits(
        self, usage_state: BigQueryUsageState, table_refs: Collection[str]
    ) -> Iterable[MetadataWorkUnit]:
        self.report.set_ingestion_stage("*", "Usage Extraction Operational Stats")
        for audit_event in usage_state.standalone_events():
            try:
                operational_wu = self._create_operation_workunit(
                    audit_event, table_refs
                )
                if operational_wu:
                    yield operational_wu
                    self.report.num_operational_stats_workunits_emitted += 1
            except Exception as e:
                logger.warning(
                    f"Unable to generate operation workunit for event {audit_event}",
                    exc_info=True,
                )
                self._report_error("operation-workunit", e)

    def _generate_usage_workunits(
        self, usage_state: BigQueryUsageState
    ) -> Iterable[MetadataWorkUnit]:
        self.report.set_ingestion_stage("*", "Usage Extraction Usage Aggregation")
        top_n = (
            self.config.usage.top_n_queries
            if self.config.usage.include_top_n_queries
            else 0
        )
        for entry in usage_state.usage_statistics(top_n=top_n):
            try:
                query_freq = [
                    (
                        self.uuid_to_query.get(
                            query_hash, usage_state.queries[query_hash]
                        ),
                        count,
                    )
                    for query_hash, count in entry.query_freq
                ]
                yield make_usage_workunit(
                    bucket_start_time=datetime.fromisoformat(entry.timestamp),
                    resource=BigQueryTableRef.from_string_name(entry.resource),
                    query_count=entry.query_count,
                    query_freq=query_freq,
                    user_freq=entry.user_freq,
                    column_freq=entry.column_freq,
                    bucket_duration=self.config.bucket_duration,
                    resource_urn_builder=lambda resource: resource.to_urn(
                        self.config.env
                    ),
                    top_n_queries=self.config.usage.top_n_queries,
                    format_sql_queries=self.config.usage.format_sql_queries,
                )
                self.report.num_usage_workunits_emitted += 1
            except Exception as e:
                logger.warning(
                    f"Unable to generate usage workunit for bucket {entry.timestamp}, {entry.resource}",
                    exc_info=True,
                )
                self._report_error("statistics-workunit", e)

    def _get_usage_events(self, projects: Iterable[str]) -> Iterable[AuditEvent]:
        if self.config.use_exported_bigquery_audit_metadata:
            projects = ["*"]  # project_id not used when using exported metadata

        for project_id in projects:
            with PerfTimer() as timer:
                try:
                    self.report.set_ingestion_stage(
                        project_id, "Usage Extraction Ingestion"
                    )
                    yield from self._get_parsed_bigquery_log_events(project_id)
                except Exception as e:
                    logger.error(
                        f"Error getting usage events for project {project_id}",
                        exc_info=True,
                    )
                    self.report.usage_failed_extraction.append(project_id)
                    self.report.report_warning(f"usage-extraction-{project_id}", str(e))

                self.report.usage_extraction_sec[project_id] = round(
                    timer.elapsed_seconds(), 2
                )

    def _store_usage_event(
        self,
        event: AuditEvent,
        usage_state: BigQueryUsageState,
        table_refs: Collection[str],
    ) -> bool:
        """Stores a usage event in `usage_state` and returns if an event was successfully processed."""
        if event.read_event and (
            self.config.start_time <= event.read_event.timestamp < self.config.end_time
        ):
            resource = event.read_event.resource
            if str(resource) not in table_refs:
                logger.debug(f"Skipping non-existent {resource} from usage")
                self.report.num_usage_resources_dropped += 1
                self.report.report_dropped(str(resource))
                return False
            elif resource.is_temporary_table([self.config.temp_table_dataset_prefix]):
                logger.debug(f"Dropping temporary table {resource}")
                self.report.report_dropped(str(resource))
                return False

            # Use uuid keys to store all entries -- no overwriting
            key = str(uuid.uuid4())
            usage_state.read_events[key] = event.read_event
            for field_read in event.read_event.fieldsRead:
                usage_state.column_accesses[str(uuid.uuid4())] = key, field_read
            return True
        elif event.query_event and event.query_event.job_name:
            query = event.query_event.query[:MAX_QUERY_LENGTH]
            query_hash = hashlib.md5(query.encode(STRING_ENCODING)).hexdigest()
            if usage_state.queries.get(query_hash, query) != query:
                key = str(uuid.uuid4())
                self.uuid_to_query[key] = query
                event.query_event.query = key
                self.report.num_usage_query_hash_collisions += 1
            else:
                usage_state.queries[query_hash] = query
                event.query_event.query = query_hash
            usage_state.query_events[event.query_event.job_name] = event.query_event
            return True
        return False

    def _get_exported_bigquery_audit_metadata(
        self,
        bigquery_client: BigQueryClient,
        allow_filter: str,
        limit: Optional[int] = None,
    ) -> Iterable[BigQueryAuditMetadata]:
        if self.config.bigquery_audit_metadata_datasets is None:
            return

        corrected_start_time = self.config.start_time - self.config.max_query_duration
        start_time = corrected_start_time.strftime(BQ_DATETIME_FORMAT)
        start_date = corrected_start_time.strftime(BQ_DATE_SHARD_FORMAT)
        self.report.audit_start_time = start_time

        corrected_end_time = self.config.end_time + self.config.max_query_duration
        end_time = corrected_end_time.strftime(BQ_DATETIME_FORMAT)
        end_date = corrected_end_time.strftime(BQ_DATE_SHARD_FORMAT)
        self.report.audit_end_time = end_time

        for dataset in self.config.bigquery_audit_metadata_datasets:
            logger.info(
                f"Start loading log entries from BigQueryAuditMetadata in {dataset}"
            )

            query = bigquery_audit_metadata_query_template(
                dataset,
                self.config.use_date_sharded_audit_log_tables,
                allow_filter,
                limit=limit,
            ).format(
                start_time=start_time,
                end_time=end_time,
                start_date=start_date,
                end_date=end_date,
            )

            query_job = bigquery_client.query(query)
            logger.info(
                f"Finished loading log entries from BigQueryAuditMetadata in {dataset}"
            )
            if self.config.rate_limit:
                with RateLimiter(max_calls=self.config.requests_per_min, period=60):
                    yield from query_job
            else:
                yield from query_job

    def _get_bigquery_log_entries_via_gcp_logging(
        self, client: GCPLoggingClient, limit: Optional[int] = None
    ) -> Iterable[AuditLogEntry]:

        filter = self._generate_filter(BQ_AUDIT_V2)
        logger.debug(filter)

        list_entries: Iterable[AuditLogEntry]
        rate_limiter: Optional[RateLimiter] = None
        if self.config.rate_limit:
            # client.list_entries is a generator, does api calls to GCP Logging when it runs out of entries and needs to fetch more from GCP Logging
            # to properly ratelimit we multiply the page size by the number of requests per minute
            rate_limiter = RateLimiter(
                max_calls=self.config.requests_per_min * self.config.log_page_size,
                period=60,
            )

        list_entries = client.list_entries(
            filter_=filter,
            page_size=self.config.log_page_size,
            max_results=limit,
        )

        for i, entry in enumerate(list_entries):
            if i == 0:
                logger.info(f"Starting log load from GCP Logging for {client.project}")
            if i % 1000 == 0:
                logger.info(f"Loaded {i} log entries from GCP Log for {client.project}")
            self.report.total_query_log_entries += 1

            if rate_limiter:
                with rate_limiter:
                    yield entry
            else:
                yield entry

        logger.info(
            f"Finished loading {self.report.total_query_log_entries} log entries from GCP Logging for {client.project}"
        )

    def _generate_filter(self, audit_templates: Dict[str, str]) -> str:
        # We adjust the filter values a bit, since we need to make sure that the join
        # between query events and read events is complete. For example, this helps us
        # handle the case where the read happens within our time range but the query
        # completion event is delayed and happens after the configured end time.
        # Can safely access the first index of the allow list as it by default contains ".*"
        use_allow_filter = self.config.table_pattern and (
            len(self.config.table_pattern.allow) > 1
            or self.config.table_pattern.allow[0] != ".*"
        )
        use_deny_filter = self.config.table_pattern and self.config.table_pattern.deny
        allow_regex = (
            audit_templates["BQ_FILTER_REGEX_ALLOW_TEMPLATE"].format(
                table_allow_pattern=self.config.get_table_pattern(
                    self.config.table_pattern.allow
                )
            )
            if use_allow_filter
            else ""
        )
        deny_regex = (
            audit_templates["BQ_FILTER_REGEX_DENY_TEMPLATE"].format(
                table_deny_pattern=self.config.get_table_pattern(
                    self.config.table_pattern.deny
                ),
                logical_operator="AND" if use_allow_filter else "",
            )
            if use_deny_filter
            else ("" if use_allow_filter else "FALSE")
        )

        logger.debug(
            f"use_allow_filter={use_allow_filter}, use_deny_filter={use_deny_filter}, "
            f"allow_regex={allow_regex}, deny_regex={deny_regex}"
        )
        start_time = (self.config.start_time - self.config.max_query_duration).strftime(
            BQ_DATETIME_FORMAT
        )
        self.report.log_entry_start_time = start_time
        end_time = (self.config.end_time + self.config.max_query_duration).strftime(
            BQ_DATETIME_FORMAT
        )
        self.report.log_entry_end_time = end_time
        filter = audit_templates["BQ_FILTER_RULE_TEMPLATE"].format(
            start_time=start_time,
            end_time=end_time,
            allow_regex=allow_regex,
            deny_regex=deny_regex,
        )
        return filter

    @staticmethod
    def _get_destination_table(event: AuditEvent) -> Optional[BigQueryTableRef]:
        if (
            not event.read_event
            and event.query_event
            and event.query_event.destinationTable
        ):
            return event.query_event.destinationTable
        elif event.read_event:
            return event.read_event.resource
        else:
            # TODO: CREATE_SCHEMA operation ends up here, maybe we should capture that as well
            # but it is tricky as we only get the query so it can't be tied to anything
            # - SCRIPT statement type ends up here as well
            logger.debug(f"Unable to find destination table in event {event}")
            return None

    def _extract_operational_meta(
        self, event: AuditEvent
    ) -> Optional[OperationalDataMeta]:
        # If we don't have Query object that means this is a queryless read operation or a read operation which was not executed as JOB
        # https://cloud.google.com/bigquery/docs/reference/auditlogs/rest/Shared.Types/BigQueryAuditMetadata.TableDataRead.Reason/
        if not event.query_event and event.read_event:
            return OperationalDataMeta(
                statement_type=OperationTypeClass.CUSTOM,
                custom_type="CUSTOM_READ",
                last_updated_timestamp=int(
                    event.read_event.timestamp.timestamp() * 1000
                ),
                actor_email=event.read_event.actor_email,
            )
        elif event.query_event:
            custom_type = None
            # If AuditEvent only have queryEvent that means it is the target of the Insert Operation
            if (
                event.query_event.statementType in OPERATION_STATEMENT_TYPES
                and not event.read_event
            ):
                statement_type = OPERATION_STATEMENT_TYPES[
                    event.query_event.statementType
                ]
            # We don't have SELECT in OPERATION_STATEMENT_TYPES , so those queries will end up here
            # and this part should capture those operation types as well which we don't have in our mapping
            else:
                statement_type = OperationTypeClass.CUSTOM
                custom_type = event.query_event.statementType

            self.report.operation_types_stat[event.query_event.statementType] = (
                self.report.operation_types_stat.get(event.query_event.statementType, 0)
                + 1
            )
            return OperationalDataMeta(
                statement_type=statement_type,
                custom_type=custom_type,
                last_updated_timestamp=int(
                    event.query_event.timestamp.timestamp() * 1000
                ),
                actor_email=event.query_event.actor_email,
            )
        else:
            return None

    def _create_operation_workunit(
        self, event: AuditEvent, table_refs: Collection[str]
    ) -> Optional[MetadataWorkUnit]:
        if not event.read_event and not event.query_event:
            return None

        destination_table = self._get_destination_table(event)
        if destination_table is None:
            return None

        if (
            not self._is_table_allowed(destination_table)
            or str(destination_table) not in table_refs
        ):
            logger.debug(
                f"Filtering out operation {event.query_event}: invalid destination {destination_table}."
            )
            self.report.num_usage_operations_dropped += 1
            return None

        operational_meta = self._extract_operational_meta(event)
        if not operational_meta:
            return None

        if not self.config.usage.include_read_operational_stats and (
            operational_meta.statement_type not in OPERATION_STATEMENT_TYPES.values()
        ):
            return None

        reported_time: int = int(time.time() * 1000)
        affected_datasets = []
        if event.query_event and event.query_event.referencedTables:
            for table in event.query_event.referencedTables:
                affected_datasets.append(table.to_urn(self.config.env))

        operation_aspect = OperationClass(
            timestampMillis=reported_time,
            lastUpdatedTimestamp=operational_meta.last_updated_timestamp,
            actor=make_user_urn(operational_meta.actor_email.split("@")[0]),
            operationType=operational_meta.statement_type,
            customOperationType=operational_meta.custom_type,
            affectedDatasets=affected_datasets,
        )

        if self.config.usage.include_read_operational_stats:
            operation_aspect.customProperties = (
                self._create_operational_custom_properties(event)
            )
            if event.query_event and event.query_event.numAffectedRows:
                operation_aspect.numAffectedRows = event.query_event.numAffectedRows

        return MetadataChangeProposalWrapper(
            entityUrn=destination_table.to_urn(env=self.config.env),
            aspect=operation_aspect,
        ).as_workunit()

    def _create_operational_custom_properties(
        self, event: AuditEvent
    ) -> Dict[str, str]:
        custom_properties: Dict[str, str] = {}
        # This only needs for backward compatibility reason. To make sure we generate the same operational metadata than before
        if self.config.usage.include_read_operational_stats:
            if event.query_event:
                if event.query_event.end_time and event.query_event.start_time:
                    custom_properties["millisecondsTaken"] = str(
                        int(event.query_event.end_time.timestamp() * 1000)
                        - int(event.query_event.start_time.timestamp() * 1000)
                    )

                if event.query_event.job_name:
                    custom_properties["sessionId"] = event.query_event.job_name

                custom_properties["text"] = event.query_event.query

                if event.query_event.billed_bytes:
                    custom_properties["bytesProcessed"] = str(
                        event.query_event.billed_bytes
                    )

                if event.query_event.default_dataset:
                    custom_properties[
                        "defaultDatabase"
                    ] = event.query_event.default_dataset
            if event.read_event:
                if event.read_event.readReason:
                    custom_properties["readReason"] = event.read_event.readReason

                if event.read_event.fieldsRead:
                    custom_properties["fieldsRead"] = ",".join(
                        event.read_event.fieldsRead
                    )

        return custom_properties

    def _parse_bigquery_log_entry(
        self, entry: Union[AuditLogEntry, BigQueryAuditMetadata]
    ) -> Optional[AuditEvent]:
        event: Optional[Union[ReadEvent, QueryEvent]] = None

        missing_read_entry = ReadEvent.get_missing_key_entry(entry)
        if missing_read_entry is None:
            event = ReadEvent.from_entry(entry, self.config.debug_include_full_payloads)
            if not self._is_table_allowed(event.resource):
                self.report.num_filtered_read_events += 1
                return None

            if event.readReason:
                self.report.read_reasons_stat[event.readReason] += 1
            self.report.num_read_events += 1

        missing_query_entry = QueryEvent.get_missing_key_entry(entry)
        if event is None and missing_query_entry is None:
            event = QueryEvent.from_entry(entry)
            self.report.num_query_events += 1

        missing_query_entry_v2 = QueryEvent.get_missing_key_entry_v2(entry)

        if event is None and missing_query_entry_v2 is None:
            event = QueryEvent.from_entry_v2(
                entry, self.config.debug_include_full_payloads
            )
            self.report.num_query_events += 1

        if event is None:
            logger.warning(
                f"Unable to parse {type(entry)} missing read {missing_read_entry}, "
                f"missing query {missing_query_entry} missing v2 {missing_query_entry_v2} for {entry}"
            )
            return None

        return AuditEvent.create(event)

    def _parse_exported_bigquery_audit_metadata(
        self, audit_metadata: BigQueryAuditMetadata
    ) -> Optional[AuditEvent]:
        event: Optional[Union[ReadEvent, QueryEvent]] = None

        missing_read_event = ReadEvent.get_missing_key_exported_bigquery_audit_metadata(
            audit_metadata
        )
        if missing_read_event is None:
            event = ReadEvent.from_exported_bigquery_audit_metadata(
                audit_metadata, self.config.debug_include_full_payloads
            )
            if not self._is_table_allowed(event.resource):
                self.report.num_filtered_read_events += 1
                return None
            if event.readReason:
                self.report.read_reasons_stat[event.readReason] += 1
            self.report.num_read_events += 1

        missing_query_event = (
            QueryEvent.get_missing_key_exported_bigquery_audit_metadata(audit_metadata)
        )
        if event is None and missing_query_event is None:
            event = QueryEvent.from_exported_bigquery_audit_metadata(
                audit_metadata, self.config.debug_include_full_payloads
            )
            self.report.num_query_events += 1

        if event is None:
            logger.warning(
                f"{audit_metadata['logName']}-{audit_metadata['insertId']} "
                f"Unable to parse audit metadata missing QueryEvent keys:{str(missing_query_event)} "
                f"ReadEvent keys: {str(missing_read_event)} for {audit_metadata}"
            )
            return None

        return AuditEvent.create(event)

    def _get_parsed_bigquery_log_events(
        self, project_id: str, limit: Optional[int] = None
    ) -> Iterable[AuditEvent]:
        parse_fn: Callable[[Any], Optional[AuditEvent]]
        if self.config.use_exported_bigquery_audit_metadata:
            bq_client = get_bigquery_client(self.config)
            entries = self._get_exported_bigquery_audit_metadata(
                bigquery_client=bq_client,
                allow_filter=self.config.get_table_pattern(
                    self.config.table_pattern.allow
                ),
                limit=limit,
            )
            parse_fn = self._parse_exported_bigquery_audit_metadata
        else:
            logging_client = _make_gcp_logging_client(
                project_id, self.config.extra_client_options
            )
            entries = self._get_bigquery_log_entries_via_gcp_logging(
                logging_client, limit=limit
            )
            parse_fn = self._parse_bigquery_log_entry

        for entry in entries:
            try:
                event = parse_fn(entry)
                if event:
                    yield event
            except Exception as e:
                logger.warning(
                    f"Unable to parse log entry `{entry}` for project {project_id}",
                    exc_info=True,
                )
                self._report_error(
                    f"log-parse-{project_id}", e, group="usage-log-parse"
                )

    def _report_error(
        self, label: str, e: Exception, group: Optional[str] = None
    ) -> None:
        """Report an error that does not constitute a major failure."""
        self.report.usage_error_count[label] += 1
        self.report.report_warning(group or f"usage-{label}", str(e))

    def test_capability(self, project_id: str) -> None:
        for entry in self._get_parsed_bigquery_log_events(project_id, limit=1):
            logger.debug(f"Connection test got one {entry}")
            return
