-- Workbook row numbers are unique within a sheet, not across the whole file.
ALTER TABLE schedule_rows ADD COLUMN source_sheet text NOT NULL DEFAULT '';
ALTER TABLE schedule_rows DROP CONSTRAINT schedule_rows_pkey;
ALTER TABLE schedule_rows ADD PRIMARY KEY (version_id, source_sheet, source_row_number);
CREATE INDEX schedule_trial_idx ON schedule_rows (tenant_id, schedule_kind, trial_deadline);
CREATE INDEX schedule_final_idx ON schedule_rows (tenant_id, schedule_kind, final_deadline);
CREATE INDEX schedule_ship_idx ON schedule_rows (tenant_id, schedule_kind, estimated_ship_date);
