// SPDX-License-Identifier: Apache-2.0
// Copyright (C) 2026 Marcin Zieba
// Annotated examples for the JavaScript review patterns.

function unsafeMemberLabels(row) {
  // ruleid: nso-observed-members-join-without-array-check
  const display = (row.observed_members || []).join(", ");
  // ruleid: nso-observed-members-join-without-array-check
  const search = (row.observed_members ?? []).join(" ");
  return [display, search];
}

function validatedMemberLabels(row) {
  // ok: nso-observed-members-join-without-array-check
  const display = Array.isArray(row.observed_members) ? row.observed_members.join(", ") : "";
  // ok: nso-observed-members-join-without-array-check
  const search = (Array.isArray(row.observed_members) ? row.observed_members : []).join(" ");
  // ok: nso-observed-members-join-without-array-check
  const nativeMembers = (row.members || []).join(" ");
  return [display, search, nativeMembers];
}
