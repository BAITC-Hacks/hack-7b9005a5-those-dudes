# Render deployment

Deploy this repository as a **Python web service**, with the repository root as
the working directory. `render.yaml` contains a free demonstration configuration.
Select the branch containing that file when creating the service.

- Build: `pip install -r requirements.txt && python run_project.py --no-server`
- Start: `python run_project.py --skip-pipeline --skip-dashboard --host 0.0.0.0 --port $PORT`
- Health check: `/health`
- Python: the latest 3.12 patch, selected by `.python-version`

The build generates the included dataset's outputs and dashboard. Startup only
starts the server. Render provides `PORT` and `RENDER_EXTERNAL_URL`; the latter
allows HTTPS uploads while rejecting browser requests from other origins.
For a custom domain, set `PUBLIC_ORIGIN=https://your-domain.example`.

After deployment verify `/health`, `/dashboard?run=default`, a three-file Parquet
upload, the resulting dashboard, and its ZIP export. Browser uploads exercise
the HTTPS origin check, which a bare curl request without Origin does not test.

## Free instance limitations

Uploaded datasets and generated explanations live in `runtime/`. On a free
instance they are lost on restart, redeploy, or idle shutdown. The bundled
dataset is rebuilt during deployment and remains available. A paid instance
with a disk mounted at `/opt/render/project/src/runtime` can preserve uploads;
keep one application instance because jobs and locks are process-local.

The application has no user authentication. This configuration is for a public
demonstration of the included case data, not confidential uploads. The free
configuration installs no external AI SDK and supplies no API key. Local graph
analysis, chat, and CSV exports work without one. Add authentication and usage
controls before enabling paid AI or handling private data; then install
`requirements-ai.txt` and configure the key through Render's secret settings.

Official references: [web services](https://render.com/docs/web-services),
[Python versions](https://render.com/docs/python-version),
[free instance limits](https://render.com/docs/free).
