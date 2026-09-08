# Xianyu server migration tools

These scripts migrate the deployable state of a Linux server. They are
deliberately independent of a fixed disk layout: pass `--app-root` and use the
optional environment variables for the cloud-auth and reverse-proxy paths.

The bundle contains an encrypted copy of:

- application deployment files and the production `.env`;
- a consistent MySQL logical dump;
- Redis persistence data when available;
- Docker named-volume data for static files, backups and browser state;
- the cloud authentication service database and key files when detected;
- optional Docker images for a fully offline server replacement.

The export does not stop or remove the old deployment. The import refuses to
overwrite a non-empty target unless `--force` is supplied. Keep the old server
running until the new server passes the health check and business smoke tests.

## Export

```bash
sudo bash ./server-migration-export.sh --app-root /path/to/xianyu --output /tmp/xianyu-migrations
```

Optional arguments:

```text
--auth-root PATH       cloud-auth application directory
--auth-data PATH       cloud-auth data directory
--nginx-config PATH    reverse-proxy configuration to archive
--include-images       include all current Compose images for offline restore
--include-registry PATH  include a Docker Registry data directory (auto-detects /var/lib/gemstory/xianyu-registry)
```

The script asks for a migration passphrase without echoing it. The generated
`.tar.gz.enc` file and its `.sha256` file are the only files that need to be
copied to the new server.

The static Tencent release directory grows by one archive per service for each
published version. Use `deploy/server-migration/cleanup-xianyu-releases.sh` from a
maintenance shell to retain a bounded number of complete versions; it defaults
to a dry run and never removes `latest.json`.

## Import

```bash
sudo bash ./server-migration-import.sh \
  --app-root /path/to/xianyu \
  --bundle /tmp/xianyu-migrations/xianyu-migration-....tar.gz.enc \
  --force
```

Use `--restore-nginx` only after checking that the new server has the required
certificate paths. The script does not change DNS. Switch the domain only after
`server-healthcheck.sh` and the registration, login, approval and account-sync
smoke tests pass.

## Backup and rollback

The source server should remain intact until cutover. Before a forced import,
the target operator should take a provider snapshot or a separate application
backup. If the new server fails, point DNS back to the old server; no client
configuration change is needed.

## Capacity expansion

The default Compose file remains single-node and keeps its stable container
names and diagnostic ports. For a controlled API scale-out, first confirm that
the database and Redis have sufficient connection capacity, then inspect the
merged configuration:

```bash
docker compose \
  -f /path/to/xianyu/docker-compose.yml \
  -f /path/to/xianyu/deploy/server-migration/docker-compose.api-scale.yml \
  --env-file /path/to/xianyu/.env config
```

After a provider snapshot and a staging smoke test, run the same file set with
`up -d --scale backend=2`. The frontend Nginx resolves the `backend` service
name inside the Compose network. Do not scale WebSocket or Scheduler with this
overlay: they own session/browser and task-scheduler responsibilities that
require connection draining and a distributed lock, which is the next
zero-downtime phase. Roll back by stopping the scaled project and starting the
default Compose file again.
