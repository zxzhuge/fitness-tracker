FROM python:3.13-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

COPY pyproject.toml uv.lock ./

RUN uv sync --frozen --no-dev --no-install-project

COPY . .

EXPOSE 5000

CMD ["uv", "run", "flask", "run", "--host=0.0.0.0", "--port=5000"]
