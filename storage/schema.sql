-- Serpent Circle Hype-Coin Predictive Engine schema.
-- Alembic migration 0001_initial is authoritative; this file is the readable SQL contract.
-- Generated from storage.models metadata. Do not hand-edit.


CREATE TABLE backtest_runs (
	id SERIAL NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE NOT NULL,
	cutoff_start TIMESTAMP WITH TIME ZONE NOT NULL,
	cutoff_end TIMESTAMP WITH TIME ZONE NOT NULL,
	config_json JSON NOT NULL,
	git_sha VARCHAR(64),
	model_version VARCHAR(128) NOT NULL,
	status VARCHAR(32) NOT NULL,
	PRIMARY KEY (id)
);


CREATE TABLE chains (
	id SERIAL NOT NULL,
	slug VARCHAR(32) NOT NULL,
	name VARCHAR(128) NOT NULL,
	vm_type VARCHAR(32) NOT NULL,
	native_symbol VARCHAR(32) NOT NULL,
	finality_profile JSON NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
);


CREATE TABLE narrative_clusters (
	id SERIAL NOT NULL,
	cluster_key VARCHAR(128) NOT NULL,
	seed_topic VARCHAR(256) NOT NULL,
	mention_count INTEGER NOT NULL,
	first_seen_at TIMESTAMP WITH TIME ZONE NOT NULL,
	last_seen_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_narrative_cluster_key UNIQUE (cluster_key)
);


CREATE TABLE retention_runs (
	id SERIAL NOT NULL,
	ts TIMESTAMP WITH TIME ZONE NOT NULL,
	partitions INTEGER NOT NULL,
	archived_rows INTEGER NOT NULL,
	byte_size INTEGER NOT NULL,
	compacted INTEGER NOT NULL,
	pruned INTEGER NOT NULL,
	growth_bytes INTEGER NOT NULL,
	growth_pct FLOAT,
	duration_sec FLOAT,
	PRIMARY KEY (id)
);


CREATE TABLE parity_mismatches (
	id SERIAL NOT NULL,
	run_ts TIMESTAMP WITH TIME ZONE NOT NULL,
	decision_ts TIMESTAMP WITH TIME ZONE NOT NULL,
	asset_id INTEGER,
	symbol VARCHAR(64),
	feature_name VARCHAR(128) NOT NULL,
	sql_value FLOAT,
	lake_value FLOAT,
	sql_missing BOOLEAN NOT NULL,
	lake_missing BOOLEAN NOT NULL,
	state VARCHAR(16) NOT NULL,
	PRIMARY KEY (id)
);

CREATE INDEX ix_parity_mismatch_run_ts ON parity_mismatches (run_ts);

CREATE INDEX ix_parity_mismatch_decision_ts ON parity_mismatches (decision_ts);


CREATE TABLE sources (
	id SERIAL NOT NULL,
	name VARCHAR(128) NOT NULL,
	source_type VARCHAR(64) NOT NULL,
	tier VARCHAR(64) NOT NULL,
	base_url VARCHAR(512),
	enabled BOOLEAN NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (name)
);


CREATE TABLE system_health (
	id SERIAL NOT NULL,
	component VARCHAR(128) NOT NULL,
	ts TIMESTAMP WITH TIME ZONE NOT NULL,
	freshness_sec FLOAT,
	error_count INTEGER NOT NULL,
	lag_sec FLOAT,
	state VARCHAR(32) NOT NULL,
	message TEXT,
	PRIMARY KEY (id),
	CONSTRAINT uq_system_health_component_ts UNIQUE (component, ts)
);


CREATE TABLE wallet_clusters (
	id SERIAL NOT NULL,
	method_version VARCHAR(64) NOT NULL,
	confidence FLOAT NOT NULL,
	first_seen_at TIMESTAMP WITH TIME ZONE NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
);


CREATE TABLE archive_manifests (
	id SERIAL NOT NULL,
	object_key VARCHAR(1024) NOT NULL,
	source_id INTEGER NOT NULL,
	partition_year INTEGER NOT NULL,
	partition_month INTEGER NOT NULL,
	row_count INTEGER NOT NULL,
	byte_size INTEGER NOT NULL,
	first_observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	last_observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_archive_manifest_object_key UNIQUE (object_key),
	FOREIGN KEY(source_id) REFERENCES sources (id)
);


