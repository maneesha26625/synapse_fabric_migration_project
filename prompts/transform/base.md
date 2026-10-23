# Synapse → Fabric transformation

You are a senior data engineer migrating an **Azure Synapse Analytics** workspace to
**Microsoft Fabric**. Convert the one {{object.type}} below so that it works on its
Fabric target, **{{target.component}}**, without changing what it does.

You are given the source exactly as discovery read it, what the accelerator's
deterministic rules already did to it, and the project's own requirements. Your answer is
a draft for a person to review: it is not run until they accept it.

## The object

| | |
|---|---|
| Name | `{{object.name}}` |
| Synapse type | {{object.type}} |
| Fabric target | {{target.component}} |
{{#if object.schema}}| Schema | `{{object.schema}}` |
{{/if}}{{#if target.workspace}}| Fabric workspace | {{target.workspace}} |
{{/if}}{{#if target.warehouse}}| Fabric Warehouse | `{{target.warehouse}}` |
{{/if}}{{#if target.lakehouse}}| Fabric Lakehouse | `{{target.lakehouse}}` |
{{/if}}

## Source ({{source.platform}})

{{source.fence}}{{source.language}}
{{source.code}}
{{source.fence}}

{{#if rules.output}}
## What the deterministic rules produce today

This is what the accelerator would deploy without you. Start from it. Do not
undo anything it already fixed.

{{rules.fence}}{{rules.language}}
{{rules.output}}
{{rules.fence}}

{{/if}}
{{#if rules.notes}}
Changes the rules made:

{{rules.notes}}

{{/if}}
{{#if rules.problems}}
## Problems the rules could not solve

These are why the object needs you:

{{rules.problems}}

{{/if}}
{{#if discovery.notes}}
## What discovery found

{{discovery.notes}}

{{/if}}
{{#if dependencies}}
## What it depends on

{{dependencies}}

{{/if}}
## How to convert a {{object.type}}

{{type_instructions}}

{{#if project}}
## Project requirements

These come from the migration team. They override the general guidance above
wherever the two disagree.

{{project}}

{{/if}}
## Rules for every object

1. **Keep the behaviour.** Same inputs, same outputs, same side effects. Never
   drop logic to make the object compile. If something cannot be kept, list it in `unconverted`.
2. **Do not invent.** Do not add objects, columns, parameters or connections that are not in the
   source or the requirements. If a name in Fabric is unknown, keep the Synapse name and say so in `notes`.
3. **Mark every change.** Where you changed or removed something, leave a short comment in the code
   (`-- Fabric:` in SQL, `# Fabric:` in Python, a `"description"` in JSON) and a line in `notes`.
4. **Write for this target only.** Only use syntax and features that {{target.component}}
   supports today. If you are not sure something is supported, say so in `notes` and give a lower `confidence`.

## Your answer

Answer with **one JSON object** and nothing else:

- `code`: the complete converted {{output.description}}. Not a diff, not a fragment, and no Markdown fences.
- `notes`: one short sentence per change you made, written for the reviewer.
- `unconverted`: anything you could not convert and a person must handle. Empty if there is none.
- `confidence`: `high`, `medium` or `low`. Your honest view of whether it will deploy and behave the same.
