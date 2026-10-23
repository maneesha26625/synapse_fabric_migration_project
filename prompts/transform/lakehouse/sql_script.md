The target is the Fabric Lakehouse `{{target.lakehouse}}`. Its SQL analytics endpoint is **read-only**, so the
script becomes a **PySpark notebook** attached to the Lakehouse, which runs it with Spark SQL.

Return the whole notebook as JSON in the same shape as the rule output:
`{"language": "python", "cells": [{"cell_type": "markdown" | "code", "source": "..."}]}`.

- Start from the rule output (it uses the helpers `q()`, `scalar()` and `truth()`, and runs the body in `run()`);
  fix what it marked `# REVIEW`.
- Follow the same Spark SQL rules as a stored procedure: `schema.table` names in the default Lakehouse, Delta
  statements (`MERGE INTO`, `DELETE FROM`, `INSERT INTO ... SELECT`), Spark functions, no multi-statement
  transactions, temporary tables in the `tmp_work` schema, variables as named parameters (`:name`).
- `USE <database>` and `GO` have no meaning in a notebook: remove them.
- A `SELECT` with no side effects shows its result with `display(q(...))`.
