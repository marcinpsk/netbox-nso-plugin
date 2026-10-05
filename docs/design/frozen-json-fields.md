# Lossless frozen JSON fields

## Scope and acceptance

Renderer mutation plans must retain JSON field values without mutable references.
Execution must preserve objects, arrays, empty containers, scalars, and null.
Exact write matching must distinguish an object from an array.
Field defaults must not determine a stored value's shape.
The LACP, subinterface, and redistribution executors must share the writer's codec.
Canonical intent fragments and database schemas stay unchanged.

## Blind design and decision

The coordinator and an independent GPT-6.1 Sol designer at high effort inspected
only the failure, constraints, and current code before comparing designs.
Both selected a field-aware codec in the renderer writer module.
The competing shape was a recursive tuple representation with container tags.
Canonical JSON text uses the existing standard library and field encoders.
It needs no recursive decoder or shape inference.

The interface is `freeze_field_value(field, value)` and
`thaw_field_value(field, value)`.
A frozen dataclass stores canonical JSON text for JSON fields.
Other fields use the existing normalization.
The decoder requires the frozen JSON type and uses the field's decoder.
Every plan producer and matching path uses the encoder.
Every model materialization path uses the decoder.
There is no fallback for the previous tuple representation.

## Validation

Use real renderer plans, writers, model instances, and PostgreSQL.
Confirm nullable and nested JSON regressions fail before changing production code.
Check the three executors, exact shape matching, mutable input isolation,
and JSON set updates.
Run the full configured worker pool on each final branch head.

## Review

Astra High ratified revision r1 after a read-only code review.
An independent in-memory proof passed 15 JSON round trips and distinguished
five normalization collisions. No design blockers remain.
The first increment replaces frozen field encoding and all three executors.
