-- Runs once, on first initialisation of the PostgreSQL data volume.
--
-- pgvector is what backs the semantic half of the hybrid retrieval channel.
-- Enabling it here (rather than at query time) means the extension is present
-- before the application ever starts, so the first vector search cannot fail
-- with "extension does not exist".

CREATE EXTENSION IF NOT EXISTS vector;

-- Trigram index support for fuzzy keyword matching on knowledge titles.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
