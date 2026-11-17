# Claude Code / Claude Fable 2 — startprompt for Stiflyt2

Read `STIFLYT2_BUILD_PLAN_V3.md` in full. Treat it as the target architecture and implementation acceptance contract, not as a description of the current repository.

**First inspect the existing Stiflyt v1 repository**. Identify reusable code and workflows, especially data acquisition/import, Turrutebasen ETL/migrations, FKB integration, GPX parsing, spatial place-name search, authentication, and existing frontend components. Do not assume existing source endpoints, schemas, permissions or licenses are valid without checking. Do not rewrite v1 blindly.

The primary phase-1 product is a **mountain trail route reconstruction workbench**. Select an area using route prefixes (RON/FEM/BRE) and a non-authoritative DNT maintenance filter; import Turrutebasen route candidates, FKB-TraktorvegSti geometry and user GPX; prefer appropriate FKB geometry; create and edit a Stiflyt-owned connected graph, routes and verified metadata; validate, publish and export.

**Non-negotiable isolation:** `source_raw`, `source_index`, `import_control`, `core`, `history` are distinct PostgreSQL schemas with separate permissions and migrations. Import workers cannot write `core` or `history`. No foreign keys from `core` to imported source tables. Stiflyt stores its own approved geometry, stable IDs and immutable published revisions. Imported source snapshots are immutable; normalized indexes are rebuildable. Reimport must never silently change a published route.

Use Python/FastAPI, PostgreSQL/PostGIS, React/TypeScript/MapLibre and a versioned `/api/v1` with Pydantic DTOs, OpenAPI-generated TS client and a separate import UI/job workflow. Favor a modular monolith and a simple worker over microservices. Design stable identifiers so images, journals and tasks can be added later, but do not implement those modules now.

**Execution:**
1. Produce a concise repository assessment, a source-to-domain boundary diagram, a proposed schema, REST contracts, migration strategy, key risks and an incremental task plan.
2. Identify ambiguities requiring a product decision, especially real FKB/Turrutebasen access and the minimum metadata required to publish. State assumptions clearly; do not fabricate APIs.
3. Implement phase 0 only until the schema/API proposal is reviewed, unless explicitly instructed to proceed.
4. After approval, deliver vertical slices in the order specified in the build plan. For each slice: tests, migrations, API docs, runnable UI, and a short changelog.
5. Demonstrate a destructive-source-index rebuild test and a source-feature-deletion test before calling phase 1 complete.

Prioritize a fast, understandable editor and correct graph topology over sophisticated automatic map matching. Never mistake a connected route for a physically safe route.
