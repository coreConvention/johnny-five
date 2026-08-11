# Project Isolation Boundary Design

## Problem

Johnny-Five currently applies project isolation differently across read and write paths:

- enforced search filters project tags but ignores an explicit `MemoryRecord.project_dir`, allowing another project's record to appear;
- session recall can surface a candidate owned by another explicit project if an upstream candidate source returns one;
- deduplication searches every vector and can merge a new project-scoped store into a record owned by another project.

These are the root causes of issues #21 and #23.

## Boundary contract

`MemoryRecord.project_dir` is the authoritative ownership boundary when it is present.

- A record with an explicit project directory is eligible only for the same canonical project directory.
- Windows paths compare case-insensitively and tolerate slash-style differences.
- POSIX paths preserve case and nonblank whitespace while normalizing redundant separators and path segments.
- An explicit conflicting project directory is never overridden by `scope:cross-project` or a project tag.
- An unscoped record remains globally readable. Existing tag-only records retain their current behavior for each read path so Claude compatibility is preserved.
- Global session recall accepts only canonically global records; it never injects a record with explicit project ownership.
- Deduplication is stricter than reading: scoped writes merge only with the same explicit scope, and global writes merge only with global records. Tags never authorize a cross-scope mutation.

## Implementation

Add a small shared scope module that owns path canonicalization and the two compatibility decisions:

1. read eligibility for enforced search and session recall;
2. exact-scope eligibility for deduplication.

Use the same canonical read predicate during FTS and always-load candidate acquisition so equivalent path spellings are not discarded before post-lookup enforcement. General vector retrieval remains unchanged. Deduplication expands its ordered vector window when foreign near-duplicates fill it, stopping when it finds a compatible candidate, reaches the distance threshold, or exhausts the result set.

## Compatibility

Claude and Codex use the same server paths, so the fix is client-neutral. Enforced search preserves the existing tag-only rules, including `scope:cross-project`; session recall preserves its broader tag-only behavior. Both paths now reject records whose stored `project_dir` explicitly belongs elsewhere.

No schema migration, data rewrite, tool signature change, hook change, service restart, or memory mutation is required.

## Verification

- Search excludes conflicting explicit scopes with and without cross-project tags.
- Search accepts equivalent Windows path spellings and retains POSIX case sensitivity.
- FTS and always-load acquisition use the same canonical scope comparison.
- Session recall excludes conflicting explicit scopes while retaining legacy tag-only records.
- Deduplication never mutates a record in another scope and can continue to a later compatible candidate.
- Foreign candidates cannot starve a compatible duplicate outside the initial vector window.
- Scoped/global writes do not merge across the scope boundary.
- Existing search, recall, deduplication, MCP, and hook tests remain green.
