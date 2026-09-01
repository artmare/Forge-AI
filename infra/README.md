# Infrastructure

Infrastructure is defined by the root `docker-compose.yml`. PostgreSQL owns domain and lifecycle
data, Redis transports committed events, and `forge-workspaces` is a dedicated project-filesystem
volume mounted only into the API service. No host home or broad user directory is mounted.