CREATE TABLE assets (
	id SERIAL NOT NULL,
	chain_id INTEGER NOT NULL,
	address VARCHAR(160) NOT NULL,
	symbol VARCHAR(64) NOT NULL,
	name VARCHAR(256),
	asset_type VARCHAR(64) NOT NULL,
	first_seen_at TIMESTAMP WITH TIME ZONE NOT NULL,
	status VARCHAR(64) NOT NULL,
	identity_confidence FLOAT NOT NULL,
	website_url VARCHAR(1024),
	github_url VARCHAR(1024),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_assets_chain_address UNIQUE (chain_id, address),
	FOREIGN KEY(chain_id) REFERENCES chains (id)
);


CREATE TABLE backtest_results (
	id SERIAL NOT NULL,
	run_id INTEGER NOT NULL,
	metric_name VARCHAR(128) NOT NULL,
	metric_value FLOAT NOT NULL,
	chain_slug VARCHAR(32),
	details_json JSON NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_backtest_result_metric_chain UNIQUE (run_id, metric_name, chain_slug),
	FOREIGN KEY(run_id) REFERENCES backtest_runs (id)
);


CREATE TABLE ingestion_watermarks (
	id SERIAL NOT NULL,
	source_id INTEGER NOT NULL,
	chain_id INTEGER,
	cursor_name VARCHAR(128) NOT NULL,
	cursor_value VARCHAR(512),
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_watermark UNIQUE (source_id, chain_id, cursor_name),
	FOREIGN KEY(source_id) REFERENCES sources (id),
	FOREIGN KEY(chain_id) REFERENCES chains (id)
);


CREATE TABLE raw_evidence_items (
	id SERIAL NOT NULL,
	source_id INTEGER NOT NULL,
	source_type VARCHAR(64) NOT NULL,
	source_tier VARCHAR(64) NOT NULL,
	url_hash VARCHAR(128),
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	effective_at TIMESTAMP WITH TIME ZONE,
	ingested_at TIMESTAMP WITH TIME ZONE NOT NULL,
	raw_path VARCHAR(1024),
	content_hash VARCHAR(128) NOT NULL,
	payload JSON NOT NULL,
	archived_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT uq_raw_evidence_source_hash UNIQUE (source_id, content_hash),
	FOREIGN KEY(source_id) REFERENCES sources (id)
);


CREATE TABLE venues (
	id SERIAL NOT NULL,
	venue_type VARCHAR(64) NOT NULL,
	name VARCHAR(128) NOT NULL,
	chain_id INTEGER,
	official_url VARCHAR(512),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_venues_name_chain UNIQUE (name, chain_id),
	FOREIGN KEY(chain_id) REFERENCES chains (id)
);


CREATE TABLE wallet_cluster_members (
	id SERIAL NOT NULL,
	cluster_id INTEGER NOT NULL,
	wallet_address VARCHAR(192) NOT NULL,
	confidence FLOAT NOT NULL,
	role VARCHAR(32) NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_wallet_cluster_member UNIQUE (cluster_id, wallet_address),
	FOREIGN KEY(cluster_id) REFERENCES wallet_clusters (id)
);


CREATE TABLE catalysts (
	id SERIAL NOT NULL,
	asset_id INTEGER NOT NULL,
	catalyst_type VARCHAR(128) NOT NULL,
	scheduled_at TIMESTAMP WITH TIME ZONE,
	published_at TIMESTAMP WITH TIME ZONE,
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	confidence FLOAT NOT NULL,
	source_id INTEGER NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(asset_id) REFERENCES assets (id),
	FOREIGN KEY(source_id) REFERENCES sources (id)
);


CREATE TABLE contracts (
	id SERIAL NOT NULL,
	chain_id INTEGER NOT NULL,
	asset_id INTEGER,
	address VARCHAR(160) NOT NULL,
	verified_flag BOOLEAN NOT NULL,
	proxy_flag BOOLEAN NOT NULL,
	implementation_address VARCHAR(160),
	deployer_wallet VARCHAR(160),
	bytecode_hash VARCHAR(128),
	abi_hash VARCHAR(128),
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_contracts_chain_address UNIQUE (chain_id, address),
	FOREIGN KEY(chain_id) REFERENCES chains (id),
	FOREIGN KEY(asset_id) REFERENCES assets (id)
);


