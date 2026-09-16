-- Daily aggregates: the write-side counterpart to the primary datastore's
-- daily_metrics table.
--
-- Why this exists alongside ad_events: the ad networks hand back daily reports,
-- not individual impressions. Storing a pulled report here means the warehouse
-- can be populated today, without inventing one synthetic event per impression
-- (which would also mean inventing a cost allocation for each of them).
-- ad_events stays what it was designed to be - the landing table for a real
-- event stream, once one exists.
--
-- Dimensions are String rather than Enum8 on purpose. The ad_events table
-- declares Enum8 columns, which makes it reject an unrecognised device instead
-- of storing it. For an aggregate table that is the wrong trade: a platform we
-- have not modelled yet should be storable and filterable, not refused at the
-- door. The read side selects on exact values, so a stray value simply never
-- matches a filter.
--
-- ReplacingMergeTree keyed on the same tuple the primary datastore uses as its
-- uniqueness constraint, so replaying a backfill corrects rows in place instead
-- of duplicating them.

CREATE TABLE IF NOT EXISTS ad_optimizer.campaign_daily_metrics
(
    campaign_id    String,
    stat_date      Date,
    creative_id    String DEFAULT '',
    impressions    UInt64,
    clicks         UInt64,
    conversions    UInt64,
    cost           Float64,
    revenue        Float64,
    unique_reach   Nullable(UInt64),
    source         String DEFAULT '',
    updated_at     DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (campaign_id, creative_id, stat_date);

-- The read side filters on a date window and groups by campaign or creative.
-- A skip index on stat_date is cheap insurance once the table grows past the
-- point where the primary key alone no longer prunes enough granules.
ALTER TABLE ad_optimizer.campaign_daily_metrics
    ADD INDEX IF NOT EXISTS idx_daily_stat_date stat_date TYPE minmax GRANULARITY 4;
