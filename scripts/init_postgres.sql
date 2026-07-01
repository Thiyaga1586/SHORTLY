-- scripts/init_postgres.sql
-- --------------------------
-- Run automatically by the postgres Docker container on first start.
-- Creates one logical database per storage node + the app user.

CREATE USER shortly WITH PASSWORD 'shortly_secret';

CREATE DATABASE shortly_node_1 OWNER shortly;
CREATE DATABASE shortly_node_2 OWNER shortly;
CREATE DATABASE shortly_node_3 OWNER shortly;

-- Allow pgBouncer stats user to connect
GRANT pg_monitor TO shortly;
