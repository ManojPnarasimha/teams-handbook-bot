-- Supabase schema for TriconGPT.
-- Run in the Supabase SQL editor (or via psql) against your project database.

-- Prerequisite: enable the `vector` extension once from the Supabase dashboard
-- (Database → Extensions → search "vector" → Enable). Supabase manages the
-- schema for extensions itself, so we don't `CREATE EXTENSION` here.
-- `pgcrypto` (used for gen_random_uuid()) is enabled by Supabase by default.

-- ---- Core tables ------------------------------------------------------------

create table if not exists employees (
  id uuid primary key default gen_random_uuid(),
  aad_object_id text unique not null,
  name text,
  email text,
  created_at timestamptz default now()
);

create table if not exists documents (
  id uuid primary key default gen_random_uuid(),
  source_path text not null,          -- path of the file in the 'org-docs' bucket
  source_updated_at timestamptz,      -- object's updated_at/etag, used to detect changes
  content text,
  content_hash text not null,         -- hash of this chunk's text, for idempotent upserts
  embedding vector(1536),
  created_at timestamptz default now()
);
create unique index if not exists documents_content_hash_key on documents (content_hash);
create index if not exists documents_embedding_ivfflat on documents
  using ivfflat (embedding vector_cosine_ops);
create index if not exists documents_source_path_idx on documents (source_path);

create table if not exists conversations (
  id uuid primary key default gen_random_uuid(),
  employee_id uuid references employees(id) on delete cascade,
  channel text,                        -- 'teams' | 'webchat' | 'local'
  created_at timestamptz default now()
);

create table if not exists messages (
  id uuid primary key default gen_random_uuid(),
  conversation_id uuid references conversations(id) on delete cascade,
  role text check (role in ('user','assistant')),
  content text,
  created_at timestamptz default now()
);
create index if not exists messages_conversation_created_idx
  on messages (conversation_id, created_at);

create table if not exists memory_summaries (
  employee_id uuid primary key references employees(id) on delete cascade,
  summary_text text,
  updated_at timestamptz default now()
);

create table if not exists conversation_references (
  employee_id uuid primary key references employees(id) on delete cascade,
  reference jsonb,                     -- Bot Framework ConversationReference
  updated_at timestamptz default now()
);

-- ---- Similarity search RPC --------------------------------------------------
-- Called from rag.py via supabase.rpc('match_documents', {...}).
-- Cosine similarity = 1 - cosine_distance; we filter by a min-similarity threshold.

create or replace function match_documents(
  query_embedding vector(1536),
  match_count int default 5,
  similarity_threshold float default 0.72
)
returns table (
  id uuid,
  source_path text,
  content text,
  similarity float
)
language sql stable
as $$
  select
    d.id,
    d.source_path,
    d.content,
    1 - (d.embedding <=> query_embedding) as similarity
  from documents d
  where d.embedding is not null
    and 1 - (d.embedding <=> query_embedding) >= similarity_threshold
  order by d.embedding <=> query_embedding
  limit match_count;
$$;
