FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
COPY api ./api
COPY agents ./agents
COPY saga ./saga
COPY disruption ./disruption
COPY mcp_servers ./mcp_servers
COPY evals ./evals
RUN pip install --no-cache-dir .
EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
