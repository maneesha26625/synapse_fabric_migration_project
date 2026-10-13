<!--
  PROJECT REQUIREMENTS. Edit this file for each migration project.

  Everything below this comment is put into every transformation prompt, under
  "Project requirements", and takes priority over the general guidance. The
  comment itself is removed before the prompt is sent.

  You can use the same placeholders as the other templates, for example
  {{target.warehouse}}, {{target.workspace}}, {{object.schema}}, {{object.type}},
  and conditional blocks: {{#if object.schema}} ... {{/if}}.

  Delete the examples that don't apply, or leave the file empty for no project rules.
-->

- Target Warehouse: `{{target.warehouse}}`. Keep every schema name as it is in Synapse.
- Every changed line in SQL gets a `-- Fabric:` comment that says why.
- Keep T-SQL keywords in upper case and 4-space indentation, as in the source.
- Do not add new error handling or logging that isn't in the source.