CREATE TABLE features (
	id SERIAL NOT NULL,
	asset_id INTEGER NOT NULL,
	decision_ts TIMESTAMP WITH TIME ZONE NOT NULL,
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	feature_name VARCHAR(128) NOT NULL,
	feature_value FLOAT NOT NULL,
	source_count INTEGER NOT NULL,
	freshness_score FLOAT NOT NULL,
	missing_flag BOOLEAN NOT NULL,
	source_refs JSON NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_features_asset_ts_name UNIQUE (asset_id, decision_ts, feature_name),
	FOREIGN KEY(asset_id) REFERENCES assets (id)
);


CREATE TABLE fingerprint_assessments (
	id SERIAL NOT NULL,
	asset_id INTEGER NOT NULL,
	decision_ts TIMESTAMP WITH TIME ZONE NOT NULL,
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	recidivism_score FLOAT NOT NULL,
	matched_cluster_count INTEGER NOT NULL,
	matched_wallet_count INTEGER NOT NULL,
	matched_roles JSON NOT NULL,
	matched_clusters JSON NOT NULL,
	details JSON NOT NULL,
	model_version VARCHAR(128) NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_fingerprint_asset_ts_version UNIQUE (asset_id, decision_ts, model_version),
	FOREIGN KEY(asset_id) REFERENCES assets (id)
);


CREATE TABLE forecasts (
	id SERIAL NOT NULL,
	asset_id INTEGER NOT NULL,
	decision_ts TIMESTAMP WITH TIME ZONE NOT NULL,
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	p_ignition_24h FLOAT NOT NULL,
	p_collapse_24h FLOAT NOT NULL,
	expected_hours_to_peak FLOAT,
	expected_hours_to_collapse FLOAT,
	calibration_bucket VARCHAR(32),
	calibrated BOOLEAN NOT NULL,
	details JSON NOT NULL,
	model_version VARCHAR(128) NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_forecast_asset_ts_version UNIQUE (asset_id, decision_ts, model_version),
	FOREIGN KEY(asset_id) REFERENCES assets (id)
);


CREATE TABLE holders (
	id SERIAL NOT NULL,
	asset_id INTEGER NOT NULL,
	wallet_address VARCHAR(192) NOT NULL,
	source_id INTEGER NOT NULL,
	ts TIMESTAMP WITH TIME ZONE NOT NULL,
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	balance FLOAT NOT NULL,
	pct_supply FLOAT,
	PRIMARY KEY (id),
	CONSTRAINT uq_holders_asset_wallet_ts UNIQUE (asset_id, wallet_address, ts, source_id),
	FOREIGN KEY(asset_id) REFERENCES assets (id),
	FOREIGN KEY(source_id) REFERENCES sources (id)
);


CREATE TABLE ignition_events (
	id SERIAL NOT NULL,
	asset_id INTEGER NOT NULL,
	source_id INTEGER NOT NULL,
	event_type VARCHAR(64) NOT NULL,
	ts TIMESTAMP WITH TIME ZONE NOT NULL,
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	confidence FLOAT NOT NULL,
	details JSON NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_ignition_event UNIQUE (asset_id, event_type, ts, source_id),
	FOREIGN KEY(asset_id) REFERENCES assets (id),
	FOREIGN KEY(source_id) REFERENCES sources (id)
);


CREATE TABLE labels (
	id SERIAL NOT NULL,
	asset_id INTEGER NOT NULL,
	ts TIMESTAMP WITH TIME ZONE NOT NULL,
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	label_type VARCHAR(128) NOT NULL,
	label_value VARCHAR(256) NOT NULL,
	label_source VARCHAR(128) NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_labels_asset_ts_type UNIQUE (asset_id, ts, label_type),
	FOREIGN KEY(asset_id) REFERENCES assets (id)
);


