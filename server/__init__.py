"""The Lumen API server.

A FastAPI application in front of Supabase (Postgres + GoTrue). It exists so
that the desktop agent can be one user among many without any user's data,
credentials or agent ever reaching another's.

Where to look:

    main.py          the app: startup checks, middleware, routers
    settings.py      configuration, and the allowlist of what a client may see
    db.py            RLS-scoped connections, and the one that bypasses them
    deps.py          the gate: how a request becomes a verified user
    errors.py        the error envelope; nothing internal crosses it
    observability.py redacting logger and the audit trail
    security/        JWT verification, token encryption, rate limits, headers
    routers/         auth, account, integrations, agent
    services/        GoTrue client, OAuth custody, permissions, agent binding
"""
