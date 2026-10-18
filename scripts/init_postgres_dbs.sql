-- Initialize dual logical databases for RPIM Pipeline
SELECT 'CREATE DATABASE rpim_discovery'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'rpim_discovery')\gexec

SELECT 'CREATE DATABASE rpim_processing'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'rpim_processing')\gexec