CREATE TABLE lifecycle_events (
	id SERIAL NOT NULL,
	asset_id INTEGER NOT NULL,
	phase VARCHAR(32) NOT NULL,
	event_type VARCHAR(32) NOT NULL,
	ts TIMESTAMP WITH TIME ZONE NOT NULL,
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	confidence FLOAT NOT NULL,
	details JSON NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_lifecycle_asset_phase_ts UNIQUE (asset_id, phase, ts, event_type),
	FOREIGN KEY(asset_id) REFERENCES assets (id)
);


CREATE TABLE news_items (
	id SERIAL NOT NULL,
	source_id INTEGER NOT NULL,
	published_at TIMESTAMP WITH TIME ZONE,
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	source_domain VARCHAR(256) NOT NULL,
	title_hash VARCHAR(128) NOT NULL,
	title TEXT NOT NULL,
	url_hash VARCHAR(128) NOT NULL,
	official_flag BOOLEAN NOT NULL,
	raw_evidence_id INTEGER,
	PRIMARY KEY (id),
	CONSTRAINT uq_news_url_hash UNIQUE (url_hash),
	FOREIGN KEY(source_id) REFERENCES sources (id),
	FOREIGN KEY(raw_evidence_id) REFERENCES raw_evidence_items (id)
);


CREATE TABLE pools (
	id SERIAL NOT NULL,
	chain_id INTEGER NOT NULL,
	address VARCHAR(192) NOT NULL,
	dex_id VARCHAR(128) NOT NULL,
	base_asset_id INTEGER NOT NULL,
	quote_asset_id INTEGER,
	created_at_source TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_pools_chain_address UNIQUE (chain_id, address),
	FOREIGN KEY(chain_id) REFERENCES chains (id),
	FOREIGN KEY(base_asset_id) REFERENCES assets (id),
	FOREIGN KEY(quote_asset_id) REFERENCES assets (id)
);


CREATE TABLE prelaunch_candidates (
	id SERIAL NOT NULL,
	asset_id INTEGER NOT NULL,
	decision_ts TIMESTAMP WITH TIME ZONE NOT NULL,
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	priority_score FLOAT NOT NULL,
	drivers JSON NOT NULL,
	model_version VARCHAR(128) NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_prelaunch_asset_ts UNIQUE (asset_id, decision_ts, model_version),
	FOREIGN KEY(asset_id) REFERENCES assets (id)
);


CREATE TABLE scores (
	id SERIAL NOT NULL,
	asset_id INTEGER NOT NULL,
	decision_ts TIMESTAMP WITH TIME ZONE NOT NULL,
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	hype FLOAT NOT NULL,
	ethos FLOAT NOT NULL,
	risk FLOAT NOT NULL,
	liquidity_access FLOAT NOT NULL,
	manipulation FLOAT NOT NULL,
	confidence FLOAT NOT NULL,
	uncertainty FLOAT NOT NULL,
	catalyst FLOAT NOT NULL,
	exit_risk FLOAT NOT NULL,
	research_priority FLOAT NOT NULL,
	risk_band VARCHAR(16) NOT NULL,
	model_version VARCHAR(128) NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_scores_asset_ts_model UNIQUE (asset_id, decision_ts, model_version),
	FOREIGN KEY(asset_id) REFERENCES assets (id)
);


CREATE TABLE social_mentions (
	id SERIAL NOT NULL,
	asset_id INTEGER,
	topic VARCHAR(256),
	source_id INTEGER NOT NULL,
	ts TIMESTAMP WITH TIME ZONE NOT NULL,
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	author_hash VARCHAR(128),
	metrics_json JSON NOT NULL,
	raw_ref VARCHAR(1024),
	PRIMARY KEY (id),
	FOREIGN KEY(asset_id) REFERENCES assets (id),
	FOREIGN KEY(source_id) REFERENCES sources (id)
);


CREATE TABLE alert_type_controls (
    id INTEGER PRIMARY KEY,
    alert_type VARCHAR(128) NOT NULL UNIQUE,
    reenabled BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMP NOT NULL
);

