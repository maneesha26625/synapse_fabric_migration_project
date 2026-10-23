The target is a view on the **SQL analytics endpoint** of the Fabric Lakehouse `{{target.lakehouse}}`. Return the
full T-SQL `CREATE VIEW` statement; the run creates it on the endpoint.

- Keep the name `{{object.schema}}.{{object.name}}`, the column names, their order and their types. Callers and
  reports depend on them.
- The endpoint reads the Lakehouse's Delta tables by `schema.table`. Every table the view reads must be one of
  them: a table that is not in the Lakehouse (another database, an external table) goes in `unconverted`.
- The endpoint compares text **case-sensitively** (`Latin1_General_100_BIN2_UTF8`). Where the view joins or filters
  on text that relied on Synapse's case-insensitive collation, compare `LOWER()` / `UPPER()` values or add
  `COLLATE Latin1_General_100_CI_AS_KS_WS_SC_UTF8`, and say so in `notes`.
- Delta text columns surface as `varchar(8000)`; `datetime` columns as `datetime2(6)`. Keep casts that callers rely on.
- Materialized views do not exist on the endpoint: convert to a plain view and say it is no longer precomputed.
- Remove table hints (`NOLOCK` and other locking hints) and say so in `notes`.
- `sys.pdw_*` views do not exist in Fabric. Use the Fabric equivalent, or list it in `unconverted`.
