-- Run "psql postgres" to connect to postgres shell/terminal

CREATE USER postgres WITH PASSWORD 'test-password';
CREATE DATABASE ecpmp;
GRANT ALL PRIVILEGES ON DATABASE ecpmp TO postgres;