CREATE TABLE alerts (
	id SERIAL NOT NULL,
	asset_id INTEGER NOT NULL,
	score_id INTEGER,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	alert_type VARCHAR(128) NOT NULL,
	threshold_version VARCHAR(128) NOT NULL,
	score_snapshot_ref VARCHAR(256),
	state VARCHAR(32) NOT NULL,
	message TEXT NOT NULL,
	notified_at TIMESTAMP WITH TIME ZONE,
	acked_at TIMESTAMP WITH TIME ZONE,
	ack_quality VARCHAR(32),
	snoozed_until TIMESTAMP,
	PRIMARY KEY (id),
	FOREIGN KEY(asset_id) REFERENCES assets (id),
	FOREIGN KEY(score_id) REFERENCES scores (id)
);


CREATE TABLE contract_flags (
	id SERIAL NOT NULL,
	contract_id INTEGER NOT NULL,
	source_id INTEGER NOT NULL,
	ts TIMESTAMP WITH TIME ZONE NOT NULL,
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	flag_type VARCHAR(128) NOT NULL,
	severity VARCHAR(32) NOT NULL,
	evidence_id INTEGER,
	details JSON NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_contract_flag UNIQUE (contract_id, ts, flag_type, source_id),
	FOREIGN KEY(contract_id) REFERENCES contracts (id),
	FOREIGN KEY(source_id) REFERENCES sources (id),
	FOREIGN KEY(evidence_id) REFERENCES raw_evidence_items (id)
);


CREATE TABLE liquidity_snapshots (
	id SERIAL NOT NULL,
	pool_id INTEGER NOT NULL,
	source_id INTEGER NOT NULL,
	ts TIMESTAMP WITH TIME ZONE NOT NULL,
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	reserve_base FLOAT,
	reserve_quote FLOAT,
	reserve_usd FLOAT,
	lp_concentration_hhi FLOAT,
	raw_evidence_id INTEGER,
	PRIMARY KEY (id),
	CONSTRAINT uq_liquidity_pool_ts_source UNIQUE (pool_id, ts, source_id),
	FOREIGN KEY(pool_id) REFERENCES pools (id),
	FOREIGN KEY(source_id) REFERENCES sources (id),
	FOREIGN KEY(raw_evidence_id) REFERENCES raw_evidence_items (id)
);


CREATE TABLE pairs (
	id SERIAL NOT NULL,
	venue_id INTEGER NOT NULL,
	chain_id INTEGER NOT NULL,
	base_asset_id INTEGER NOT NULL,
	quote_asset_id INTEGER,
	pool_id INTEGER,
	created_at_source TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_pairs_natural UNIQUE (venue_id, base_asset_id, quote_asset_id, pool_id),
	FOREIGN KEY(venue_id) REFERENCES venues (id),
	FOREIGN KEY(chain_id) REFERENCES chains (id),
	FOREIGN KEY(base_asset_id) REFERENCES assets (id),
	FOREIGN KEY(quote_asset_id) REFERENCES assets (id),
	FOREIGN KEY(pool_id) REFERENCES pools (id)
);


CREATE TABLE score_explanations (
	id SERIAL NOT NULL,
	score_id INTEGER NOT NULL,
	drivers JSON NOT NULL,
	risk_reasons JSON NOT NULL,
	missing_features JSON NOT NULL,
	changed_features JSON NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_score_explanations_score UNIQUE (score_id),
	FOREIGN KEY(score_id) REFERENCES scores (id)
);


CREATE TABLE market_snapshots (
	id SERIAL NOT NULL,
	pair_id INTEGER NOT NULL,
	source_id INTEGER NOT NULL,
	ts TIMESTAMP WITH TIME ZONE NOT NULL,
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	open FLOAT,
	high FLOAT,
	low FLOAT,
	close FLOAT,
	price_usd FLOAT,
	volume_usd FLOAT,
	buys INTEGER,
	sells INTEGER,
	trades INTEGER,
	raw_evidence_id INTEGER,
	PRIMARY KEY (id),
	CONSTRAINT uq_market_pair_ts_source UNIQUE (pair_id, ts, source_id),
	FOREIGN KEY(pair_id) REFERENCES pairs (id),
	FOREIGN KEY(source_id) REFERENCES sources (id),
	FOREIGN KEY(raw_evidence_id) REFERENCES raw_evidence_items (id)
);
