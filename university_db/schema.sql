-- One directory of UGC universities and AICTE approved institutions.
-- When the same institution is on both lists (same normalized name and state),
-- the row is the UGC record: UGC supplies the name, address, and status,
-- and the AICTE columns are filled in beside it.
-- An institution that exists only on AICTE keeps its own row.

CREATE DATABASE IF NOT EXISTS university_registry
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE university_registry;

DROP TABLE IF EXISTS institutions;

CREATE TABLE institutions (
  id                    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  name                  VARCHAR(500)    NOT NULL,
  name_key              VARCHAR(500)    NOT NULL,
  state                 VARCHAR(120)    NULL,
  primary_source        ENUM('ugc', 'aicte') NOT NULL,
  in_ugc                TINYINT(1)      NOT NULL DEFAULT 0,
  in_aicte              TINYINT(1)      NOT NULL DEFAULT 0,

  ugc_id                VARCHAR(32)     NULL,
  ugc_type              VARCHAR(80)     NULL,
  address               VARCHAR(1000)   NULL,
  zip                   VARCHAR(80)     NULL,
  ugc_status            VARCHAR(80)     NULL,
  website_url           VARCHAR(1000)   NULL,

  aicte_permanent_id    VARCHAR(32)     NULL,
  aicte_permanent_ids   VARCHAR(1000)   NULL,
  aicte_sr_no           INT             NULL,
  region                VARCHAR(40)     NULL,
  district              VARCHAR(150)    NULL,
  city                  VARCHAR(150)    NULL,
  user_group            VARCHAR(20)     NULL,
  aicte_match_count     INT             NOT NULL DEFAULT 0,

  record_key            VARCHAR(80)     NOT NULL,
  loaded_at             TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,

  PRIMARY KEY (id),
  UNIQUE KEY uq_record_key (record_key),
  KEY idx_name_key (name_key),
  KEY idx_state (state),
  KEY idx_primary_source (primary_source),
  KEY idx_flags (in_ugc, in_aicte),
  KEY idx_ugc_id (ugc_id),
  KEY idx_aicte_permanent_id (aicte_permanent_id)
) ENGINE=InnoDB;
