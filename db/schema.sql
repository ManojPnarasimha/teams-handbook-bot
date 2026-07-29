-- Azure SQL Database schema for TriVA.
--
-- Apply once with:
--   sqlcmd -S <server>.database.windows.net -d <db> -U <user> -P <pwd> -N -C -i db/schema.sql
-- or with Azure Data Studio / the VS Code MSSQL extension.
--
-- Re-running against the same database will error (tables already exist);
-- that is intentional. Drop the schema first if you need a clean re-apply.
--
-- Uses the native VECTOR type (GA in Azure SQL Database). Cosine similarity
-- is computed inline in rag.py via VECTOR_DISTANCE('cosine', ...).

-- ---- Core tables ------------------------------------------------------------

CREATE TABLE dbo.employees (
    id             UNIQUEIDENTIFIER NOT NULL DEFAULT NEWID() PRIMARY KEY,
    aad_object_id  NVARCHAR(128)    NOT NULL UNIQUE,
    name           NVARCHAR(256)    NULL,
    email          NVARCHAR(256)    NULL,
    created_at     DATETIMEOFFSET   NOT NULL DEFAULT SYSUTCDATETIME()
);

CREATE TABLE dbo.documents (
    id                 UNIQUEIDENTIFIER NOT NULL DEFAULT NEWID() PRIMARY KEY,
    source_path        NVARCHAR(450)    NOT NULL,       -- SharePoint item path within the drive; 450 keeps the nonclustered index key <= 900 bytes (SQL Server limit is 1700)
    source_updated_at  DATETIMEOFFSET   NULL,           -- SharePoint lastModifiedDateTime
    content            NVARCHAR(MAX)    NULL,
    content_hash       NVARCHAR(64)     NOT NULL UNIQUE,-- SHA-256 hex for idempotent upserts
    embedding          VECTOR(768)      NULL,           -- Azure OpenAI text-embedding-3-small @ dim=768
    created_at         DATETIMEOFFSET   NOT NULL DEFAULT SYSUTCDATETIME()
);

CREATE INDEX documents_source_path_idx ON dbo.documents (source_path);

-- Optional DiskANN vector index for large datasets (>10k chunks).
-- Brute-force VECTOR_DISTANCE is fast enough below that threshold.
--   CREATE VECTOR INDEX documents_embedding_diskann
--     ON dbo.documents (embedding)
--     WITH (metric = 'cosine', type = 'diskann');

CREATE TABLE dbo.conversations (
    id           UNIQUEIDENTIFIER NOT NULL DEFAULT NEWID() PRIMARY KEY,
    employee_id  UNIQUEIDENTIFIER NULL REFERENCES dbo.employees (id) ON DELETE CASCADE,
    channel      NVARCHAR(32)     NULL,                    -- 'teams' | 'webchat' | 'local'
    created_at   DATETIMEOFFSET   NOT NULL DEFAULT SYSUTCDATETIME()
);

CREATE INDEX conversations_employee_channel_idx
    ON dbo.conversations (employee_id, channel, created_at DESC);

CREATE TABLE dbo.messages (
    id               UNIQUEIDENTIFIER NOT NULL DEFAULT NEWID() PRIMARY KEY,
    conversation_id  UNIQUEIDENTIFIER NULL REFERENCES dbo.conversations (id) ON DELETE CASCADE,
    role             NVARCHAR(16)     NOT NULL CHECK (role IN ('user', 'assistant')),
    content          NVARCHAR(MAX)    NULL,
    created_at       DATETIMEOFFSET   NOT NULL DEFAULT SYSUTCDATETIME()
);

CREATE INDEX messages_conversation_created_idx
    ON dbo.messages (conversation_id, created_at);

CREATE TABLE dbo.memory_summaries (
    employee_id   UNIQUEIDENTIFIER NOT NULL PRIMARY KEY REFERENCES dbo.employees (id) ON DELETE CASCADE,
    summary_text  NVARCHAR(MAX)    NULL,
    updated_at    DATETIMEOFFSET   NOT NULL DEFAULT SYSUTCDATETIME()
);

CREATE TABLE dbo.conversation_references (
    employee_id  UNIQUEIDENTIFIER NOT NULL PRIMARY KEY REFERENCES dbo.employees (id) ON DELETE CASCADE,
    reference    NVARCHAR(MAX)    NULL CHECK (reference IS NULL OR ISJSON(reference) = 1),
    updated_at   DATETIMEOFFSET   NOT NULL DEFAULT SYSUTCDATETIME()
);
