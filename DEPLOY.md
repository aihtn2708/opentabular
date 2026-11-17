# Deploying a test instance

`otab serve` is one Python process with DuckDB inside. It needs a host that runs
a container or a Python process, so serverless edge platforms (Cloudflare Workers
or Pages) cannot run it as is.

Whatever the host, set `OTAB_PASSWORD`. The server refuses to listen on a public
address without it, and then asks every visitor for the user `otab` and that
password. It is one shared password, not per-user access. Use the bundled example
data, or data you are allowed to expose to everyone who has the password.

## Option 1: Render free web service (hosted, no cost)

1. Put this repository on GitHub (a private repository works).
2. In Render choose **New > Blueprint** and pick the repository. Render reads
   `render.yaml`, builds the `Dockerfile` and creates the service
   `opentabular-demo` on the free plan.
3. Open the service's **Environment** page and copy the generated value of
   `OTAB_PASSWORD`.
4. Open the service URL (`https://<name>.onrender.com/`) and sign in as `otab`.
   The example app is at `/apps/revenue`.

Free services sleep after 15 minutes without traffic, so the first request
afterwards takes about a minute. The example data is read-only CSV inside the
image, so nothing is lost when the service restarts.

To serve your own project, replace `examples/sales` in the `Dockerfile` with your
project folder.

## Option 2: Cloudflare Quick Tunnel (from your computer, no cost)

```bash
pip install -e .
OTAB_PASSWORD='choose-a-password' otab serve -p examples/sales
# in a second terminal
cloudflared tunnel --url http://localhost:7979
```

`cloudflared` prints a temporary `trycloudflare.com` URL that works while both
commands run. To admit only named people, add
`--allowed-mail 'you@example.com,colleague@example.com'` to the tunnel command;
visitors then confirm a one-time code sent to their email.

## Option 3: Cloudflare Containers (hosted on Cloudflare, paid plan)

Cloudflare Containers can run the same `Dockerfile`, but they are not included
in the Workers Free plan; they need the Workers Paid plan. This repository does
not include the Worker and Wrangler configuration for it yet.

## Any other container host

```bash
docker build -t opentabular .
docker run -p 10000:10000 -e OTAB_PASSWORD='choose-a-password' opentabular
```

The container listens on `$PORT` (default 10000) and answers `GET /healthz`
without sign-in for health checks.
