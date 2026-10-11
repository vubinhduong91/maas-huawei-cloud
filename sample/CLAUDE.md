# Reaparr

Cross-platform Plex media downloader. One repository holds the .NET backend and the Vue front-end.

## Stack

| Layer    | Stack |
|----------|-------|
| Backend  | .NET 10, FastEndpoints, EF Core (SQLite), Autofac, Quartz.NET, SignalR (MessagePack), Serilog, Polly, FluentResults |
| Frontend | Nuxt 4 / Vue 3 + TypeScript, Pinia, Quasar, PrimeVue (in `src/AppHost/ClientApp/`) |
| Testing  | TUnit, Shouldly, Moq, Bogus (backend); Vitest, Cypress (frontend) |

## Layout

- `src/AppHost` – ASP.NET Core host, DI wiring, serves the generated Nuxt app; `ClientApp/` is the front-end.
- `src/Domain` – entities and domain logic.
- `src/Application` (+ `.Contracts`) – commands/queries and their handlers.
- `src/Data` (+ `.Contracts`) – EF Core `DbContext`, migrations, repositories (SQLite).
- `src/PublicAPI` (+ `.Contracts`) – FastEndpoints HTTP endpoints and DTOs.
- `src/SignalR`, `src/PlexApi`, `src/FileSystem`, `src/Settings`, `src/Identity`, `src/External`, `src/Logging`, `src/Environment` – infrastructure modules, each with a `.Contracts` project for its interfaces.
- `tests/UnitTests/<Module>.UnitTests`, `tests/IntegrationTests` – backend tests (TUnit).

## Commands

- Backend build: `dotnet build Reaparr.sln`
- Backend tests: `dotnet test` (TUnit on Microsoft.Testing.Platform, see `global.json`); one project: `dotnet test --project tests/UnitTests/Domain.UnitTests`
- Run backend in development: `dotnet run --project src/AppHost`
- Front-end (Bun only, never npm/yarn/pnpm), from `src/AppHost/ClientApp/`: `bun install`, `bun run typecheck`, `bun run unit-test`, `bun run lint`
- Full app in Docker: `docker build -f docker/Dockerfile -t reaparr:local .` then `docker run -p 7000:7000 reaparr:local`

## Working rules

- Prefer small, focused diffs; fix root causes, no hacks to silence symptoms.
- Match existing naming, folder layout, analyzers and EditorConfig; keep tests deterministic.
- New backend behaviour goes through an Application command/query plus a PublicAPI endpoint; new UI talks to the API through the generated client in `ClientApp/src/types/api/generated`.
- Add or update tests next to the code you change.
- Do not commit; leave changes in the working tree for review.

`AGENTS.md` targets other agent tooling (skills, `dotnet-test-mcp`, `trash`) that is not installed here; follow this file instead.
